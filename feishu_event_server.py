"""飞书 / Lark 事件回调 HTTP 服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from aiohttp import web

from feishu_client import FeishuClient

logger = logging.getLogger(__name__)

DEDUP_TTL_SECONDS = 3600

FeishuMessageHandler = Callable[
    [str, str, str, Optional[str], str, list[str]],
    Awaitable[None],
]

# 飞书回复时自带的内部 @ 占位符（如 @_user_1），不是 TG 用户名
_FEISHU_AT_PLACEHOLDER_RE = re.compile(r"@_user_\d+")


def strip_feishu_at_placeholders(text: str) -> str:
    cleaned = _FEISHU_AT_PLACEHOLDER_RE.sub("", text)
    return " ".join(cleaned.split())


def _parse_message_content(message: dict[str, Any]) -> tuple[str, list[str]]:
    raw_content = message.get("content", "")
    if not raw_content:
        return "", []

    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError:
        return strip_feishu_at_placeholders(raw_content.strip()), []

    msg_type = message.get("message_type") or message.get("msg_type") or "text"
    if msg_type == "text":
        return strip_feishu_at_placeholders(str(content.get("text", "")).strip()), []

    if msg_type == "image":
        image_key = str(content.get("image_key", "")).strip()
        return "", [image_key] if image_key else []

    if msg_type == "post":
        parts: list[str] = []
        image_keys: list[str] = []
        for block in content.get("content", []):
            for item in block:
                tag = item.get("tag")
                if tag == "text":
                    parts.append(item.get("text", ""))
                elif tag == "at":
                    # 飞书回复 @ 占位，TG 侧由自动 @ 原发言者处理
                    continue
                elif tag == "img":
                    image_key = str(item.get("image_key", "")).strip()
                    if image_key:
                        image_keys.append(image_key)
        text = "\n".join(part for part in parts if part).strip()
        return strip_feishu_at_placeholders(text), image_keys

    return "", []


class FeishuEventServer:
    def __init__(
        self,
        feishu: FeishuClient,
        *,
        group_chat_ids: set[str],
        dm_chat_id: str = "",
        verification_token: str = "",
        encrypt_key: str = "",
        allowed_open_ids: set[str] | None = None,
        on_message: FeishuMessageHandler | None = None,
        path: str = "/feishu/event",
    ) -> None:
        self.feishu = feishu
        self.group_chat_ids = group_chat_ids
        self.dm_chat_id = dm_chat_id.strip()
        self.verification_token = verification_token.strip()
        self.encrypt_key = encrypt_key.strip()
        self.allowed_open_ids = allowed_open_ids or set()
        self.on_message = on_message
        self.path = path if path.startswith("/") else f"/{path}"
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._processed_message_ids: dict[str, float] = {}

    def _is_duplicate_message(self, message_id: str) -> bool:
        if not message_id:
            return False

        now = time.time()
        expired = [
            mid
            for mid, ts in self._processed_message_ids.items()
            if now - ts > DEDUP_TTL_SECONDS
        ]
        for mid in expired:
            del self._processed_message_ids[mid]

        if message_id in self._processed_message_ids:
            return True

        self._processed_message_ids[message_id] = now
        return False

    def _is_watched_chat(self, chat_id: str) -> bool:
        if chat_id in self.group_chat_ids:
            return True
        return bool(self.dm_chat_id and chat_id == self.dm_chat_id)

    async def _handle_event(self, request: web.Request) -> web.Response:
        raw_body = await request.read()
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.json_response({"msg": "invalid json"}, status=400)

        if payload.get("encrypt") and self.encrypt_key:
            decrypted = self.feishu.decrypt_event(payload["encrypt"], self.encrypt_key)
            if not decrypted:
                return web.json_response({"msg": "decrypt failed"}, status=400)
            payload = decrypted

        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            logger.info("飞书事件订阅 URL 验证成功")
            return web.json_response({"challenge": challenge})

        header = payload.get("header", {})
        token = header.get("token", "")
        if self.verification_token and token != self.verification_token:
            logger.warning("飞书事件 token 校验失败")
            return web.json_response({"msg": "invalid token"}, status=403)

        event_type = header.get("event_type", "")
        if event_type == "im.message.receive_v1" and self.on_message:
            asyncio.create_task(
                self._process_message_event_safe(payload.get("event", {}))
            )

        return web.json_response({})

    async def _process_message_event_safe(self, event: dict[str, Any]) -> None:
        try:
            await self._handle_message_event(event)
        except Exception:
            logger.exception("处理飞书消息事件失败")

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        sender = event.get("sender", {})
        if sender.get("sender_type") == "bot":
            return

        sender_open_id = (sender.get("sender_id") or {}).get("open_id", "")
        if self.allowed_open_ids and sender_open_id not in self.allowed_open_ids:
            logger.info("跳过：飞书用户不在白名单 open_id=%s", sender_open_id)
            return

        message = event.get("message", {})
        feishu_chat_id = message.get("chat_id", "")
        if not self._is_watched_chat(feishu_chat_id):
            return

        parent_id = (message.get("parent_id") or message.get("root_id") or "").strip() or None
        message_id = str(message.get("message_id", "")).strip()
        if self._is_duplicate_message(message_id):
            logger.info("跳过：重复飞书消息 message_id=%s", message_id)
            return

        reply_text, image_keys = _parse_message_content(message)
        if not reply_text and not image_keys:
            logger.info("跳过：消息内容为空 chat_id=%s", feishu_chat_id)
            return

        logger.info(
            "飞书消息 -> TG chat=%s parent=%s sender=%s text=%r images=%d",
            feishu_chat_id,
            parent_id,
            sender_open_id,
            reply_text[:80],
            len(image_keys),
        )
        await self.on_message(
            feishu_chat_id,
            reply_text,
            sender_open_id,
            parent_id,
            message_id,
            image_keys,
        )

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_post(self.path, self._handle_event)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        logger.info("飞书事件服务已启动 http://%s:%s%s", host, port, self.path)

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
