"""Parsing for MB Bank's server-side notification inbox.

``POST /api/retail_web/notification/getNotificationDataList`` returns the rows
the bank writes every time it pushes a balance-change notification towards the
phone app.  The native app only receives an FCM nudge and then fetches
exactly this list, so these rows are the bank's own event log - a far better
change detector than watching the balance for a difference, and the only
non-polling signal that exists outside the APK.

Two body shapes exist.

The classic transfer row, pipe separated::

    TK 92xxx456|GD: +95,000VND 06/11/26 06:50 |SD: 623,871VND|ND: QAAZEP6354 ...
    TK 75xxx009|GD: -170,000VND 03/11/26 21:10 |SD: 20,060VND|DEN: LE TRUNG QUOC - -30...|ND: ...

``TK`` is the masked account, ``GD`` the signed transaction amount (optionally
followed by extra fee legs such as ``Phi 8,012VND``), ``SD`` the balance after
the transaction, ``TU`` the sender (money in), ``DEN`` the receiver (money
out) and ``ND`` the description.

The card row, HTML flavoured::

    MB [VISA]:SD THE [484804....2039]<br>Ngày GD: [2026-11-05 06:43:11]<br>Số tiền GD: -4.43 USD ...

The row identity is the ``notiId`` plus the masked account: a single transfer
generates one notification per involved account and those rows share the same
``notiId``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from bm_lib.modals.transaction_history import Transaction

CARD_NOTI_TYPE = "MSG_NOTI_CARD"
VOICE_NOTI_TYPE = "MSG_NOTI_BDSD_T24_CHUDONG_PUSH_VOICE"

# "GD: -114,179VND 05/11/26 06:43 Phi 8,012VND" -> one leg per amount
_LEG_RE = re.compile(r"([+-]?)\s*(\d[\d,]*(?:\.\d+)?)\s*([A-Z]{2,4})")
_DT_RE = re.compile(r"(\d{2}/\d{2}/\d{2})\s+(\d{2}:\d{2})")
_TK_RE = re.compile(r"TK\s+([0-9A-Za-z*]+)")
_CARD_DT_RE = re.compile(r"Ngày GD:\s*\[?([^\]\n]+)\]?")
_CARD_AMOUNT_RE = re.compile(r"Số tiền GD:\s*\[?([^\]\n]+)\]?")
_CARD_ACCT_RE = re.compile(r"TKTT:\s*\[?([0-9]+)\]?")
_CARD_DESC_RE = re.compile(r"Nội dung GD:\s*\[?([^\]\n]+)\]?")


def _parse_bank_dt(raw: Any) -> Optional[datetime]:
    """Parse the bank's own timestamp fields ("06/11/2026 06:50:08")."""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None

# The first leg of a GD field is the principal amount, every further leg is a
# fee charged on top of it ("Phi 8,012VND").  Whether the bank already summed
# them up in the principal was settled against the live balance chain - see
# D:\\mbprobe\\test_notifications.py.
FEE_LEGS_ARE_EXTRA = True


def mask_account(account_no: str) -> str:
    """Mask an account the way the bank does: 9290123456 -> 92xxx456."""
    digits = "".join(char for char in str(account_no or "") if char.isdigit())
    if len(digits) < 5:
        return str(account_no or "")
    return digits[:2] + "xxx" + digits[-3:]


def _to_number(raw: str) -> Optional[float]:
    text = str(raw or "").replace(",", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value


def _segments(body: str) -> dict[str, str]:
    """Split the pipe separated body into ``LABEL -> value`` pairs."""
    result: dict[str, str] = {}
    for chunk in str(body or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            continue
        label, value = chunk.split(":", 1)
        label = label.strip().upper()
        if label and label not in result:
            result[label] = value.strip()
    return result


def _split_counterparty(raw: str) -> tuple[str, str]:
    """'LE TRUNG QUOC - -30072007686868' -> ('LE TRUNG QUOC', '-30072007686868')."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if " - " in text:
        name, account = text.split(" - ", 1)
        return name.strip(), account.strip()
    return "", text


def _parse_gd(chunk: str) -> tuple[float, str, Optional[datetime], list[tuple[float, str]]]:
    """Parse a GD field into (signed amount, currency, moment, legs)."""
    text = str(chunk or "")
    moment: Optional[datetime] = None
    match = _DT_RE.search(text)
    if match:
        try:
            moment = datetime.strptime(
                f"{match.group(1)} {match.group(2)}", "%d/%m/%y %H:%M"
            )
        except ValueError:
            moment = None
        text = text[: match.start()] + " " + text[match.end() :]

    legs: list[tuple[float, str]] = []
    for sign, number, currency in _LEG_RE.findall(text):
        value = _to_number(number)
        if value is None:
            continue
        if sign == "-":
            value = -value
        legs.append((value, currency))
    if not legs:
        return 0.0, "", moment, []

    principal = legs[0][0]
    extras = sum(abs(value) for value, _ in legs[1:])
    if FEE_LEGS_ARE_EXTRA:
        total = (abs(principal) + extras) * (-1 if principal < 0 else 1)
    else:
        total = principal
    return total, legs[0][1], moment, legs


def _parse_amount(chunk: str) -> tuple[float, str]:
    match = _LEG_RE.search(str(chunk or ""))
    if not match:
        return 0.0, ""
    value = _to_number(match.group(2)) or 0.0
    if match.group(1) == "-":
        value = -value
    return value, match.group(3)


@dataclass
class NotificationEvent:
    """One row of the bank's notification inbox."""

    noti_id: str
    noti_type: str
    account_mask: str
    account_no: Optional[str]
    direction: str  # "in" or "out"
    amount: float
    currency: str
    balance: Optional[float]
    occurred_at: Optional[datetime]
    description: str
    counterparty: str
    counterparty_account: str
    voice_text: str = ""
    raw_body: str = ""
    principal: float = 0.0
    legs: list[tuple[float, str]] = field(default_factory=list)
    # The bank's own clock on the row.  ``createDt`` is what the bank orders
    # by - the minute printed inside the body can collide when two transfers
    # land in the same minute, so it must never be used for ordering.
    created_at: Optional[datetime] = None
    pushed_at: Optional[datetime] = None

    @property
    def sort_key(self) -> tuple:
        """Bank ordering: createDt first, then the row id as a tie breaker."""
        return (
            self.created_at or self.occurred_at or datetime.min,
            self.noti_id,
        )

    @property
    def ref_no(self) -> str:
        """The transaction reference - the notiId starts with it.

        ``FT26310664785169-uuid-95b3a2c4-...`` belongs to transaction
        ``FT26310664785169``, which is exactly the refNo the history endpoint
        reports.  That is what makes the join between the two APIs possible.
        """
        if not self.noti_id:
            return ""
        head = self.noti_id.split("-")[0]
        return head if head and any(char.isalpha() for char in head) else ""

    @property
    def key(self) -> str:
        """Dedupe key: notiId plus the account the row belongs to."""
        return f"noti:{self.noti_id}|{self.account_mask}"

    def to_transaction(self, account_no: Optional[str] = None) -> Transaction:
        """Render the row as a transaction object the Discord layer understands."""
        moment = self.occurred_at or datetime.now()
        target_account = account_no or self.account_no or self.account_mask
        amount = abs(self.amount)
        if self.direction == "in":
            credit, debit = f"{amount:g}", "0"
        else:
            credit, debit = "0", f"{amount:g}"
        balance = self.balance if self.balance is not None else 0
        return Transaction(
            postingDate=moment.strftime("%d/%m/%Y %H:%M:%S"),
            transactionDate=moment.strftime("%d/%m/%Y %H:%M:%S"),
            accountNo=target_account,
            creditAmount=credit,
            debitAmount=debit,
            currency=self.currency or "VND",
            description=self.description,
            addDescription=self.voice_text,
            availableBalance=f"{balance:g}",
            beneficiaryAccount=self.counterparty_account,
            refNo=self.ref_no,
            benAccountName=self.counterparty,
            bankName="",
            benAccountNo=self.counterparty_account,
            dueDate="",
            docId=self.noti_id,
            transactionType=self.noti_type,
            pos="",
            tracingType="",
        )


def _parse_transfer(
    item: dict, body: str, noti_id: str, noti_type: str, voice: str
) -> Optional[NotificationEvent]:
    segments = _segments(body)
    amount, currency, moment, legs = _parse_gd(segments.get("GD", ""))
    balance, _balance_currency = _parse_amount(segments.get("SD", ""))
    counterparty, counterparty_account = _split_counterparty(
        segments.get("TU", "") or segments.get("DEN", "")
    )
    mask_match = _TK_RE.search(body)
    mask = mask_match.group(1) if mask_match else ""
    if not mask and not amount:
        return None
    principal = legs[0][0] if legs else 0.0
    created_at = _parse_bank_dt(item.get("createDt"))
    pushed_at = _parse_bank_dt(item.get("pushTime"))
    # ``pushTime`` carries seconds while the body only prints the minute.
    # It matches the history endpoint's transactionDate to within a second,
    # so it is the better "when did this happen" answer.
    occurred = pushed_at or created_at or moment
    return NotificationEvent(
        noti_id=noti_id,
        noti_type=noti_type,
        account_mask=mask,
        account_no=None,
        direction="out" if amount < 0 else "in",
        amount=amount,
        currency=currency or "VND",
        balance=balance or None,
        occurred_at=occurred,
        description=segments.get("ND", ""),
        counterparty=counterparty,
        counterparty_account=counterparty_account,
        voice_text=voice,
        raw_body=body,
        principal=principal,
        legs=legs,
        created_at=created_at,
        pushed_at=pushed_at,
    )


def _parse_card(
    item: dict, body: str, noti_id: str, noti_type: str, voice: str
) -> Optional[NotificationEvent]:
    flat = re.sub(r"<br\s*/?>", "\n", body)
    amount_match = _CARD_AMOUNT_RE.search(flat)
    amount, currency = 0.0, ""
    if amount_match:
        amount, currency = _parse_amount(amount_match.group(1))
    date_match = _CARD_DT_RE.search(flat)
    moment: Optional[datetime] = None
    if date_match:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S"):
            try:
                moment = datetime.strptime(date_match.group(1).strip(), fmt)
                break
            except ValueError:
                continue
    account_match = _CARD_ACCT_RE.search(flat)
    account_no = account_match.group(1) if account_match else None
    desc_match = _CARD_DESC_RE.search(flat)
    description = desc_match.group(1).strip() if desc_match else ""
    if not amount and not description:
        return None
    created_at = _parse_bank_dt(item.get("createDt"))
    pushed_at = _parse_bank_dt(item.get("pushTime"))
    return NotificationEvent(
        noti_id=noti_id,
        noti_type=noti_type,
        account_mask=mask_account(account_no) if account_no else "",
        account_no=account_no,
        direction="out" if amount < 0 else "in",
        amount=amount,
        currency=currency or "USD",
        balance=None,
        occurred_at=pushed_at or created_at or moment,
        description=description,
        counterparty="",
        counterparty_account="",
        voice_text=voice,
        raw_body=body,
        principal=amount,
        legs=[(amount, currency)] if currency else [],
        created_at=created_at,
        pushed_at=pushed_at,
    )


def parse_notification(item: dict) -> Optional[NotificationEvent]:
    """Turn one inbox row into a :class:`NotificationEvent`."""
    if not isinstance(item, dict):
        return None
    body = str(item.get("body") or item.get("shortBody") or "")
    if not body:
        return None
    noti_id = str(item.get("notiId") or "")
    noti_type = str(item.get("notiType") or "")
    voice = ""
    for extra in item.get("bodyListDataExtendParam") or []:
        if isinstance(extra, dict) and extra.get("dataKey") == "textVoice":
            voice = str(extra.get("dataValue") or "")
    if CARD_NOTI_TYPE in noti_type or "Số tiền GD:" in body:
        return _parse_card(item, body, noti_id, noti_type, voice)
    return _parse_transfer(item, body, noti_id, noti_type, voice)


def parse_notifications(items: list[dict]) -> list[NotificationEvent]:
    """Parse a whole inbox page, dropping the rows that cannot be read."""
    events = []
    for item in items or []:
        event = parse_notification(item)
        if event is not None:
            events.append(event)
    return events
