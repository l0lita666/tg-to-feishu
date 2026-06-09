"""飞书 / Lark Webhook 消息发送客户端。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 4000
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tiff"}


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

    async def send_compact(
        self,
        time_str: str,
        info: str,
        body: str,
        image_key: str | None = None,
        image_keys: list[str] | None = None,
    ) -> bool:
        """发送紧凑卡片：灰色小字 meta + 可选图片（支持多张）+ 正文。"""
        if len(body) > MAX_TEXT_LEN:
            body = body[: MAX_TEXT_LEN - 3] + "..."

        keys = list(image_keys or [])
        if image_key and image_key not in keys:
            keys.insert(0, image_key)

        meta_line = f"{time_str} · {info}" if info else time_str
        elements: list[dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"<font color='grey'>{meta_line}</font>",
                },
            },
        ]

        for idx, key in enumerate(keys, start=1):
            alt = "图片" if len(keys) == 1 else f"图片 {idx}/{len(keys)}"
            elements.append(
                {
                    "tag": "img",
                    "img_key": key,
                    "alt": {"tag": "plain_text", "content": alt},
                }
            )

        if body:
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": body},
                }
            )

        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "elements": elements,
            },
        }
        if await self._post(payload):
            return True

        # 卡片失败时降级：先发文字，再逐张发图
        text_ok = await self.send_text("", meta_line + (f"\n{body}" if body else ""))
        images_ok = all(await self.send_image(key) for key in keys) if keys else True
        if text_ok and images_ok:
            return True

        fallback = meta_line
        if body:
            fallback = f"{fallback}\n{body}"
        if keys:
            fallback = f"{fallback}\n[含 {len(keys)} 张图片，卡片发送失败]"
        return await self.send_text("", fallback)

    async def send_image(self, image_key: str) -> bool:
        """单独发送图片消息。"""
        payload: dict[str, Any] = {
            "msg_type": "image",
            "content": {"image_key": image_key},
        }
        return await self._post(payload)

    async def send_text(self, title: str, content: str) -> bool:
        """发送纯文本消息。"""
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
        return await self._post(payload)

    async def _post(self, payload: dict[str, Any]) -> bool:
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
