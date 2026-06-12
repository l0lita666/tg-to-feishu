"""飞书 / Lark Webhook 与 Bot API 客户端。"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import tempfile
import time
import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from read_status import (
    ReadStatus,
    build_read_receipt_inner_column_set,
    format_read_suffix_html,
)

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 4000
CARD_BODY_BACKGROUND = "wathet-50"
CARD_BODY_PADDING = "10px 12px 10px 12px"
CARD_META_TIME_COLOR = "grey"
CARD_META_TITLE_COLOR = "green"
PREVIEW_MAX_LEN = 100
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tiff"}


def _is_media_placeholder(body: str) -> bool:
    text = body.strip()
    return bool(text) and text.startswith("[") and text.endswith("]")


def _extract_sender_label(info: str) -> str:
    """从 info（群名 · 发送者 或 私聊发送者）中提取发送者。"""
    info = info.strip()
    if " · " in info:
        return info.rsplit(" · ", 1)[-1].strip()
    return info


def _build_body_preview(body: str, image_count: int) -> str:
    """生成以正文为主的预览文字（通知/会话列表优先展示）。"""
    text = body.strip()
    if text and not _is_media_placeholder(text):
        preview = " ".join(text.split())
    elif image_count > 1:
        preview = f"[相册 {image_count} 张]"
    elif image_count == 1:
        preview = "[图片]"
    elif text:
        preview = text
    else:
        preview = "新消息"
    if len(preview) > PREVIEW_MAX_LEN:
        return preview[: PREVIEW_MAX_LEN - 3] + "..."
    return preview


def _build_notification_preview(info: str, body: str, image_count: int) -> str:
    """通知/会话列表预览：发送者 + 正文（飞书通常只展示 header title）。"""
    content = _build_body_preview(body, image_count)
    sender = _extract_sender_label(info)
    if not sender:
        return content
    prefix = f"{sender}: "
    max_content = PREVIEW_MAX_LEN - len(prefix)
    if max_content < 10:
        return sender[:PREVIEW_MAX_LEN]
    if len(content) > max_content:
        content = content[: max_content - 3] + "..."
    return prefix + content


def _build_card_config(
    info: str,
    body: str,
    image_count: int,
    *,
    updatable: bool = False,
) -> dict[str, Any]:
    """config.summary 供会话列表/通知预览；卡片正文不再依赖 header 重复展示。"""
    config: dict[str, Any] = {
        "summary": {
            "content": _build_notification_preview(info, body, image_count),
        },
    }
    if updatable:
        config["update_multi"] = True
    return config


def _pick_grid_rows(count: int) -> list[int]:
    """按张数拆成多行，每行列数（近似 TG 相册网格）。"""
    if count <= 0:
        return []
    if count == 1:
        return [1]
    if count == 2:
        return [2]
    if count == 3:
        return [3]
    if count == 4:
        return [2, 2]
    if count == 5:
        return [3, 2]
    if count == 6:
        return [3, 3]
    if count == 7:
        return [3, 2, 2]
    if count == 8:
        return [3, 3, 2]
    if count == 9:
        return [3, 3, 3]
    return [3, 3, 3, 1]


def _thumb_size_for_cols(cols: int) -> str:
    return {
        1: "220px 220px",
        2: "150px 150px",
        3: "100px 100px",
    }.get(cols, "100px 100px")


def _preview_img_element(img_key: str, alt: str, size: str) -> dict[str, Any]:
    return {
        "tag": "img",
        "img_key": img_key,
        "alt": {"tag": "plain_text", "content": alt},
        "preview": True,
        "scale_type": "crop_center",
        "size": size,
        "corner_radius": "4px",
    }


def _build_image_grid_elements(keys: list[str]) -> list[dict[str, Any]]:
    """缩略图网格，每张图支持点击放大预览。"""
    if not keys:
        return []
    if len(keys) == 1:
        return [_preview_img_element(keys[0], "图片", "220px 220px")]

    elements: list[dict[str, Any]] = []
    rows = _pick_grid_rows(len(keys))
    offset = 0
    for cols in rows:
        row_keys = keys[offset : offset + cols]
        offset += cols
        size = _thumb_size_for_cols(cols)
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "small",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [
                            _preview_img_element(
                                key,
                                f"图片 {offset - len(row_keys) + idx + 1}/{len(keys)}",
                                size,
                            )
                        ],
                    }
                    for idx, key in enumerate(row_keys)
                ],
            }
        )
    return elements


def _format_card_clock(time_str: str) -> str:
    """从存储的时间串提取 HH:MM，用于卡片右下角。"""
    text = time_str.strip()
    if not text:
        return ""
    clock = text.rsplit(" ", 1)[-1] if " " in text else text
    parts = clock.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return clock


def _meta_div(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
        },
    }


def _build_meta_content_html(
    info: str,
    *,
    read_suffix: str = "",
) -> str:
    """标题行：仅来源/发送者（绿色），已读后缀可选。"""
    parts: list[str] = []
    if info:
        parts.append(f"<font color='{CARD_META_TITLE_COLOR}'>{info}</font>")
    content = "".join(parts)
    if read_suffix:
        content = f"{content} {read_suffix}" if content else read_suffix
    return content or " "


def _build_meta_row(
    info: str,
    read_status: ReadStatus | None,
    *,
    is_outgoing: bool,
    is_group: bool,
) -> dict[str, Any]:
    read_suffix = format_read_suffix_html(
        read_status,
        is_outgoing=is_outgoing,
        is_group=is_group,
    )
    content = _build_meta_content_html(info, read_suffix=read_suffix)
    return _meta_div(content)


def _build_card_clock_element(clock: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": clock,
            "text_size": "notation",
            "text_color": CARD_META_TIME_COLOR,
            "text_align": "left",
        },
    }


def _build_card_footer_row(
    time_str: str,
    read_status: ReadStatus | None,
    *,
    is_outgoing: bool,
    is_group: bool,
) -> dict[str, Any] | None:
    """底栏：左下角始终显示发送时间；发出消息右侧显示已读（群聊头像 / 私聊 ✓）。"""
    clock = _format_card_clock(time_str)
    receipt = build_read_receipt_inner_column_set(
        read_status,
        is_outgoing=is_outgoing,
        is_group=is_group,
    )
    if not clock and not receipt:
        return None

    columns: list[dict[str, Any]] = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "bottom",
            "horizontal_align": "left",
            "elements": [_build_card_clock_element(clock)] if clock else [],
        },
    ]
    if receipt:
        columns.append(
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "bottom",
                "horizontal_align": "right",
                "elements": [receipt],
            }
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "vertical_align": "bottom",
        "columns": columns,
    }


def _body_div(body: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": body,
            "text_color": "default",
        },
    }


def _build_read_action_button() -> dict[str, Any]:
    """飞书→TG 已读：消息右侧蓝色 Rd 按钮（与发出灰色 ✓ 区分）。"""
    return {
        "tag": "button",
        "text": {
            "tag": "plain_text",
            "content": "Rd",
            "text_size": "notation",
        },
        "type": "primary_text",
        "size": "tiny",
        "width": "default",
        "behaviors": [
            {
                "type": "callback",
                "value": {"action": "read_one"},
            }
        ],
    }


def _wrap_content_with_read_button(
    content_elements: list[dict[str, Any]],
    *,
    show_read_button: bool,
) -> list[dict[str, Any]]:
    """将消息内容与右侧已读按钮并排（按钮仅在入站未读时出现）。"""
    if not content_elements:
        return []
    if not show_read_button:
        return content_elements
    return [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "small",
            "horizontal_align": "left",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": content_elements,
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "vertical_align": "top",
                    "horizontal_align": "right",
                    "elements": [_build_read_action_button()],
                },
            ],
        }
    ]


class FeishuClient:
    def __init__(
        self,
        webhook_url: str,
        app_id: str = "",
        app_secret: str = "",
        api_base: str = "",
        timeout: float = 30.0,
        *,
        api_min_interval: float = 0.25,
    ) -> None:
        self.webhook_url = webhook_url
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.timeout = timeout
        self.api_base = api_base.strip() or self._detect_api_base(webhook_url)
        self._token = ""
        self._token_expire_at = 0.0
        self._api_min_interval = max(api_min_interval, 0.05)
        self._api_lock = asyncio.Lock()
        self._last_api_at = 0.0
        self._rate_limit_until = 0.0

    @property
    def api_rate_limited(self) -> bool:
        return time.monotonic() < self._rate_limit_until

    async def _wait_api_slot(self) -> None:
        async with self._api_lock:
            now = time.monotonic()
            if now < self._rate_limit_until:
                await asyncio.sleep(self._rate_limit_until - now)
                now = time.monotonic()
            elapsed = now - self._last_api_at
            if elapsed < self._api_min_interval:
                await asyncio.sleep(self._api_min_interval - elapsed)
            self._last_api_at = time.monotonic()

    def _extend_rate_limit_cooldown(self, response: httpx.Response, *, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after:
            with contextlib.suppress(ValueError):
                wait = float(retry_after)
                self._rate_limit_until = max(
                    self._rate_limit_until,
                    time.monotonic() + wait,
                )
                return wait
        wait = max(60.0, min(120.0, 5.0 * (attempt + 1)))
        self._rate_limit_until = max(
            self._rate_limit_until,
            time.monotonic() + wait,
        )
        return wait

    async def _api_request(
        self,
        method: str,
        url: str,
        *,
        max_retries: int = 4,
        optional: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if optional and self.api_rate_limited:
            raise httpx.HTTPStatusError(
                "rate limited (optional request skipped)",
                request=httpx.Request(method, url),
                response=httpx.Response(429, request=httpx.Request(method, url)),
            )

        attempts = 1 if optional else max_retries
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self._wait_api_slot()
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(method, url, **kwargs)
                if response.status_code == 429:
                    wait = self._extend_rate_limit_cooldown(response, attempt=attempt)
                    if optional:
                        logger.warning(
                            "飞书 API 限流 %s %s（可选请求，不重试）",
                            method,
                            url,
                        )
                        response.raise_for_status()
                    logger.warning(
                        "飞书 API 限流 %s %s，%.1fs 后重试 (%d/%d)",
                        method,
                        url,
                        wait,
                        attempt + 1,
                        attempts,
                    )
                    if attempt >= attempts - 1:
                        response.raise_for_status()
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code == 429:
                    if optional:
                        raise
                    if attempt < attempts - 1:
                        wait = self._extend_rate_limit_cooldown(
                            exc.response,
                            attempt=attempt,
                        )
                        await asyncio.sleep(wait)
                        continue
                raise
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt >= attempts - 1:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise RuntimeError(f"飞书 API 请求失败: {method} {url}")

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
            response = await self._api_request(
                "POST",
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
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

    def _write_image_bytes(self, image_key: str, data: bytes) -> Path | None:
        if not data:
            return None
        tmp_dir = Path(tempfile.gettempdir()) / "feishu-tg-media"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        suffix = self._guess_image_suffix(data)
        file_path = tmp_dir / f"{image_key}{suffix}"
        file_path.write_bytes(data)
        logger.info("飞书图片已下载: %s (%d bytes)", file_path.name, len(data))
        return file_path

    async def download_message_image(self, message_id: str, image_key: str) -> Path | None:
        """下载飞书用户消息中的图片资源（飞书→TG 场景）。"""
        if not message_id or not image_key:
            return None

        token = await self._get_tenant_token()
        if not token:
            return None

        url = f"{self.api_base}/im/v1/messages/{message_id}/resources/{image_key}"
        try:
            response = await self._api_request(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"type": "image"},
            )
            return self._write_image_bytes(image_key, response.content)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning(
                    "下载飞书消息图片被限流 message_id=%s key=%s",
                    message_id,
                    image_key,
                )
            else:
                logger.exception(
                    "下载飞书消息图片失败 message_id=%s key=%s", message_id, image_key
                )
            return None
        except Exception:
            logger.exception(
                "下载飞书消息图片失败 message_id=%s key=%s", message_id, image_key
            )
            return None

    async def upload_image(self, file_path: Path, *, optional: bool = False) -> str | None:
        """上传图片到 Lark，返回 image_key。optional=True 时遇 429 立即放弃（用于头像等非关键资源）。"""
        if file_path.stat().st_size > 10 * 1024 * 1024:
            logger.error("图片超过 10MB 上限，无法上传: %s", file_path.name)
            return None

        token = await self._get_tenant_token()
        if not token:
            return None

        url = f"{self.api_base}/im/v1/images"
        try:
            with file_path.open("rb") as fp:
                response = await self._api_request(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (file_path.name, fp)},
                    optional=optional,
                    max_retries=1 if optional else 4,
                )
            data = response.json()
            if data.get("code", 0) != 0:
                logger.error("上传图片失败: %s", data)
                return None
            image_key = data.get("data", {}).get("image_key")
            if image_key:
                logger.info("图片已上传 Lark: %s", file_path.name)
            return image_key
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                if optional:
                    logger.debug("上传图片被限流（可选）: %s", file_path.name)
                else:
                    logger.warning("上传图片被限流: %s", file_path.name)
            else:
                logger.exception("上传图片异常: %s", file_path)
            return None
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
        *,
        is_outgoing: bool = False,
        is_group: bool = False,
        read_status: ReadStatus | None = None,
        updatable: bool = False,
        show_read_buttons: bool = False,
    ) -> dict[str, Any]:
        if len(body) > MAX_TEXT_LEN:
            body = body[: MAX_TEXT_LEN - 3] + "..."

        keys = list(image_keys or [])
        if image_key and image_key not in keys:
            keys.insert(0, image_key)

        meta = _build_meta_row(
            info,
            read_status,
            is_outgoing=is_outgoing,
            is_group=is_group,
        )
        body_div = _body_div(body) if body else None

        content_elements: list[dict[str, Any]] = [meta]
        if body_div:
            content_elements.append(body_div)

        elements: list[dict[str, Any]] = []
        if avatar_key:
            wrapped = _wrap_content_with_read_button(
                content_elements,
                show_read_button=show_read_buttons and not is_outgoing,
            )
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "none",
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
                                        "content": _build_notification_preview(
                                            info, body, len(keys)
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
                            "elements": wrapped,
                        },
                    ],
                }
            )
        else:
            elements.extend(
                _wrap_content_with_read_button(
                    content_elements,
                    show_read_button=show_read_buttons and not is_outgoing,
                )
            )

        elements.extend(_build_image_grid_elements(keys))
        footer = _build_card_footer_row(
            time_str,
            read_status,
            is_outgoing=is_outgoing,
            is_group=is_group,
        )
        if footer:
            elements.append(footer)

        card: dict[str, Any] = {
            "schema": "2.0",
            "config": _build_card_config(
                info,
                body,
                len(keys),
                updatable=updatable,
            ),
            "body": {
                "direction": "vertical",
                "padding": "0",
                "vertical_spacing": "0",
                "elements": [
                    {
                        "tag": "column_set",
                        "flex_mode": "none",
                        "margin": "0",
                        "background_style": CARD_BODY_BACKGROUND,
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "padding": CARD_BODY_PADDING,
                                "vertical_align": "top",
                                "elements": elements,
                            }
                        ],
                    }
                ],
            },
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
        is_outgoing: bool = False,
        is_group: bool = False,
        read_status: ReadStatus | None = None,
        updatable: bool = False,
        show_read_buttons: bool = False,
    ) -> tuple[bool, str | None]:
        """发送紧凑卡片，返回 (是否成功, 飞书 message_id)。"""
        card = self.build_compact_card(
            time_str,
            info,
            body,
            image_key,
            image_keys,
            avatar_key=avatar_key,
            is_outgoing=is_outgoing,
            is_group=is_group,
            read_status=read_status,
            updatable=updatable,
            show_read_buttons=show_read_buttons,
        )
        keys = list(image_keys or [])
        if image_key and image_key not in keys:
            keys.insert(0, image_key)
        meta_line = f"{time_str} · {info}" if info else time_str

        if prefer_bot and target_chat_id and self.bot_api_enabled:
            message_id = await self._send_bot_card(card, target_chat_id)
            if message_id:
                return True, message_id
            return False, None

        if not prefer_bot:
            payload: dict[str, Any] = {
                "msg_type": "interactive",
                "card": card,
            }
            if await self._post_webhook(payload):
                return True, None

        text_ok = await self.send_text("", meta_line + (f"\n{body}" if body else ""))
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
            response = await self._api_request(
                "POST",
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                max_retries=2,
            )
            data = response.json()
            if data.get("code", 0) != 0:
                logger.error("Bot 发送卡片失败: %s", data)
                return None
            message_id = data.get("data", {}).get("message_id")
            if message_id:
                logger.info("Bot 卡片已发送 message_id=%s", message_id)
            return message_id
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("Bot 发送卡片被限流 chat_id=%s", target_chat_id)
            else:
                logger.exception("Bot 发送卡片异常")
            return None
        except Exception:
            logger.exception("Bot 发送卡片异常")
            return None

    async def patch_message_card(self, message_id: str, card: dict[str, Any]) -> bool:
        """通过 message_id 更新已发送的卡片（需 config.update_multi=true）。"""
        if not message_id:
            return False
        if self.api_rate_limited:
            return False

        token = await self._get_tenant_token()
        if not token:
            return False

        url = f"{self.api_base}/im/v1/messages/{message_id}"
        payload = {"content": json.dumps(card, ensure_ascii=False)}
        try:
            response = await self._api_request(
                "PATCH",
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                optional=True,
                max_retries=1,
            )
            data = response.json()
            if data.get("code", 0) != 0:
                logger.error("更新消息卡片失败: %s", data)
                return False
            logger.info("消息卡片已更新 message_id=%s", message_id)
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("更新消息卡片被限流 message_id=%s", message_id)
            else:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = exc.response.text[:500]
                logger.error(
                    "更新消息卡片失败 message_id=%s status=%s body=%s",
                    message_id,
                    exc.response.status_code,
                    detail,
                )
            return False
        except Exception:
            logger.exception("更新消息卡片异常 message_id=%s", message_id)
            return False

    async def delay_update_message_card(
        self,
        callback_token: str,
        card: dict[str, Any],
        *,
        open_ids: list[str] | None = None,
    ) -> bool:
        """用 card.action.trigger 回调 token 延时更新卡片（需先响应空 JSON）。"""
        if not callback_token:
            return False
        if self.api_rate_limited:
            return False

        tenant_token = await self._get_tenant_token()
        if not tenant_token:
            return False

        card_payload: dict[str, Any] = {
            "type": "raw",
            "data": card,
        }
        if open_ids:
            card_payload["open_ids"] = open_ids

        url = f"{self.api_base}/interactive/v1/card/update"
        try:
            response = await self._api_request(
                "POST",
                url,
                headers={"Authorization": f"Bearer {tenant_token}"},
                json={"token": callback_token, "card": card_payload},
                optional=True,
                max_retries=1,
            )
            data = response.json()
            if data.get("code", 0) != 0:
                logger.error("延时更新卡片失败: %s", data)
                return False
            return True
        except httpx.HTTPStatusError as exc:
            detail = ""
            with contextlib.suppress(Exception):
                detail = exc.response.text[:500]
            logger.error(
                "延时更新卡片失败 status=%s body=%s",
                exc.response.status_code,
                detail,
            )
            return False
        except Exception:
            logger.exception("延时更新卡片异常")
            return False

    async def get_message_read_users(
        self,
        message_id: str,
    ) -> list[tuple[str, float]]:
        """查询飞书消息已读用户，返回 [(open_id, read_ts_seconds), ...]。"""
        if not message_id:
            return []

        token = await self._get_tenant_token()
        if not token:
            return []

        url = f"{self.api_base}/im/v1/messages/{message_id}/read_users"
        readers: list[tuple[str, float]] = []
        page_token = ""
        try:
            while True:
                params: dict[str, str | int] = {
                    "user_id_type": "open_id",
                    "page_size": 50,
                }
                if page_token:
                    params["page_token"] = page_token
                response = await self._api_request(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                data = response.json()
                if data.get("code", 0) != 0:
                    logger.warning("查询飞书已读失败 message_id=%s data=%s", message_id, data)
                    break
                payload = data.get("data") or {}
                for item in payload.get("items") or []:
                    open_id = str(item.get("user_id", "")).strip()
                    if not open_id:
                        continue
                    try:
                        read_ts = int(item.get("timestamp", 0)) / 1000.0
                    except (TypeError, ValueError):
                        read_ts = 0.0
                    readers.append((open_id, read_ts))
                if not payload.get("has_more"):
                    break
                page_token = str(payload.get("page_token", "")).strip()
                if not page_token:
                    break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("查询飞书已读被限流 message_id=%s", message_id)
            else:
                logger.exception("查询飞书已读异常 message_id=%s", message_id)
        except Exception:
            logger.exception("查询飞书已读异常 message_id=%s", message_id)
        return readers

    async def recall_message(self, message_id: str) -> bool:
        """撤回指定飞书消息。机器人仅能撤回自己发的消息，或作为群主/管理员撤回他人消息。"""
        if not message_id or not self.bot_api_enabled:
            return False

        token = await self._get_tenant_token()
        if not token:
            return False

        url = f"{self.api_base}/im/v1/messages/{message_id}"
        try:
            response = await self._api_request(
                "DELETE",
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            data = response.json()
            if data.get("code", 0) != 0:
                logger.info(
                    "撤回飞书消息失败 message_id=%s code=%s msg=%s",
                    message_id,
                    data.get("code"),
                    data.get("msg"),
                )
                return False
            logger.info("飞书消息已撤回 message_id=%s", message_id)
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("撤回飞书消息被限流 message_id=%s", message_id)
            else:
                logger.warning(
                    "撤回飞书消息失败 message_id=%s err=%s",
                    message_id,
                    exc,
                )
            return False
        except Exception:
            logger.exception("撤回飞书消息异常 message_id=%s", message_id)
            return False

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
            response = await self._api_request(
                "POST",
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            data = response.json()
            return data.get("code", 0) == 0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("Bot 发送文本被限流 chat_id=%s", target_chat_id)
            else:
                logger.exception("Bot 发送文本失败")
            return False
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
