import base64
import datetime
import hashlib
import json
import os
import random
import re
import time
from urllib.parse import unquote

import aiohttp
import requests
from bs4 import BeautifulSoup as Bs4

from .errors import (
    BankNotFoundError,
    CapchaError,
    CryptoVerifyError,
    MBBankAPIError,
    MBBankError,
)
from .wasm_helper import wasm_encrypt

headers_default = {
    "Cache-Control": "max-age=0",
    "Accept": "application/json, text/plain, */*",
    "Authorization": "Basic RU1CUkVUQUlMV0VCOlNEMjM0ZGZnMzQlI0BGR0AzNHNmc2RmNDU4NDNm",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Origin": "https://online.mbbank.com.vn",
    "Referer": "https://online.mbbank.com.vn/pl/login?returnUrl=%2F",
    "App": "MB_WEB",
    "Sec-Ch-Ua": '"Not.A/Brand";v="8", "Chromium";v="134", "Google Chrome";v="134"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


async def login(user, password):
    stop = False
    while not stop:
        try:
            url = "https://online.mbbank.com.vn/api/retail-web-internetbankingms/getCaptchaImage"
            now = datetime.datetime.now()
            now = now + datetime.timedelta(hours=7)
            year = str(now.year)
            month = str(now.month if now.month > 9 else ("0" + str(now.month)))
            day = str(now.day if now.day > 9 else ("0" + str(now.day)))
            hour = str(now.hour if now.hour > 9 else ("0" + str(now.hour)))
            minute = str(now.minute if now.minute > 9 else ("0" + str(now.minute)))
            second = str(now.second if now.second > 9 else ("0" + str(now.second)))
            minisecond = str(
                now.microsecond if now.microsecond > 9 else ("0" + str(now.microsecond))
            )[0:1]
            ref = year + month + day + hour + minute + second + minisecond
            deviceId = "es4uuquy-mbib-0000-0000-" + ref
            data = {"refNo": ref, "deviceIdCommon": deviceId, "sessionId": ""}
            headers = {
                "authorization": "Basic RU1CUkVUQUlMV0VCOlNEMjM0ZGZnMzQlI0BGR0AzNHNmc2RmNDU4NDNm",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
                "Deviceid": deviceId,
            }
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
                async with session.post(url, headers=headers, json=data) as res:
                    js = await res.json()
                    imgdata = base64.b64decode(js["imageString"])
                    filename = "captcha.jpg"  # I assume you have a way of picking unique filenames
                    with open(filename, "wb") as f:
                        f.write(imgdata)
                    url = "https://tmpfiles.org/"
                    async with session.get(url, headers=headers) as res:
                        content = await res.text()
                        html = Bs4(content, "html.parser")
                        token = html.find("input", {"name": "_token"})["value"]
                        data = {
                            "file": open(filename, "rb"),
                            "_token": token,
                            "upload": "Upload",
                        }
                        async with session.post(
                            url, data=data, allow_redirects=False
                        ) as res:
                            if res.status < 400:
                                id = res.headers["location"].split("/")[3]
                                urlFile = (
                                    "https://tmpfiles.org/dl/" + id + "/" + filename
                                )
                                # print(urlFile)
                                url = "https://vision.googleapis.com/v1/images:annotate?key=AIzaSyAV-SXt0qiF5aHdn-Zgcl4Gr61_gxx28qs"
                                data = {
                                    "requests": [
                                        {
                                            "image": {"source": {"imageUri": urlFile}},
                                            "features": [
                                                {"type": "DOCUMENT_TEXT_DETECTION"}
                                            ],
                                        }
                                    ]
                                }
                                async with session.post(
                                    url,
                                    headers={
                                        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
                                        "Referer": "https://brandfolder.com/",
                                    },
                                    json=data,
                                ) as res:
                                    print(res.status)
                                    if res.status < 400:
                                        js = await res.json()
                                        print(js)
                                        text = js["responses"][0]["fullTextAnnotation"][
                                            "text"
                                        ]
                                        captcha = text.strip().replace(" ", "")
                                        data = {
                                            "userId": user,
                                            "password": password,
                                            "captcha": captcha,
                                            "ibAuthen2faString": "d94f855ee10ccd70171ce81c879e3643",
                                            "sessionId": None,
                                            "refNo": "59f482789f35c9b1d2c7ca80516043ad-2024050710134545",
                                            "deviceIdCommon": deviceId,
                                        }
                                        # print(captcha)
                                        async with session.post(
                                            "https://online.mbbank.com.vn/api/retail_web/internetbanking/v2.0/doLogin",
                                            headers=headers,
                                            json=data,
                                        ) as res:
                                            js = await res.json()
                                            # print(js)
                                            if (
                                                res.status < 400
                                                and js["result"]["ok"] == True
                                            ):
                                                stop = True
                                                sessionId = js["sessionId"]
                                                userId = js["cust"]["userId"]
                                                cards = js["cust"]["cardList"]
                                                for i, item in enumerate(cards):
                                                    if i == len(cards) - 1:
                                                        accNo = cards[item]["acctNo"]
                                                headers["RefNo"] = userId + "-" + ref
                                                print(user + " login success")
                                                return {
                                                    "headers": headers,
                                                    "sessionId": sessionId,
                                                    "userId": userId,
                                                    "cards": cards,
                                                    "deviceId": deviceId,
                                                }
                                            print(user, "Trying re-login...")
        except:
            pass


async def getTransaction(headers, deviceId, sessionId, userId, cards):
    for i, item in enumerate(cards):
        if i == len(cards) - 1:
            accNo = cards[item]["acctNo"]
    now = datetime.datetime.now()
    now = now  # +datetime.timedelta(hours=7)
    year = str(now.year)
    month = str(now.month if now.month > 9 else ("0" + str(now.month)))
    day = str(now.day if now.day > 9 else ("0" + str(now.day)))
    hour = str(now.hour if now.hour > 9 else ("0" + str(now.hour)))
    minute = str(now.minute if now.minute > 9 else ("0" + str(now.minute)))
    second = str(now.second if now.second > 9 else ("0" + str(now.second)))
    minisecond = str(
        now.microsecond if now.microsecond > 9 else ("0" + str(now.microsecond))
    )[0:1]
    ref = year + month + day + hour + minute + second + minisecond
    url = "https://online.mbbank.com.vn/api/retail-transactionms/transactionms/get-account-transaction-history"
    data = {
        "accountNo": accNo,
        "fromDate": f"{day}/{month}/{year}",
        "toDate": f"{day}/{month}/{year}",
        "sessionId": sessionId,
        "refNo": userId + "-" + ref,
        "deviceIdCommon": deviceId,
    }
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        async with session.post(url, headers=headers, json=data) as res:
            js = await res.json()
            if res.status < 400 and js["result"]["ok"] == True:
                return js["transactionHistoryList"]
            return False


def _get_wasm_file(self):
    if self._wasm_cache is not None:
        return self._wasm_cache
    file_data = requests.get(
        "https://online.mbbank.com.vn/assets/wasm/main.wasm",
        proxies=self.proxy,
        timeout=self.timeout,
    ).content
    self._wasm_cache = file_data
    return file_data


def get_capcha_image(self) -> bytes:
    """
    Get capcha image as bytes

    Returns:
        success (bytes): capcha image as bytes
    """
    rid = f"{self._userid}-{self._get_now_time()}"
    json_data = {
        "sessionId": "",
        "refNo": rid,
        "deviceIdCommon": self.deviceIdCommon,
    }
    headers = headers_default.copy()
    headers["X-Request-Id"] = rid
    headers["Deviceid"] = self.deviceIdCommon
    headers["Refno"] = rid
    with requests.post(
        "https://online.mbbank.com.vn/api/retail-internetbankingms/getCaptchaImage",
        headers=headers,
        json=json_data,
        proxies=self.proxy,
        timeout=self.timeout,
    ) as r:
        if r.status_code == 428:
            raise CryptoVerifyError(r.text, r.headers.get("Content-Type", ""))
        data_out = r.json()
        return base64.b64decode(data_out["imageString"])


def login1(self, captcha_text: str):
    """
    Login to MBBank account

    Args:
        captcha_text (str): capcha text from capcha image

    Raises:
        MBBankAPIError: if login failed
    """
    payload = {
        "userId": self._userid,
        "password": hashlib.md5(self._password.encode()).hexdigest(),
        "captcha": captcha_text,
        "sessionId": "",
        "refNo": f"{self._userid}-{self._get_now_time()}",
        "deviceIdCommon": self.deviceIdCommon,
        "ibAuthen2faString": self.FPR,
    }
    wasm_bytes = self._get_wasm_file()
    data_encrypt = wasm_encrypt(wasm_bytes, payload)
    with requests.post(
        "https://online.mbbank.com.vn/api/retail_web/internetbanking/v2.0/doLogin",
        headers=headers_default,
        json={"dataEnc": data_encrypt},
        proxies=self.proxy,
        timeout=self.timeout,
    ) as r:
        if r.status_code == 428:
            raise CryptoVerifyError(r.text, r.headers.get("Content-Type", ""))
        data_out = r.json()
    if data_out["result"]["ok"]:
        self.sessionId = data_out["sessionId"]
        data_out.pop("result", None)
        self._userinfo = data_out
        self._verify_biometric_check()
        return
    else:
        raise MBBankAPIError(data_out["result"])


self = None
self.ocr_class = ocr_class
captcha = get_capcha_image(self)
fileWasm = _get_wasm_file(self)
if captcha:
    login1(self, captcha)
