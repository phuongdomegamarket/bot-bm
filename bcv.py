import asyncio
import base64
import json
import locale
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from webbrowser import get

import aiohttp
import easyocr
import numpy as np
import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

BASE_URL = "https://digiapp.vietcombank.com.vn/bank-service"
DEFAULT_PAYLOAD = {
    "DT": "Windows",
    "E": None,
    "OV": "10",
    "PM": "Firefox 147.0",
    "appVersion": "",
    "lang": "en",
}


def generate_key():
    # 1. Khởi tạo và tạo cặp khóa RSA 1024 bit
    private_key = rsa.generate_private_key(
        public_exponent=65537,  # Tương đương 0x10001
        key_size=1024,
    )
    public_key = private_key.public_key()

    # 2. Xuất Khóa Private dưới dạng Base64 (Loại bỏ header/footer)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # PKCS#1 giống node-forge
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_base64 = "".join(private_pem.decode().splitlines()[1:-1])

    # 3. Xuất Khóa Public dưới dạng Base64 (Loại bỏ header/footer)
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,  # PKCS#8/X.509
    )
    public_key_base64 = "".join(public_pem.decode().splitlines()[1:-1])
    return {
        "private_key_base64": private_key_base64,
        "public_key_base64": public_key_base64,
        "private_key_pem": private_pem,
        "public_key_pem": public_pem,
    }


def generate_rsa_keypair_1024():
    """
    Tạo cặp khóa RSA 1024-bit giống node-forge (public exponent = 65537).
    Trả về: public_pem, private_pem, public_base64, private_base64
    """
    # Tạo private key (tự động sinh public key kèm theo)
    private_key = rsa.generate_private_key(
        public_exponent=65537,  # tương đương 0x10001
        key_size=1024,
    )

    # Public key PEM (SubjectPublicKeyInfo format - giống node-forge)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )

    # Private key PEM (PKCS8 unencrypted - phổ biến và tương thích tốt)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    # Base64 thuần (giống cách bạn làm trong JS: bỏ header/footer + xóa khoảng trắng)
    public_base64 = "".join(
        line
        for line in public_pem.splitlines()
        if not line.startswith("-----") and line.strip()
    )

    private_base64 = "".join(
        line
        for line in private_pem.splitlines()
        if not line.startswith("-----") and line.strip()
    )

    return {
        "public_key_pem": public_pem,
        "private_key_pem": private_pem,
        "public_key_base64": public_base64,
        "private_key_base64": private_base64,
    }


results = generate_rsa_keypair_1024()
DEFAULTS = {
    "headers": {
        "X-Channel": "Web",
        "Content-Type": "application/json",
    },
    "browser_id": "a4a797ced529a8127044cc42eg0d6996",
    "server_public_key_base64": "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQklqQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FROEFNSUlCQ2dLQ0FRRUFpa3FRckl6WkprVXZIaXNqZnU1WkNOK1RMeS8vNDNDSWM1aEpFNzA5VElLM0hiY0M5dnVjMitQUEV0STZwZVNVR3FPbkZvWU93bDNpOHJSZFNhSzE3RzJSWk4wMU1JcVJJSi82YWM5SDRMMTFkdGZRdFI3S0hxRjdLRDBmajZ2VTRrYjUrMGN3UjNSdW1CdkRlTWxCT2FZRXBLd3VFWTlFR3F5OWJjYjVFaE5HYnh4TmZiVWFvZ3V0VndHNUMxZUtZSXR6YVlkNnRhbzNncTdzd05IN3A2VWRsdHJDcHhTd0ZFdmM3ZG91RTJzS3JQRHA4MDdaRzJkRnNsS3h4bVI0V0hESFdmSDBPcHpyQjVLS1dRTnl6WHhUQlhlbHFyV1pFQ0xSeXBOcTdQKzFDeWZnVFNkUTM1ZmRPN00xTW5pU0JUMVYzM0xkaFhvNzMvOXFENWU1VlFJREFRQUIKLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0t",
    "private_key_base64": results["private_key_base64"],
    "private_key_pem": results["private_key_pem"],
    "public_key_base64": results["public_key_base64"],
    "public_key_pem": results["public_key_pem"],
}


def encrypt_request(payload, client_public_key_base64, server_pub_pem_b64):
    try:
        # 1. Tạo AES key (32 bytes) và IV (16 bytes)
        aes_key = os.urandom(32)
        iv = os.urandom(16)

        # 2. Thêm public key vào payload
        enhanced_payload = {"clientPubKey": client_public_key_base64, **payload}

        # 3. JSON String to Bytes
        json_data = json.dumps(enhanced_payload).encode("utf-8")

        # 4. Mã hóa AES-CTR
        cipher = Cipher(
            algorithms.AES(aes_key), modes.CTR(iv), backend=default_backend()
        )
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(json_data) + encryptor.finalize()

        # 5. Ghép IV + ciphertext và convert Base64
        combined_data = iv + ciphertext
        d_param = base64.b64encode(combined_data).decode("utf-8")

        # 6. Mã hóa AES key bằng RSA Public Key của Server
        # Server PEM từ base64 trong code của bạn
        server_pem_bytes = base64.b64decode(server_pub_pem_b64)
        server_public_key = serialization.load_pem_public_key(server_pem_bytes)

        # node-forge encode64 key trước khi encrypt RSA
        aes_key_b64 = base64.b64encode(aes_key)

        # Mã hóa RSA PKCS1v15 (tương đương forge mặc định)
        encrypted_aes_key = server_public_key.encrypt(aes_key_b64, padding.PKCS1v15())

        return {"d": d_param, "k": base64.b64encode(encrypted_aes_key).decode("utf-8")}
    except Exception as e:
        print(f"Lỗi mã hóa: {e}")
        return {"d": "", "k": ""}


def decrypt_response(response, private_key_pem):
    try:
        # 1. Load Private Key
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )

        # 2 & 3. Giải mã RSA để lấy AES Key Base64
        encrypted_key_bytes = base64.b64decode(response["k"])
        decrypted_aes_key_b64 = private_key.decrypt(
            encrypted_key_bytes, padding.PKCS1v15()
        )

        # 4. Chuyển base64 -> raw bytes
        # node-forge: decodeUtf8 rồi decode64
        aes_key_raw = base64.b64decode(decrypted_aes_key_b64.decode("utf-8"))

        # 5 & 6. Tách IV và Ciphertext từ 'd'
        combined_data = base64.b64decode(response["d"])
        iv = combined_data[:16]
        ciphertext = combined_data[16:]

        # 7, 8, 9. Giải mã AES-CTR
        cipher = Cipher(
            algorithms.AES(aes_key_raw), modes.CTR(iv), backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext_bytes = decryptor.update(ciphertext) + decryptor.finalize()

        # 10. Decode UTF-8
        return plaintext_bytes.decode("utf-8")
    except Exception as e:
        print(f"Lỗi giải mã: {e}")
        return None


class CaptchaManager:
    def __init__(self):
        self.expire_time_captcha = 0
        self.captcha_guid = ""
        self.captcha_image_url = ""

    def get_captcha(self):
        # 1. Cập nhật thời gian hết hạn (timestamp tính bằng miligiây)
        self.expire_time_captcha = int(time.time() * 1000)

        # 2. Tạo GUID mới chuẩn UUID v4
        # uuid.uuid4() tự động tạo định dạng xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
        self.captcha_guid = str(uuid.uuid4())

        # 3. Xây dựng URL tải hình CAPTCHA
        self.captcha_image_url = f"https://digiapp.vietcombank.com.vn/utility-service/v2/captcha/MASS/{self.captcha_guid}"
        # URL ảnh cần OCR (thay bằng link của bạn)
        # url = self.captcha_image_url  # ← thay link thật vào đây

        # response = requests.get(url)
        # if response.status_code != 200:
        #     print(f"Lỗi tải ảnh: {response.status_code}")
        #     exit()

        # image_bytes = response.content

        # # Chuyển bytes → PIL Image → numpy array (RGB)
        # img_pil = Image.open(BytesIO(image_bytes)).convert("RGB")
        # img_array = np.array(img_pil)  # shape (height, width, 3)

        # # Khởi tạo EasyOCR Reader (chạy lần đầu sẽ tải model về ~ vài trăm MB)
        # # ['vi', 'en'] để hỗ trợ tiếng Việt + Anh tốt hơn
        # reader = easyocr.Reader(["vi", "en"], gpu=True)  # gpu=False nếu không có GPU

        # # Chạy OCR
        # # detail=1: trả về bounding box + text + confidence
        # # detail=0: chỉ text
        # results = reader.readtext(img_array, detail=1)

        # # In kết quả
        # for detection in results:
        #     bbox = detection[0]  # list 4 điểm [[x1,y1], [x2,y2], ...]
        #     text = detection[1]  # chữ nhận diện
        #     confidence = detection[2]  # độ tin cậy (0-1)
        #     print(f"Text: {text:<40} | Confidence: {confidence:.2%}")
        print("New CAPTCHA:")
        print(f"- GUID: {self.captcha_guid}")
        print(f"- Image URL: {self.captcha_image_url}")
        print(f"- Expire time: {self.expire_time_captcha}")

        return {"guid": self.captcha_guid, "url": self.captcha_image_url}


# Sử dụng
manager = CaptchaManager()
# guid = manager.get_captcha()

private_key_base64 = results["private_key_base64"]
public_key_base64 = results["public_key_base64"]


async def login(
    username,
    password,
    GUID=None,
    captcha_value=None,
    headers=None,
    browser_id=None,
    public_key_base64=None,
    private_key_pem=None,
    save_browser=False,
):
    global DEFAULT_PAYLOAD
    if not GUID or not captcha_value:
        result = manager.get_captcha()
        GUID = result["guid"]
        captcha_url = result["url"]
        captcha_value = await getTextFromImage(captcha_url)
    if not public_key_base64 or not private_key_pem:
        public_key_base64 = DEFAULTS["public_key_base64"]
        private_key_pem = DEFAULTS["private_key_pem"]
    if not browser_id:
        browser_id = DEFAULTS["browser_id"]
    if not headers:
        headers = DEFAULTS["headers"]
    if public_key_base64 and private_key_pem:
        payload = encrypt_request(
            {
                "captchaToken": GUID,
                "captchaValue": captcha_value,
                "password": password,
                "user": username,
                "browserId": browser_id,
                "mid": 6,
                "lang": "vi",
                "E": None,
                "DT": "Windows",
                "PM": "Chrome 144.0.0.0",
                "OV": "10",
                "appVersion": "",
            },
            public_key_base64,
            DEFAULTS["server_public_key_base64"],
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://digiapp.vietcombank.com.vn/authen-service/v1/login",
                headers=headers,
                json=payload,
            ) as response:
                try:
                    jsonData = decrypt_response(
                        json.loads(await response.text()), private_key_pem
                    )
                    if jsonData and "sessionId" in jsonData:
                        jsonData = json.loads(jsonData)
                        DEFAULT_PAYLOAD = {
                            **DEFAULT_PAYLOAD,
                            "sessionId": jsonData["sessionId"],
                            "browserId": browser_id,
                            "mobileId": jsonData["userInfo"]["mobileId"],
                            "accountType": jsonData["userInfo"]["defaultAccountType"],
                            "user": username,
                            "clientId": jsonData["userInfo"]["clientId"],
                            "cif": jsonData["userInfo"]["cif"],
                        }
                        print("Login successful")
                        return jsonData
                    print("Login failed")
                except Exception as e:
                    print(f"Error: {e}")
                return None


async def getTextFromImage(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            try:
                image_bytes = await response.read()
                base64_str = base64.b64encode(image_bytes).decode("ascii")
                mime_type = response.headers.get("Content-Type", "image/jpeg")
                data_uri = f"data:{mime_type};base64,{base64_str}"
                url = "https://www.jpgtotext.com/"
                response = await session.get(url)
                responseText = await response.text()
                csrf_token = re.search(
                    r'"X-CSRF-TOKEN"\s*:\s*"([^"]+)"', responseText
                ).group(1)
                set_cookie_list = response.headers.getall("Set-Cookie", [])
                cookie = ""
                if set_cookie_list:
                    for cookie_str in set_cookie_list:
                        cookie += f"{cookie_str};"
                url = f"https://www.jpgtotext.com/emd/captcha-verify/{datetime.now().timestamp()}"
                payload = {
                    "emd_captcha_1": "1Ux6aoGeookM8oIstJ5RAy4WV6MMvRSEh1lDKsLfIn",
                    "emd_captcha_2": None,
                    "emd_captcha_3": datetime.now().timestamp(),
                    "emd_is_tool_premium": 0,
                }
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-CSRF-TOKEN": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Cookie": cookie,
                }
                response = await session.post(url, headers=headers, data=payload)
                jsonData = await response.json()
                url = "https://www.jpgtotext.com/free-image-to-text"
                payload = {
                    "req_key": jsonData["req_key"],
                    "base64": data_uri,
                    "imgName": datetime.now(),
                    "dimension": None,
                    "fileSize": None,
                    "count": 0,
                    "e_track_key": "17697332588988h35wuqoba",
                    "parent_id": "1",
                    "tool_key": "jpg_to_text",
                }
                response = await session.post(url, headers=headers, data=payload)
                print(await response.json())
                captcha_value = "".join(
                    re.findall(r"\d+", (await response.json())["text"])
                )
                return captcha_value
            except Exception as e:
                print(f"Error: {e}")
                return None


async def getAccountList(public_key_base64=None, private_key_pem=None):
    if not public_key_base64 or not private_key_pem:
        public_key_base64 = DEFAULTS["public_key_base64"]
        private_key_pem = DEFAULTS["private_key_pem"]
    payload = encrypt_request(
        {**DEFAULT_PAYLOAD, "accountType": "ALL", "lang": "en", "mid": 8},
        public_key_base64,
        DEFAULTS["server_public_key_base64"],
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/v2/get-list-account-via-cif",
            headers=DEFAULTS["headers"],
            json=payload,
        ) as response:
            if response.status < 400:
                jsonData = decrypt_response(await response.json(), private_key_pem)
                if jsonData:
                    jsonData = json.loads(jsonData)
                    return jsonData["cards"]

        return None


async def transactionHistory(
    account_no,
    from_date=(datetime.now() - timedelta(days=7)).strftime("%d/%m/%Y"),
    to_date=datetime.now().strftime("%d/%m/%Y"),
    length_in_page=10,
    page_index=0,
    public_key_base64=None,
    private_key_pem=None,
):
    if not public_key_base64 or not private_key_pem:
        public_key_base64 = DEFAULTS["public_key_base64"]
        private_key_pem = DEFAULTS["private_key_pem"]
    payload = encrypt_request(
        {
            **DEFAULT_PAYLOAD,
            "accountNo": account_no,
            "fromDate": from_date,
            "lengthInPage": length_in_page,
            "mid": 14,
            "pageIndex": page_index,
            "toDate": to_date,
        },
        public_key_base64,
        DEFAULTS["server_public_key_base64"],
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/v1/transaction-history",
            headers=DEFAULTS["headers"],
            json=payload,
        ) as response:
            if response.status < 400:
                jsonData = decrypt_response(await response.json(), private_key_pem)
                if jsonData and "transactions" in str(jsonData):
                    jsonData = json.loads(jsonData)
                    return jsonData["transactions"]
        return None


async def test():
    response = await login("0386757425", "1!2@3#_Qwe")
    if response:
        cards = await getAccountList()
        if cards:
            for card in cards:
                transactions = await transactionHistory(card["cardAccount"])
                if transactions:
                    for trans in transactions:
                        print(
                            trans["tranDate"],
                            trans["CD"],
                            trans["Amount"],
                            trans["curCode"],
                            trans["Description"],
                        )


asyncio.run(test())
