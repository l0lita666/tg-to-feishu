"""飞书 / Lark Webhook 与 Bot API 客户端。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 4000
PREVIEW_MAX_LEN = 100
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tiff"}


def _build_card_preview(body: str, image_count: int, meta_line: str) -> str:
    """生成会话列表中的卡片摘要（点开前的预览文字）。"""
    text = body.strip()
    if text:
        preview = " ".join(text.split())
        if len(preview) > PREVIEW_MAX_LEN:
            return preview[: PREVIEW_MAX_LEN - 3] + "..."
        return preview
    if image_count > 1:
        return f"[相册 {image_count} 张]"
    if image_count == 1:
        return "[图片]"
    return meta_line or "新消息"


class FeishuClient:
    def __init__(
        self,
        webhook_url: str,
        app_id: str = "",
        app_secret: str = "",
        api_base: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.timeout = timeout
        self.api_base = api_base.strip() or self._detect_api_base(webhook_url)
        self._token = ""
        self._token_expire_at = 0.0

    @property
    def media_enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def bot_api_enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @staticmethod
    def _detect_api_base(webhook_url: str) -> str:
        host = urlparse(webhook_url).netloc
        if "larksuite.com" in host:
            return "https://open.larksuite.com/open-apis"
        return "https://open.feishu.cn/open-apis"

    async def _get_tenant_token(self) -> str | None:
        if not self.media_enabled:
            return None
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token

        url = f"{self.api_base}/auth/v3/tenant_access_token/internal"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code", 0) != 0:
                    logger.error("获取 Lark token 失败: %s", data)
                    return None
                self._token = data["tenant_access_token"]
                self._token_expire_at = time.time() + int(data.get("expire", 7200))
                return self._token
        except Exception:
            logger.exception("获取 Lark token 异常")
            return None

    @staticmethod
    def _guess_image_suffix(data: bytes) -> str:
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if len(data) >= 4 and data[:4] == b"\x89PNG":
            return ".png"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if len(data) >= 3 and data[:3] == b"GIF":
            return ".gif"
        return ".jpg"

    async def download_message_image(self, message_id: str, image_key: str) -> Path | None:
        """下载飞书消息中的图片，返回本地临时文件路径。"""
        if not message_id or not image_key:
            return None

        token = await self._get_tenant_token()
        if not token:
            return None

        url = (
            f"{self.api_base}/im/v1/messages/{message_id}/resources/{image_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"type": "image"},
                )
                response.raise_for_status()
                data = response.content
                if not data:
                    logger.error("下载飞书图片为空 message_id=%s key=%s", message_id, image_key)
                    return None

                tmp_dir = Path(tempfile.gettempdir()) / "feishu-tg-media"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                suffix = self._guess_image_suffix(data)
                file_path = tmp_dir / f"{image_key}{suffix}"
                file_path.write_bytes(data)
                logger.info("飞书图片已下载: %s (%d bytes)", file_path.name, len(data))
                return file_path
        except Exception:
            logger.exception(
                "下载飞书图片失败 message_id=%s key=%s", message_id, image_key
            )
            return None

    async def upload_image(self, file_path: Path) -> str | None:
        """上传图片到 Lark，返回 image_key。"""
        if file_path.stat().st_size > 10 * 1024 * 1024:
            logger.error("图片超过 10MB 上限，无法上传: %s", file_path.name)
            return None

        token = await self._get_tenant_token()
        if not token:
            return None

        url = f"{self.api_base}/im/v1/images"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                with file_path.open("rb") as fp:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data={"image_type": "message"},
                        files={"image": (file_path.name, fp)},
                    )
                response.raise_for_status()
                data = response.json()
                if data.get("code", 0) != 0:
                    logger.error("上传图片失败: %s", data)
                    return None
                image_key = data.get("data", {}).get("image_key")
                if image_key:
                    logger.info("图片已上传 Lark: %s", file_path.name)
                return image_key
        except Exception:
            logger.exception("上传图片异常: %s", file_path)
            return None

    def build_compact_card(
        self,
        time_str: str,
        info: str,
        body: str,
        image_key: str | None = None,
        image_keys: list[str] | None = None,
        avatar_key: str | None = None,
    ) -> dict[str, Any]:
        if len(body) > MAX_TEXT_LEN:
            body = body[: MAX_TEXT_LEN - 3] + "..."

        keys = list(image_keys or [])
        if image_key and image_key not in keys:
            keys.insert(0, image_key)

        meta_line = f"{time_str} · {info}" if info else time_str
        meta_div: dict[str, Any] = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>{meta_line}</font>",
            },
        }
        body_div: dict[str, Any] | None = None
        if body:
            body_div = {
                "tag": "div",
                "text": {"tag": "plain_text", "content": body},
            }

        elements: list[dict[str, Any]] = []
        if avatar_key:
            right_column_elements = [meta_div]
            if body_div:
                right_column_elements.append(body_div)
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "vertical_align": "top",
                            "elements": [
                                {
                                    "tag": "img",
                                    "img_key": avatar_key,
                                    "alt": {
                                        "tag": "plain_text",
                                        "content": _build_card_preview(
                                            body, len(keys), meta_line
                                        ),
                                    },
                                    "preview": False,
                                    "scale_type": "crop_center",
                                    "size": "40px 40px",
                                    "corner_radius": "20px",
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "vertical_align": "top",
                            "elements": right_column_elements,
                        },
                    ],
                }
            )
        else:
            elements.append(meta_div)
            if body_div:
                elements.append(body_div)

        for idx, key in enumerate(keys, start=1):
            alt = "图片" if len(keys) == 1 else f"图片 {idx}/{len(keys)}"
            elements.append(
                {
                    "tag": "img",
                    "img_key": key,
                    "alt": {"tag": "plain_text", "content": alt},
                }
            )

        card: dict[str, Any] = {
            "config": {"wide_screen_mode": True},
            "elements": elements,
        }
        return card

    async def send_compact(
        self,
        time_str: str,
        info: str,
        body: str,
        image_key: str | None = None,
        image_keys: list[str] | None = None,
        *,
        avatar_key: str | None = None,
        prefer_bot: bool = False,
        target_chat_id: str = "",
    ) -> tuple[bool, str | None]:
        """发送紧凑卡片，返回 (是否成功, 飞书 message_id)。"""
        card = self.build_compact_card(
            time_str,
            info,
            body,
            image_key,
            image_keys,
            avatar_key=avatar_key,
        )
        meta_line = f"{time_str} · {info}" if info else time_str

        if prefer_bot and target_chat_id and self.bot_api_enabled:
            message_id = await self._send_bot_card(card, target_chat_id)
            if message_id:
                return True, message_id

        if not prefer_bot:
            payload: dict[str, Any] = {
                "msg_type": "interactive",
                "card": card,
            }
            if await self._post_webhook(payload):
                return True, None

        text_ok = await self.send_text("", meta_line + (f"\n{body}" if body else ""))
        keys = list(image_keys or [])
        if image_key and image_key not in keys:
            keys.insert(0, image_key)
        images_ok = all(await self.send_image(key) for key in keys) if keys else True
        if text_ok and images_ok:
            return True, None

        fallback = meta_line
        if body:
            fallback = f"{fallback}\n{body}"
        if keys:
            fallback = f"{fallback}\n[含 {len(keys)} 张图片，卡片发送失败]"
        ok = await self.send_text("", fallback)
        return ok, None

    async def _send_bot_card(self, card: dict[str, Any], target_chat_id: str) -> str | None:
        token = await self._get_tenant_token()
        if not token:
            return None

        url = f"{self.api_base}/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": target_chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code", 0) != 0:
                    logger.error("Bot 发送卡片失败: %s", data)
                    return None
                message_id = data.get("data", {}).get("message_id")
                if message_id:
                    logger.info("Bot 卡片已发送 message_id=%s", message_id)
                return message_id
        except Exception:
            logger.exception("Bot 发送卡片异常")
            return None

    async def send_bot_text(self, text: str, target_chat_id: str) -> bool:
        if not target_chat_id or not self.bot_api_enabled:
            return False

        token = await self._get_tenant_token()
        if not token:
            return False

        if len(text) > MAX_TEXT_LEN:
            text = text[: MAX_TEXT_LEN - 3] + "..."

        url = f"{self.api_base}/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": target_chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                return data.get("code", 0) == 0
        except Exception:
            logger.exception("Bot 发送文本失败")
            return False

    async def send_image(self, image_key: str) -> bool:
        """单独发送图片消息（Webhook）。"""
        payload: dict[str, Any] = {
            "msg_type": "image",
            "content": {"image_key": image_key},
        }
        return await self._post_webhook(payload)

    async def send_text(self, title: str, content: str) -> bool:
        """发送纯文本消息（Webhook）。"""
        if title:
            full_text = f"{title}\n\n{content}"
        else:
            full_text = content
        if len(full_text) > MAX_TEXT_LEN:
            full_text = full_text[: MAX_TEXT_LEN - 3] + "..."

        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": full_text},
        }
        return await self._post_webhook(payload)

    async def _post_webhook(self, payload: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.webhook_url, json=payload)
                response.raise_for_status()
                data = response.json()
                if data.get("code", data.get("StatusCode", 0)) not in (0, None):
                    logger.error("飞书返回错误: %s", data)
                    return False
                return True
        except Exception:
            logger.exception("发送飞书消息失败")
            return False

    @staticmethod
    def decrypt_event(encrypt_data: str, encrypt_key: str) -> dict[str, Any] | None:
        """解密飞书事件回调（Encrypt Key 非空时使用）。"""
        if not encrypt_key:
            return None
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            logger.error("解密飞书事件需要安装 cryptography: pip install cryptography")
            return None

        try:
            encrypted = base64.b64decode(encrypt_data)
            key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
            iv = encrypted[:16]
            ciphertext = encrypted[16:]
            cipher = Cipher(
                algorithms.AES(key),
                modes.CBC(iv),
                backend=default_backend(),
            )
            decryptor = cipher.decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            plain = unpadder.update(padded) + unpadder.finalize()
            return json.loads(plain.decode("utf-8"))
        except Exception:
            logger.exception("飞书事件解密失败")
            return None
