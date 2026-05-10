"""
WeChat iLink API wrapper.

Handles authentication, message polling, and sending via Tencent's iLink protocol.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct

import httpx

from .config import ILINK_BASE_URL

logger = logging.getLogger(__name__)


class ILinkAPI:
    """Stateful client for the iLink bot API."""

    def __init__(self, bot_token: str, cursor: str = ""):
        self.base_url = ILINK_BASE_URL
        self.bot_token = bot_token
        self.cursor = cursor
        self._client = httpx.AsyncClient(timeout=40.0)  # > 35s long-poll timeout

    def _headers(self) -> dict[str, str]:
        """Generate per-request headers with random UIN for replay protection."""
        uin = base64.b64encode(
            str(struct.unpack("I", os.urandom(4))[0]).encode()
        ).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.bot_token}",
            "X-WECHAT-UIN": uin,
        }

    async def get_updates(self) -> list[dict]:
        """Long-poll for incoming messages. Updates cursor on success."""
        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/getupdates",
            headers=self._headers(),
            json={
                "get_updates_buf": self.cursor,
                "base_info": {"channel_version": "1.0.2"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # iLink may return ret=0 or omit ret entirely; both are valid
        ret = data.get("ret")
        if ret is not None and ret != 0:
            logger.warning("iLink get_updates ret=%s: %s", ret, data)
            return []
        new_cursor = data.get("get_updates_buf") or data.get("sync_buf")
        if new_cursor:
            self.cursor = new_cursor
        return data.get("msgs", [])

    def _make_client_id(self) -> str:
        """Generate a unique client_id for message deduplication."""
        import time
        ts = int(time.time() * 1000)
        rnd = os.urandom(4).hex()
        return f"demo-wx-{ts}-{rnd}"

    async def send_text(self, to_user_id: str, text: str, context_token: str) -> None:
        """Send a text reply."""
        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            headers=self._headers(),
            json={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": self._make_client_id(),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": {"channel_version": "0.1.0"},
            },
        )
        logger.info("iLink sendmessage response: status=%s body=%s",
                    resp.status_code, resp.text[:500])
        resp.raise_for_status()

    async def send_typing(self, ticket: str) -> None:
        """Send typing indicator."""
        await self._client.post(
            f"{self.base_url}/ilink/bot/sendtyping",
            headers=self._headers(),
            json={"typing_ticket": ticket},
        )

    async def get_config(self) -> dict:
        """Get bot config (including typing ticket)."""
        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/getconfig",
            headers=self._headers(),
            json={},
        )
        resp.raise_for_status()
        return resp.json()

    # ── AES-128-ECB encryption helpers ──

    @staticmethod
    def _aes_encrypt(data: bytes, key: bytes) -> bytes:
        """Encrypt data with AES-128-ECB, PKCS7 padding."""
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(pad(data, AES.block_size))

    @staticmethod
    def _aes_decrypt(data: bytes, key: bytes) -> bytes:
        """Decrypt data with AES-128-ECB, PKCS7 unpadding."""
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        cipher = AES.new(key, AES.MODE_ECB)
        return unpad(cipher.decrypt(data), AES.block_size)

    CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

    async def download_and_decrypt(self, cdn_url: str, aes_key_b64: str) -> bytes:
        """Download an encrypted file from CDN and decrypt it."""
        key = base64.b64decode(aes_key_b64)
        resp = await self._client.get(cdn_url, timeout=30.0)
        resp.raise_for_status()
        return self._aes_decrypt(resp.content, key)

    async def download_media(self, media_item: dict) -> bytes:
        """Download and decrypt a media item (image/voice/file).

        Accepts the full item dict (e.g. image_item) which contains:
        - aeskey: hex string (preferred)
        - media.encrypt_query_param: CDN download token
        - media.aes_key: base64 of hex key (fallback)
        """
        import binascii
        from urllib.parse import quote

        media = media_item.get("media") or {}
        encrypt_param = media.get("encrypt_query_param", "")
        if not encrypt_param:
            raise ValueError("No encrypt_query_param in media item")

        # Parse AES key: prefer aeskey (hex), fallback to media.aes_key (base64 of hex)
        hex_key = media_item.get("aeskey", "")
        if hex_key:
            key = binascii.unhexlify(hex_key)
        else:
            b64_key = media.get("aes_key", "")
            if not b64_key:
                raise ValueError("No AES key found in media item")
            decoded = base64.b64decode(b64_key)
            if len(decoded) == 16:
                key = decoded
            else:
                # base64(hex_string) → hex string → raw bytes
                key = binascii.unhexlify(decoded.decode("ascii"))

        url = f"{self.CDN_BASE_URL}/download?encrypted_query_param={quote(encrypt_param)}"
        for attempt in range(2):
            try:
                resp = await self._client.get(url, timeout=60.0)
                resp.raise_for_status()
                return self._aes_decrypt(resp.content, key)
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(2)
                else:
                    raise

    async def _encrypt_and_upload(
        self, data: bytes, media_type: int, to_user_id: str,
    ) -> tuple[str, str, int]:
        """Encrypt and upload to CDN via iLink protocol.

        Args:
            data: raw file bytes
            media_type: 1=image, 2=video, 3=file, 4=voice
            to_user_id: target user

        Returns:
            (encrypt_query_param, aes_key_hex, encrypted_size)
        """
        import hashlib
        from math import ceil
        from urllib.parse import quote

        key = os.urandom(16)
        aes_key_hex = key.hex()
        encrypted = self._aes_encrypt(data, key)
        filekey = os.urandom(16).hex()
        raw_md5 = hashlib.md5(data).hexdigest()

        # Step 1: getuploadurl
        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/getuploadurl",
            headers=self._headers(),
            json={
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": len(data),
                "rawfilemd5": raw_md5,
                "filesize": len(encrypted),
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
                "base_info": {"channel_version": "1.0.2"},
            },
        )
        resp.raise_for_status()
        upload_data = resp.json()
        upload_param = upload_data.get("upload_param", "")
        if not upload_param:
            raise ValueError(f"getuploadurl returned no upload_param: {upload_data}")

        # Step 2: POST encrypted file to CDN
        upload_url = (
            f"{self.CDN_BASE_URL}/upload"
            f"?encrypted_query_param={quote(upload_param)}"
            f"&filekey={filekey}"
        )
        cdn_resp = await self._client.post(
            upload_url,
            content=encrypted,
            headers={"Content-Type": "application/octet-stream"},
            timeout=60.0,
        )
        cdn_resp.raise_for_status()

        # The download param comes from response header
        encrypt_query_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not encrypt_query_param:
            raise ValueError("CDN upload returned no x-encrypted-param header")

        return encrypt_query_param, aes_key_hex, len(encrypted)

    async def send_image(
        self, to_user_id: str, image_bytes: bytes, context_token: str
    ) -> None:
        """Send an image via CDN upload."""
        encrypt_param, aes_key_hex, enc_size = await self._encrypt_and_upload(
            image_bytes, 1, to_user_id,
        )
        # aes_key for sendmessage: base64 of hex string
        aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()

        await self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            headers=self._headers(),
            json={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": self._make_client_id(),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": encrypt_param,
                                "aes_key": aes_key_b64,
                                "encrypt_type": 1,
                            },
                            "mid_size": enc_size,
                        },
                    }],
                },
                "base_info": {"channel_version": "1.0.2"},
            },
        )

    async def send_file(
        self, to_user_id: str, file_bytes: bytes, filename: str, context_token: str
    ) -> None:
        """Send a file attachment via CDN upload."""
        encrypt_param, aes_key_hex, enc_size = await self._encrypt_and_upload(
            file_bytes, 3, to_user_id,
        )
        aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()

        await self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            headers=self._headers(),
            json={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": self._make_client_id(),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{
                        "type": 4,
                        "file_item": {
                            "media": {
                                "encrypt_query_param": encrypt_param,
                                "aes_key": aes_key_b64,
                                "encrypt_type": 1,
                            },
                            "file_name": filename,
                            "len": str(len(file_bytes)),
                        },
                    }],
                },
                "base_info": {"channel_version": "1.0.2"},
            },
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
