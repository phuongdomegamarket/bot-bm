"""Browser-faithful client for the MB Bank retail API.

The native MB app feels realtime because the bank pushes FCM notifications
into the APK binary.  That channel only exists inside the app, so from the
outside the fastest available signal is the pair of polling endpoints the
web application uses as well:

    POST /api/retail-accountms/accountms/getBalance
    POST /api/retail-transactionms/transactionms/get-account-transaction-history

This module speaks the exact request contract that the web app speaks,
because the API is picky in ways that are not documented anywhere and every
mistake looks identical from the outside: HTTP 200 with a body of
``{"result": {"responseCode": "GW200", "message": "Session invalid"}}``.

Three things have to be right, and all three were measured against the live
bank:

* **One cookie jar for the whole session.**  The login response carries load
  balancer affinity cookies (``mbbank``, ``BIGipServer~ACI~...``).  A request
  that lands on a different backend node is answered with GW200 "Session
  invalid" - indistinguishable from an expired session.  ``bm_lib`` opens a
  fresh ``aiohttp.ClientSession`` per request, which throws those cookies
  away, and that alone is enough to keep a session from ever surviving.
* **Per route header shapes.**  The web app sends the full browser header
  set (``origin``, ``sec-fetch-*``, ``priority``) to ``/retail_web/*`` but
  only a reduced set to the ``retail-*ms`` services.
* **A pre-login handshake.**  ``getServerTime`` has to be called before the
  captcha, otherwise the captcha endpoint answers GW266 "Device is invalid".

On top of that every request needs its own ``refNo`` in one of three shapes
(pre login, login, post login) - a reused ``refNo`` is rejected outright.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import ssl
import time
from datetime import datetime
from typing import Any, Callable, Optional

import aiohttp

from bm_lib.capcha_ocr import CapchaOCR
from bm_lib.errors import CapchaError, CryptoVerifyError, MBBankAPIError
from bm_lib.modals.balance import BalanceResponseModal
from bm_lib.modals.transaction_history import TransactionHistoryResponseModal
from bm_lib.wasm_helper import wasm_encrypt

BASE_URL = "https://online.mbbank.com.vn"
API_URL = BASE_URL + "/api"
WASM_URL = BASE_URL + "/assets/wasm/main.wasm"

#: 2FA fingerprint the web app sends with every login.
FPR = "c7a1beebb9400375bb187daa33de9659"

AUTHORIZATION = "Basic RU1CUkVUQUlMV0VCOlNEMjM0ZGZnMzQlI0BGR0AzNHNmc2RmNDU4NDNm"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

ROUTE_BALANCE = "/retail-accountms/accountms/getBalance"
ROUTE_HISTORY = "/retail-transactionms/transactionms/get-account-transaction-history"
ROUTE_NOTIFICATIONS = "/retail_web/notification/getNotificationDataList"
ROUTE_PUSH_MESSAGES = "/retail_web/notification/getPushMessages"
ROUTE_SERVER_TIME = "/ap-content-protectorms/v1.0/getServerTime"
ROUTE_CAPTCHA = "/retail-internetbankingms/getCaptchaImage"
ROUTE_LOGIN = "/retail_web/internetbanking/v2.0/doLogin"

DEFAULT_TIMEOUT = 25
MAX_CAPTCHA_ATTEMPTS = 12

Logger = Callable[[str, str], None]


class MBRateLimited(Exception):
    """The bank throttled the client (HTTP 429, empty body).

    This is not a dead session - it is a signal to slow down.  The session
    stays valid, so it must never trigger a re-login.
    """


class MBProtocolError(Exception):
    """The bank answered with something that is not a JSON API envelope."""


def _ts16() -> str:
    """Timestamp shape used by the web app: 14 digits + 2 centiseconds."""
    now = datetime.now()
    return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 10000:02d}"


def _rnd5() -> str:
    return f"{random.randint(0, 99999):05d}"


def _device_id() -> str:
    """Device id in the shape the bank accepts.

    ``<8 lowercase alnum>-mbib-0000-0000-<ts16>``.  The web app's own local
    storage id (8 hex - 8 hex) is rejected with GW266 "Device is invalid".
    """
    head = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))
    return f"{head}-mbib-0000-0000-{_ts16()}"


class MBClient:
    """Talks to the MB retail API the way the web application does.

    The client owns exactly one :class:`aiohttp.ClientSession` for its whole
    life, because the cookie jar inside that session is what keeps the login
    pinned to the backend node that issued it.
    """

    def __init__(
        self,
        *,
        username: str,
        password: str,
        timeout: int = DEFAULT_TIMEOUT,
        device_store_path: Optional[str] = None,
        log: Optional[Logger] = None,
        ocr: Optional[Any] = None,
    ) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.device_store_path = device_store_path
        self._log: Logger = log or (lambda level, message: None)
        self.ocr = ocr if ocr is not None else CapchaOCR()

        self.sessionId: Optional[str] = None
        self._userinfo: Optional[dict] = None
        self.device_id: str = self._load_device_id()

        self._session: Optional[aiohttp.ClientSession] = None
        self._wasm: Optional[bytes] = None
        self._logged_in_at: Optional[float] = None

    # ------------------------------------------------------------- device id

    def _load_device_id(self) -> str:
        """Reuse the stored device id, so a restart is not a new device.

        On Streamlit Cloud there is no writable home for the store file, so
        the device id can also arrive through the ``MB_DEVICE_ID`` secret.
        The bank throttles logins per device id, which makes a stable id the
        difference between a deploy that works and one that GW200s all day.
        """
        if self.device_store_path and os.path.exists(self.device_store_path):
            try:
                with open(self.device_store_path, "r", encoding="utf-8") as handle:
                    stored = handle.read().strip()
                if stored:
                    return stored
            except OSError:
                pass
        from_env = (os.environ.get("MB_DEVICE_ID") or "").strip()
        if from_env:
            return from_env
        return self._rotate_device_id()

    def _rotate_device_id(self) -> str:
        self.device_id = _device_id()
        if self.device_store_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.device_store_path)), exist_ok=True)
                with open(self.device_store_path, "w", encoding="utf-8") as handle:
                    handle.write(self.device_id)
            except OSError:
                pass
        return self.device_id

    # --------------------------------------------------------------- session

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            connector = aiohttp.TCPConnector(
                ssl=ssl_ctx, limit=8, ttl_dns_cache=300, keepalive_timeout=60
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                cookie_jar=aiohttp.CookieJar(unsafe=False),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # --------------------------------------------------------------- headers

    def _common_headers(self) -> dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "vi-VN",
            "app": "MB_WEB",
            "authorization": AUTHORIZATION,
            "content-type": "application/json; charset=UTF-8",
            "deviceid": self.device_id,
            "elastic-apm-traceparent": "00-{}-{:016x}-01".format(
                "".join(random.choice("0123456789abcdef") for _ in range(32)),
                random.getrandbits(64),
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "user-agent": USER_AGENT,
        }

    def _headers(self, refno: str, full: bool) -> dict[str, str]:
        """Build the header set for one request.

        ``full`` is the browser set (origin, sec-fetch-*, priority) used by
        ``/retail_web/*`` and by the pre-login calls.  The reduced set is what
        the ``retail-*ms`` services expect.
        """
        headers = self._common_headers()
        if full:
            headers.update(
                {
                    "origin": BASE_URL,
                    "priority": "u=1, i",
                    "sec-ch-ua": '"Chromium";v="153", "Not_A Brand";v="8"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                }
            )
        else:
            headers["referer"] = BASE_URL + "/"
        headers["refno"] = refno
        headers["x-request-id"] = refno
        return headers

    # ---------------------------------------------------------------- request

    async def _send(
        self,
        path: str,
        payload: dict,
        *,
        refno: str,
        full: bool,
        inject_session: bool,
    ) -> tuple[int, str]:
        """POST one API call and return ``(status, raw body)``."""
        body = dict(payload)
        if inject_session and self.sessionId:
            body.setdefault("sessionId", self.sessionId)
            body.setdefault("refNo", refno)
            body.setdefault("deviceIdCommon", self.device_id)
        headers = self._headers(refno, full)
        session = await self._ensure_session()
        async with session.post(
            API_URL + path,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as response:
            status = response.status
            raw = await response.text()
            content_type = response.headers.get("Content-Type", "")
        if status == 428:
            raise CryptoVerifyError(raw, content_type)
        if status == 429:
            raise MBRateLimited("MB Bank rate limited the request (HTTP 429)")
        return status, raw

    async def _call(
        self,
        path: str,
        payload: dict,
        *,
        refno: str,
        full: bool,
        inject_session: bool = True,
    ) -> dict:
        """POST one API call and unwrap the ``result`` envelope."""
        status, raw = await self._send(
            path, payload, refno=refno, full=full, inject_session=inject_session
        )
        try:
            data = json.loads(raw)
        except ValueError:
            raise MBProtocolError(
                f"HTTP {status} phản hồi không phải JSON (len={len(raw)})"
            ) from None
        if not isinstance(data, dict):
            raise MBProtocolError(f"HTTP {status} phản hồi không phải object")
        result = data.get("result")
        if result is None:
            raise MBProtocolError(f"HTTP {status} phản hồi thiếu trường result")
        if result.get("ok"):
            return data
        raise MBBankAPIError(result)

    # ------------------------------------------------------------------ login

    async def _wasm_bytes(self) -> bytes:
        if self._wasm is not None:
            return self._wasm
        session = await self._ensure_session()
        async with session.get(
            WASM_URL, headers=self._common_headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as response:
            if response.status != 200:
                raise MBProtocolError(f"tải main.wasm thất bại (HTTP {response.status})")
            self._wasm = await response.read()
        return self._wasm

    async def _handshake(self) -> None:
        """Trusted-time handshake.  Skip it and the captcha answers GW266."""
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        await self._send(
            ROUTE_SERVER_TIME, {"refNo": _ts16()}, refno=refno, full=True, inject_session=False
        )

    async def _captcha_bytes(self) -> bytes:
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        data = await self._call(
            ROUTE_CAPTCHA,
            {"refNo": _ts16(), "deviceIdCommon": self.device_id, "sessionId": ""},
            refno=refno,
            full=True,
            inject_session=False,
        )
        image = data.get("imageString")
        if not image:
            raise MBProtocolError("captcha không trả về imageString")
        return base64.b64decode(image)

    async def _do_login(self, captcha_text: str) -> None:
        wasm = await self._wasm_bytes()
        payload = {
            "userId": self.username,
            "password": hashlib.md5(self.password.encode()).hexdigest(),
            "captcha": captcha_text,
            "sessionId": "",
            "refNo": f"{self.username}-{_ts16()}-{_rnd5()}",
            "deviceIdCommon": self.device_id,
            "ibAuthen2faString": FPR,
        }
        loop = asyncio.get_running_loop()
        data_enc = await loop.run_in_executor(None, wasm_encrypt, wasm, payload)
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        data = await self._call(
            ROUTE_LOGIN, {"dataEnc": data_enc}, refno=refno, full=True, inject_session=False
        )
        session_id = data.get("sessionId")
        if not session_id:
            raise MBProtocolError("đăng nhập thành công nhưng không có sessionId")
        self.sessionId = session_id
        self._userinfo = {key: value for key, value in data.items() if key != "result"}
        self._logged_in_at = time.time()
        # Streamlit Cloud has no writable home: the operator must copy this
        # device id into the app secrets once, or every cold restart is a
        # brand-new device and burns one of the bank's per-device logins.
        if os.environ.get("MB_DEVICE_ID", "").strip() != self.device_id:
            self._log(
                "warning",
                f"deviceId hiện tại: {self.device_id} - nếu chạy trên cloud, "
                "hãy copy giá trị này vào Secrets (mb_device_id) để giữ nguyên thiết bị giữa các lần restart.",
            )

    async def login(self) -> None:
        """Log in, retrying on a wrong captcha or a rejected device id."""
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            try:
                await self._handshake()
                image = await self._captcha_bytes()
            except MBBankAPIError as exc:
                last_error = exc
                if exc.code == "GW266":
                    # device id rejected - draw a fresh one and try again
                    self._rotate_device_id()
                await asyncio.sleep(1.5)
                continue
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, self.ocr.process_image, image)
            try:
                await self._do_login(text)
            except MBBankAPIError as exc:
                last_error = exc
                if exc.code == "GW283":
                    # captcha misread - draw a new one, the session is untouched
                    await asyncio.sleep(1.0)
                    continue
                if exc.code == "GW266":
                    self._rotate_device_id()
                    await asyncio.sleep(1.0)
                    continue
                raise
            self._log("success", f"Đăng nhập MB thành công (thiết bị {self.device_id})")
            return
        raise CapchaError(
            f"Không đăng nhập được sau {MAX_CAPTCHA_ATTEMPTS} lần thử: {last_error}"
        )

    def invalidate(self) -> None:
        """Forget the session id so the next call logs in again."""
        self.sessionId = None
        self._userinfo = None

    async def ensure_login(self) -> None:
        if not self.sessionId:
            await self.login()

    # ------------------------------------------------------------------- data

    async def getBalance(self) -> BalanceResponseModal:
        """Balances of every account of the customer."""
        await self.ensure_login()
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        data = await self._call(ROUTE_BALANCE, {}, refno=refno, full=False)
        data.pop("result", None)
        try:
            return BalanceResponseModal.model_validate(data, strict=True)
        except Exception:
            return BalanceResponseModal.model_validate(data)

    async def getTransactionAccountHistory(
        self,
        accountNo: Optional[str] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> TransactionHistoryResponseModal:
        """Transaction list of one account.

        The window is capped at three months by the bank.  ``accountNo`` has
        to be a real account number - handing over the customer id answers
        GW300 "Source Account is not belong customer".
        """
        await self.ensure_login()
        now = datetime.now()
        to_date = to_date or now
        from_date = from_date or now
        payload = {
            "accountNo": accountNo or self.username,
            "fromDate": from_date.strftime("%d/%m/%Y"),
            "toDate": to_date.strftime("%d/%m/%Y"),
        }
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        data = await self._call(ROUTE_HISTORY, payload, refno=refno, full=False)
        data.pop("result", None)
        try:
            return TransactionHistoryResponseModal.model_validate(data, strict=True)
        except Exception:
            return TransactionHistoryResponseModal.model_validate(data)

    async def getNotifications(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> list[dict]:
        """The bank's own notification inbox - the feed the native app shows.

        This is the closest thing to a push channel that exists outside the
        APK.  When a transaction lands on the account the bank writes a
        notification row here; the native app is only nudged through FCM and
        then fetches exactly this list.

        The response is a flat list under the top level key
        ``notificationBusinessList``.  Every item carries the balance change
        in a pipe separated body::

            TK 92xxx456|GD: +20,000VND 06/11/26 20:24 |SD: 1,452,480VND|TU: ...|ND: ...

        Only ``fromDate`` and ``toDate`` narrow the list down - every other
        paging parameter is ignored by the service.
        """
        await self.ensure_login()
        now = datetime.now()
        to_date = to_date or now
        from_date = from_date or now
        payload = {
            "fromDate": from_date.strftime("%d/%m/%Y"),
            "toDate": to_date.strftime("%d/%m/%Y"),
        }
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        data = await self._call(ROUTE_NOTIFICATIONS, payload, refno=refno, full=False)
        items = data.get("notificationBusinessList")
        if items is None:
            return []
        if not isinstance(items, list):
            raise MBProtocolError("notificationBusinessList không phải list")
        return items

    async def getPushMessages(self) -> dict:
        """Which accounts have balance-change push switched on, and where.

        The answer explains why an account shows up in the inbox at all: the
        account has to be registered for the balance-change notification.
        """
        await self.ensure_login()
        refno = f"{self.username.upper()}-{_ts16()}-{_rnd5()}"
        data = await self._call(ROUTE_PUSH_MESSAGES, {}, refno=refno, full=False)
        return data.get("pushMessagesInfo") or {}
