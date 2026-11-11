"""Realtime tracking for MB Bank accounts.

The MB native app feels realtime because the bank pushes FCM notifications
into the APK and the app then reads the bank's own notification inbox.  That
inbox is reachable from the outside as well, so the tracker does exactly what
the app does, minus the FCM nudge:

    POST /api/retail_web/notification/getNotificationDataList   <- primary
    POST /api/retail-accountms/accountms/getBalance             <- change detector
    POST /api/retail-transactionms/transactionms/get-account-transaction-history
                                                               <- detail + safety net

The inbox is the bank's own event log: every balance change it pushes to a
phone is written there, with the signed amount, the resulting balance and the
counterparty.  Measured against the history endpoint it covers 41 of 41
transactions, and its ``pushTime`` matches the history ``transactionDate``
within a second - while the history ``postingDate`` is stamped at the end of
the day.  So the inbox is the realtime source and the history endpoint only
fills in the details (real reference number, description, fee legs).

Everything in this module exists to make that polling cheap and reliable
enough to feel realtime:

* one long lived aiohttp session - the TLS handshake is paid once instead
  of on every request (bm_lib opens and closes a session per call) and,
  more importantly, the load balancer affinity cookies that come with the
  login response survive.  A request that reaches a different backend node
  is answered with GW200 "Session invalid", which looks exactly like an
  expired session.
* one authenticated MB session that is reused instead of rebuilt on error
  (rebuilding means a fresh captcha OCR on every hiccup)
* the session is refreshed proactively: the bank hard-expires it after
  thirty minutes with "Session hit time_to_live max limit!!"
* the inbox is the primary signal, the balance is only a cheap change
  detector and the heavier history endpoint runs when there is something to
  detail or as a safety net
* dedupe keyed on the transaction refNo, shared between the inbox rows and
  the history rows so a transaction is announced exactly once, and persisted
  across restarts
* a poll interval that stays above three seconds, because the bank starts
  answering HTTP 429 below that
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Optional

from bm_lib.errors import CryptoVerifyError, MBBankAPIError
from mb_client import MBClient, MBProtocolError, MBRateLimited
from mb_notifications import (
    CARD_NOTI_TYPE,
    NotificationEvent,
    mask_account,
    parse_notifications,
)

DEFAULT_POLL_INTERVAL = 4.0
DEFAULT_IDLE_INTERVAL = 6.0
DEFAULT_HISTORY_INTERVAL = 60.0
DEFAULT_BALANCE_INTERVAL = 20.0
DEFAULT_IDLE_AFTER = 300.0
DEFAULT_TIMEOUT = 25
HISTORY_WINDOW_DAYS = 2
# The inbox window is right open and only day granular, so a timestamp
# cursor is impossible: every poll asks for whole days and dedupes locally.
NOTIFICATION_WINDOW_DAYS = 2
SEEN_TTL_DAYS = 30
DEFAULT_GRACE_MINUTES = 10
DEVICE_STORE_NAME = ".mb_device_id"
# The bank answers HTTP 429 when two calls land too close together
# (observed at ~1.3 s spacing).  Keep a floor between every request.
MIN_CALL_GAP = 1.8
# The bank expires a session after exactly thirty minutes.
SESSION_TTL_SECONDS = 25 * 60
# A card purchase shows up twice in the inbox: once as the card notice and
# once as the balance change it caused.  They land within seconds of each
# other, so a card row this close to a balance row is the same transaction.
CARD_SIBLING_SECONDS = 120
# The balance row of a foreign currency card spend carries the original card
# amount in its description: "RetailVisa-603156-4.43-USD-63086216...".
_CARD_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)-([A-Z]{3})\b")

Logger = Callable[[str, str], None]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _amount(raw: Any) -> float:
    """Read one of the bank's amount fields ("0", "95000", "4.43")."""
    try:
        return float(str(raw or "0").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def thread_ref_no(thread_name: str) -> Optional[str]:
    """Extract the transaction refNo out of a Discord thread name.

    The thread name is built as

        "<sign> <amount> <currency>/ <timestamp>/ <refNo>/ <accountNo>"

    The timestamp and the account number are pure digits while the refNo
    always carries letters (FT..., VQRQ...), so the refNo is the first
    segment after the header that looks like an alphanumeric refNo.
    Picking "the first numeric part" would grab the timestamp instead -
    that key never matches a real transaction and the dedupe silently does
    nothing.
    """
    parts = [part.strip() for part in str(thread_name).split("/")]
    candidates = [part for part in parts[1:] if part]
    for part in candidates:
        has_alpha = any(char.isalpha() for char in part)
        has_digit = any(char.isdigit() for char in part)
        if has_alpha and has_digit:
            return part
    for part in candidates:
        if any(char.isalpha() for char in part) and len(part) >= 6:
            return part
    return None


class MBTracker:
    """Keeps an MB session alive and surfaces new transactions as they land."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        account_no: Optional[str | Sequence[str]] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        idle_interval: float = DEFAULT_IDLE_INTERVAL,
        history_interval: float = DEFAULT_HISTORY_INTERVAL,
        balance_interval: float = DEFAULT_BALANCE_INTERVAL,
        idle_after: float = DEFAULT_IDLE_AFTER,
        timeout: int = DEFAULT_TIMEOUT,
        seen_store_path: Optional[str] = None,
        grace_minutes: int = DEFAULT_GRACE_MINUTES,
        log: Optional[Logger] = None,
        log_file: Optional[str] = None,
    ) -> None:
        self.username = username
        self.password = password
        # account_no accepts a single number or a list - one login can watch
        # every account, several logins cannot coexist (single-instance rule).
        # The property setter keeps accounts/_masks in sync, so code (and the
        # test suite) may reassign account_no at any time.
        self.account_no = account_no
        self.poll_interval = poll_interval
        self.idle_interval = idle_interval
        self.history_interval = history_interval
        self.balance_interval = balance_interval
        self.idle_after = idle_after
        self.timeout = timeout
        self.seen_store_path = seen_store_path
        self.grace_minutes = grace_minutes
        self._log: Logger = log or (lambda level, message: None)
        self.log_file = log_file

        self.mb: Optional[MBClient] = None
        self._seen: dict[str, float] = {}
        self._account_no_cache: Optional[str] = None
        # refNo -> history row, used to enrich the inbox rows with the fields
        # the notification body does not carry (bank name, full description).
        self._history_cache: dict[str, Any] = {}
        self._last_balance: Optional[str] = None
        self._last_balance_at: float = 0.0
        self._last_account: Optional[Any] = None
        self._last_history_at: float = 0.0
        self._last_change_at: float = time.monotonic()
        self._last_call_at: float = 0.0
        self.paused = False
        self._first_poll = True
        self.stats: dict[str, Any] = {
            "polls": 0,
            "errors": 0,
            "transactions": 0,
            "inbox_rows": 0,
            "last_poll_at": None,
            "last_latency_ms": None,
            "last_error": None,
            "balance": None,
            "account": None,
            "interval": poll_interval,
        }

    # ------------------------------------------------------------------ setup

    def _device_store_path(self) -> Optional[str]:
        if not self.seen_store_path:
            return None
        folder = os.path.dirname(os.path.abspath(self.seen_store_path))
        return os.path.join(folder, DEVICE_STORE_NAME)

    async def start(self) -> None:
        """Open the session and authenticate once.

        Raises MBBankAPIError when the bank rejects the credentials.
        """
        if self.mb is None:
            self.mb = MBClient(
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                device_store_path=self._device_store_path(),
                log=self._log,
            )
        await self._ensure_session()
        self._load_seen()
        # explicit credential check: fails loudly instead of inside the loop
        await self._call(self.mb.getBalance)
        self._flog(
            "success",
            "Đăng nhập MB thành công, bắt đầu theo dõi realtime "
            f"(tài khoản {', '.join(self.accounts) or 'tất cả'})",
        )
        self._log("success", "Đăng nhập MB thành công, bắt đầu theo dõi realtime")

    async def close(self) -> None:
        if self.mb is not None:
            close = getattr(self.mb, "close", None)
            if close is not None:
                await close()

    def _flog(self, level: str, message: str) -> None:
        """Append to the on-disk log file.

        The Streamlit UI log (log_queue) vanishes with the process and is
        invisible in the console log, which is why the overnight run of
        2026-11-09 left no trace of the tracker dying at 23:24.  This file
        survives.  Logging must never break polling, so every failure here is
        swallowed.
        """
        if not self.log_file:
            return
        try:
            if os.path.isfile(self.log_file) and os.path.getsize(
                self.log_file
            ) > 5 * 1024 * 1024:
                os.replace(self.log_file, self.log_file + ".1")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] [{level}] {message}\n")
        except Exception:
            pass

    async def _ensure_session(self) -> Any:
        """Make sure the client owns a live session.

        Kept as a method because the tracker used to own the session itself;
        the client does now, but the name is what the offline test patches.
        """
        if self.mb is None:
            raise RuntimeError("tracker chưa được start()")
        ensure = getattr(self.mb, "_ensure_session", None)
        if ensure is not None:
            return await ensure()
        return self.mb

    # ------------------------------------------------------------------- seen

    def tx_key(self, tx: Any) -> str:
        """Stable identity of a transaction, used for dedupe."""
        for field in ("refNo", "docId"):
            value = getattr(tx, field, None)
            if value and str(value).strip() not in ("0", "null", "None"):
                return f"{field}:{str(value).strip()}"
        raw = "|".join(
            str(getattr(tx, field, "") or "")
            for field in (
                "transactionDate",
                "creditAmount",
                "debitAmount",
                "description",
                "availableBalance",
            )
        )
        return "hash:" + hashlib.md5(raw.encode("utf-8")).hexdigest()

    def seed_seen(self, keys: Iterable[str]) -> None:
        """Mark keys as already announced (used at startup from Discord threads)."""
        now = time.time()
        for key in keys:
            self._seen[key] = now

    @staticmethod
    def _tx_datetime(tx: Any) -> Optional[datetime]:
        raw = getattr(tx, "transactionDate", None) or getattr(tx, "postingDate", None)
        if not raw:
            return None
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(raw).strip(), fmt)
            except ValueError:
                continue
        return None

    def _within_grace_at(self, moment: Optional[datetime]) -> bool:
        """True when the moment is recent enough to be worth announcing."""
        if moment is None:
            return True
        return moment >= datetime.now() - timedelta(minutes=self.grace_minutes)

    def _within_grace(self, tx: Any) -> bool:
        """True when the transaction is recent enough to be worth announcing."""
        return self._within_grace_at(self._tx_datetime(tx))

    def mark_seen(self, tx: Any) -> None:
        self._seen[self.tx_key(tx)] = time.time()
        self._save_seen()

    def _load_seen(self) -> None:
        if not self.seen_store_path or not os.path.exists(self.seen_store_path):
            return
        try:
            with open(self.seen_store_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        raw = payload.get("keys", {}) if isinstance(payload, dict) else {}
        for key, stamp in raw.items():
            try:
                stamp = float(stamp)
            except (TypeError, ValueError):
                stamp = 0.0
            if stamp >= cutoff:
                self._seen[key] = stamp

    def _save_seen(self) -> None:
        if not self.seen_store_path:
            return
        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        payload = {
            "saved_at": _now_iso(),
            "keys": {k: v for k, v in self._seen.items() if v >= cutoff},
        }
        tmp = self.seen_store_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self.seen_store_path)
        except Exception:
            pass

    # ------------------------------------------------------------------ poll

    @property
    def account_no(self) -> Optional[str]:
        """Primary account, kept for backwards compatibility (stats/UI)."""
        return self.accounts[0] if self.accounts else None

    @account_no.setter
    def account_no(self, value: Optional[str | Sequence[str]]) -> None:
        if isinstance(value, (list, tuple, set)):
            self.accounts: list[str] = [str(a) for a in value if a]
        elif value:
            self.accounts = [str(value)]
        else:
            self.accounts = []
        self._masks = {mask_account(a) for a in self.accounts}

    def _select_account(self, balance: Any) -> Any:
        accounts = list(getattr(balance, "acct_list", None) or [])
        if not accounts:
            return None
        for wanted in self.accounts:
            for account in accounts:
                if account.acctNo == wanted:
                    return account
        if self.account_no:
            self._log(
                "error",
                f"Không thấy tài khoản {self.account_no} trong danh sách, dùng tài khoản đầu tiên",
            )
        return accounts[0]

    def _account_for(self, event: NotificationEvent) -> Optional[str]:
        """The real account number behind an inbox row.

        The inbox only carries the masked account ("92xxx456"), so the row
        must be rendered with the number whose mask matches it.
        """
        for account in self.accounts:
            if event.account_mask == mask_account(account):
                return account
        return event.account_no or None

    async def _throttle(self) -> None:
        """Keep a floor between two consecutive bank calls.

        Right after login the credential check and the first poll would
        otherwise land ~1 s apart, which the bank answers with HTTP 429.
        """
        now = time.monotonic()
        gap = now - self._last_call_at
        if gap < MIN_CALL_GAP:
            await asyncio.sleep(MIN_CALL_GAP - gap)
        self._last_call_at = time.monotonic()

    async def _call(self, fn: Callable[..., Awaitable[Any]], **kwargs) -> Any:
        """Call an SDK coroutine, refreshing the wasm bundle once on HTTP 428.

        A transient GW200 on a session that is younger than the bank's 30
        minute TTL is retried in place first: live probes showed one call can
        answer "GW200 | Session invalid" while the very same session answers
        the next call fine.  Re-authenticating on every hiccup would burn a
        captcha each time and risk the single-instance login storm.
        """
        await self._throttle()
        try:
            return await fn(**kwargs)
        except CryptoVerifyError:
            # The bank wants the crypto handshake again: drop the cached
            # bundle so the next attempt fetches a fresh one.
            if self.mb is not None:
                for attribute in ("_wasm", "_wasm_cache"):
                    if hasattr(self.mb, attribute):
                        setattr(self.mb, attribute, None)
            await self._throttle()
            return await fn(**kwargs)
        except MBBankAPIError as exc:
            if not self._is_transient_gw200(exc):
                raise
            for wait in (2.0, 5.0, 10.0):
                self._log("info", f"GW200 thoáng qua, thử lại sau {wait:.0f}s...")
                await asyncio.sleep(wait)
                await self._throttle()
                try:
                    return await fn(**kwargs)
                except CryptoVerifyError:
                    if self.mb is not None:
                        for attribute in ("_wasm", "_wasm_cache"):
                            if hasattr(self.mb, attribute):
                                setattr(self.mb, attribute, None)
                    await self._throttle()
                    return await fn(**kwargs)
                except MBBankAPIError as retry_exc:
                    if not self._is_transient_gw200(retry_exc):
                        raise retry_exc
            raise exc

    def _is_transient_gw200(self, exc: MBBankAPIError) -> bool:
        """A GW200 that should be retried on the SAME session.

        The hard TTL death ("Session hit time_to_live max limit!!") is NOT
        retried here - _ensure_fresh_login should have caught it; anything
        plain "Session invalid" within the TTL window gets three patient
        retries before the loop gives up and _recover re-logs in.
        """
        text = str(exc)
        if "time_to_live" in text:
            return False
        return exc.code == "GW200"

    async def _ensure_fresh_login(self) -> None:
        """Re-login before the bank's 30 minute session TTL kills the session.

        ``GW200 | Session hit time_to_live max limit!!`` lands at exactly
        +30 minutes on the clock, so the session is refreshed at the 25
        minute mark instead of waiting for the error and the missed polls
        around it.
        """
        mb = self.mb
        if mb is None:
            return
        if not mb.sessionId:
            await mb.login()
            return
        logged_at = getattr(mb, "_logged_in_at", None)
        if logged_at and (time.time() - logged_at) >= SESSION_TTL_SECONDS:
            self._log("info", "Phiên MB sắp hết hạn (TTL 30 phút), đăng nhập lại...")
            await mb.login()

    def _is_tracked(self, event: NotificationEvent) -> bool:
        """Only rows about the watched account matter.

        One transfer emits one inbox row per involved account, all sharing
        the same notiId - the other side's row must not be announced here.
        """
        if self.accounts:
            if event.account_no and event.account_no in self.accounts:
                return True
            return event.account_mask in self._masks
        return bool(event.account_mask)

    @staticmethod
    def _is_card_sibling(
        event: NotificationEvent, batch: list[NotificationEvent]
    ) -> bool:
        """True when this transfer row is the VND shadow of a card purchase.

        One card purchase produces two inbox rows seconds apart: a CARD row
        in the original currency and a BDSD row in VND whose description
        names the original amount ("RetailVisa-603156-4.43-USD-...").  The
        CARD row is announced; this row would only repeat the purchase.
        """
        if event.noti_type == CARD_NOTI_TYPE:
            return False
        for other in batch:
            if other is event or other.noti_type != CARD_NOTI_TYPE:
                continue
            if event.occurred_at is None or other.occurred_at is None:
                continue
            if (
                abs((event.occurred_at - other.occurred_at).total_seconds())
                > CARD_SIBLING_SECONDS
            ):
                continue
            if event.currency == other.currency and abs(event.amount) == abs(
                other.amount
            ):
                return True
            token = f"{abs(other.amount):g}-{other.currency}"
            if token and token in event.description:
                return True
        return False

    def _enrich(self, tx: Any) -> None:
        """Fill the fields the notification body does not carry from history."""
        row = self._history_cache.get(tx.refNo) if tx.refNo else None
        if row is None:
            return
        if not tx.bankName:
            tx.bankName = row.bankName or ""
        if not tx.benAccountName:
            tx.benAccountName = row.benAccountName or ""
        if not tx.benAccountNo:
            tx.benAccountNo = row.benAccountNo or ""

    async def poll(self) -> dict[str, Any]:
        """One polling cycle: the inbox is the change detector.

        ``getNotificationDataList`` is the bank's own event log - one row per
        balance-change notification it pushed towards the phone app, and it
        covers every tracked transaction.  Balance and history stay as the
        cheap change detector and the detail/safety net.  The inbox window is
        right-open at day granularity, so there is no timestamp cursor: every
        poll re-reads the window and dedupes locally on the transaction key.
        """
        if self.mb is None:
            raise RuntimeError("tracker chưa được start()")
        started = time.time()
        await self._ensure_fresh_login()

        now_mono = time.monotonic()
        account = None
        balance_changed = False
        need_balance = (
            self._last_balance_at == 0.0
            or (now_mono - self._last_balance_at) >= self.balance_interval
        )
        if need_balance:
            self._last_balance_at = now_mono
            balance_response = await self._call(self.mb.getBalance)
            account = self._select_account(balance_response)
            account_no = account.acctNo if account is not None else self.account_no
            if account_no:
                self._account_no_cache = account_no
            current = account.currentBalance if account is not None else None
            balance_changed = current is not None and current != self._last_balance
            if current is not None:
                self._last_balance = current
            if balance_changed:
                self._last_change_at = now_mono
        balance = self._last_balance
        account_no = self._account_no_cache or self.account_no

        transactions: list[Any] = []
        dirty = False

        # ---- primary signal: the bank's notification inbox
        now = datetime.now()
        rows = await self._call(
            self.mb.getNotifications,
            from_date=now - timedelta(days=NOTIFICATION_WINDOW_DAYS),
            to_date=now + timedelta(days=1),
        )
        events = [e for e in parse_notifications(rows) if self._is_tracked(e)]
        events.sort(key=lambda e: e.sort_key)
        # a zero-amount row means the GD segment did not parse: its refNo
        # stays free so the history safety net can announce the real amounts
        inbox_refs = {
            e.ref_no for e in events if e.ref_no and abs(e.amount) > 1e-9
        }

        for event in events:
            if abs(event.amount) <= 1e-9:
                # zero amount = unparseable GD segment.  Announcing it would
                # create a bogus "+0 VND" thread (the 10/11 08:45 incident).
                # Mark it under its notiId key so the warning fires once and
                # leave the refNo free for the history safety net.
                if event.key not in self._seen:
                    self._seen[event.key] = time.time()
                    dirty = True
                    self._log(
                        "warning",
                        f"Bỏ qua thông báo không đọc được số tiền "
                        f"(notiId={event.noti_id}): {event.raw_body[:200]!r}",
                    )
                continue
            if self._is_card_sibling(event, events):
                continue
            tx = event.to_transaction(self._account_for(event) or account_no)
            self._enrich(tx)
            key = self.tx_key(tx)
            if key in self._seen:
                continue
            if self._first_poll and not self._within_grace_at(event.occurred_at):
                # startup: old transactions are recorded, not announced
                self._seen[key] = time.time()
                dirty = True
                continue
            # Mark as seen right away: the transaction is handed to the
            # caller once, never announced twice inside the same process.
            self._seen[key] = time.time()
            dirty = True
            transactions.append(tx)

        # ---- safety net: history catches rows the inbox does not know.
        # With several watched accounts the loop visits each one; every _call
        # is throttled to MIN_CALL_GAP, so the gap protects against 429.
        if self.accounts and (
            self._first_poll
            or balance_changed
            or (now_mono - self._last_history_at) >= self.history_interval
        ):
            self._last_history_at = now_mono
            for watched in self.accounts:
                history = await self._call(
                    self.mb.getTransactionAccountHistory,
                    accountNo=watched,
                    from_date=now - timedelta(days=HISTORY_WINDOW_DAYS),
                    to_date=now,
                )
                history_rows = getattr(history, "transactionHistoryList", None) or []
                for row in history_rows:
                    if row.refNo:
                        self._history_cache[row.refNo] = row
                if len(self._history_cache) > 1000:
                    self._history_cache = {r.refNo: r for r in history_rows if r.refNo}
                for row in history_rows:
                    if row.refNo and row.refNo in inbox_refs:
                        # the inbox already covers this transaction - and deduping
                        # by refNo here would swallow the fee leg, which history
                        # reports as a second row under the same refNo
                        continue
                    credit = (getattr(row, "creditAmount", "") or "").strip()
                    debit = (getattr(row, "debitAmount", "") or "").strip()
                    if credit in ("", "0") and debit in ("", "0"):
                        # no amount on either side: not a money movement (would
                        # render as a bogus "+0 VND" thread).  Left unmarked so a
                        # later row with real amounts still gets announced.
                        continue
                    key = self.tx_key(row)
                    if key in self._seen:
                        continue
                    if self._first_poll and not self._within_grace(row):
                        self._seen[key] = time.time()
                        dirty = True
                        continue
                    self._seen[key] = time.time()
                    dirty = True
                    transactions.append(row)

        if transactions:
            self._last_change_at = now_mono
        self._first_poll = False
        if dirty:
            self._save_seen()

        latency_ms = int((time.time() - started) * 1000)
        self.stats.update(
            {
                "polls": self.stats["polls"] + 1,
                # a clean poll means the previous error is history - clear it
                # so the heartbeat stops displaying stale errors forever.
                "last_error": None,
                "transactions": self.stats["transactions"] + len(transactions),
                "inbox_rows": len(rows),
                "last_poll_at": _now_iso(),
                "last_latency_ms": latency_ms,
                "balance": balance,
                "account": account_no,
                "interval": self._current_interval(),
            }
        )
        return {
            "account": account,
            "account_no": account_no,
            "balance": balance,
            "balance_changed": balance_changed,
            "transactions": transactions,
            "inbox_rows": len(rows),
            "latency_ms": latency_ms,
        }

    def _current_interval(self) -> float:
        idle = (time.monotonic() - self._last_change_at) >= self.idle_after
        return self.idle_interval if idle else self.poll_interval

    # ------------------------------------------------------------------ loop

    async def run_forever(
        self,
        on_new: Callable[[list[Any], dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Poll forever, calling on_new with unseen transactions."""
        backoff = 1.0
        last_alive = time.monotonic()
        self._flog("info", "run_forever bắt đầu")
        while True:
            if self.paused:
                await asyncio.sleep(1.0)
                continue
            try:
                result = await self.poll()
                backoff = 1.0
                if result["transactions"]:
                    self._flog(
                        "success",
                        f"Đăng {len(result['transactions'])} giao dịch: "
                        + "; ".join(
                            f"{getattr(tx, 'refNo', '?')} "
                            f"{getattr(tx, 'debitAmount', None) or getattr(tx, 'creditAmount', None)}"
                            for tx in result["transactions"]
                        ),
                    )
                    await on_new(result["transactions"], result)
                interval = self._current_interval()
                self.stats["interval"] = interval
                now_mono = time.monotonic()
                if now_mono - last_alive >= 900:
                    # proof of life: 15-minute heartbeat, only to the file
                    last_alive = now_mono
                    self._flog(
                        "info",
                        f"còn sống: polls={self.stats['polls']} "
                        f"inbox={self.stats['inbox_rows']} "
                        f"errors={self.stats['errors']}",
                    )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                self._flog("error", "run_forever bị CANCELLED - task đã bị hủy")
                raise
            except MBBankAPIError as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = str(exc)
                self._flog("error", f"MB API lỗi: {exc}")
                self._log("error", f"MB API lỗi: {exc}")
                await self._recover(exc)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
            except MBRateLimited as exc:
                # Throttled, not dead: the session is still good, so slow down
                # without throwing it away.
                self.stats["errors"] += 1
                self.stats["last_error"] = str(exc)
                self._flog("error", f"MB giới hạn tần suất: {exc}")
                self._log("error", f"MB giới hạn tần suất: {exc}")
                await asyncio.sleep(max(backoff, 10.0))
                backoff = min(60.0, backoff * 2)
            except Exception as exc:  # network hiccup, timeout, ...
                self.stats["errors"] += 1
                self.stats["last_error"] = str(exc)
                self._flog(
                    "error",
                    f"Lỗi khi polling: {exc}\n{traceback.format_exc()}",
                )
                self._log("error", f"Lỗi khi polling: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def _recover(self, exc: MBBankAPIError) -> None:
        """Re-authenticate the SAME session instead of building a new client.

        A fresh client means a fresh captcha every single time, which is how
        the old loop turned one hiccup into a login storm.
        """
        if self.mb is None:
            return
        if exc.code in ("GW200", "GW21"):
            # The bank throttles LOGINS per device id per day (4 logins in
            # ~1h15m left the device logged-in-but-blinded all day).  When
            # the session keeps dying right after each login, re-login is
            # fuel, not fire-extinguisher: cap it to one attempt per 5
            # minutes and let the poll retries ride the existing session.
            now_mono = time.monotonic()
            # The bank flags the whole ACCOUNT after login storms; every
            # extra login burns device quota and deepens the flag, so the
            # spacing between attempts grows: 5 min -> 10 -> 20 -> 40 -> cap
            # at 60 min.  A long healthy stretch resets the spacing.
            last = getattr(self, "_last_relogin_at", 0.0)
            gap = getattr(self, "_relogin_gap", 300.0)
            if now_mono - last < gap:
                self._flog(
                    "warning",
                    f"Bỏ qua đăng nhập lại (chống login-storm, chờ {gap / 60:.0f} phút giữa các lần)",
                )
                return
            if now_mono - last < 3 * 3600.0:
                self._relogin_gap = min(3600.0, gap * 2)
            else:
                self._relogin_gap = 300.0
            self._last_relogin_at = now_mono
            invalidate = getattr(self.mb, "invalidate", None)
            if invalidate is not None:
                invalidate()
            else:
                self.mb.sessionId = None
                self.mb._userinfo = None
            login = getattr(self.mb, "login", None)
            if login is None:
                return
            try:
                await login()
                self._log("info", "Đã đăng nhập lại MB thành công")
            except Exception as auth_exc:
                self._log("error", f"Đăng nhập lại thất bại: {auth_exc}")


async def _self_test() -> None:  # pragma: no cover - manual smoke test
    from dotenv import load_dotenv

    # override=True: on Windows os.environ is case-insensitive and USERNAME is
    # already taken, so the .env entry would be skipped and the login would
    # go out with the Windows account name and an empty password.
    load_dotenv(override=True)
    tracker = MBTracker(
        username=os.environ.get("username", ""),
        password=os.environ.get("password", ""),
        account_no=[
            a.strip() for a in os.environ.get("account_no", "").split(",") if a.strip()
        ],
        seen_store_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seen_tx.json"),
        log=lambda level, message: print(f"[{level}] {message}"),
    )
    await tracker.start()
    try:
        result = await tracker.poll()
        print(f"balance={result['balance']} latency={result['latency_ms']}ms")
        for tx in result["transactions"]:
            print(" ", tracker.tx_key(tx), tx.transactionDate, tx.description)
    finally:
        await tracker.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_self_test())
