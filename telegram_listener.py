"""Telegram 消息监听并转发到飞书。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon import utils as tg_utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    FileMigrateError,
    FileReferenceExpiredError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.messages import (
    GetMessageReadParticipantsRequest,
    GetOutboxReadDateRequest,
)
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeSticker,
    InputUser,
    MessageEntityMentionName,
    MessageMediaDocument,
    MessageMediaPhoto,
    TypeMessageEntity,
    User,
)

from chat_map import invert_chat_map, lookup_feishu_chat, parse_feishu_chat_map
from feishu_client import FeishuClient, IMAGE_EXTENSIONS
from feishu_event_server import FeishuEventServer
from message_mapping import CardSnapshot, MessageMappingStore, TgMessageRef, tg_chat_id_variants
from log_config import forward_log, forward_skip, is_log_verbose, read_log, read_skip
from read_status import (
    ReadParticipant,
    ReadStatus,
    merge_read_participants,
    read_status_equal,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _is_peer_muted(notify_settings) -> bool:
    """判断会话是否处于静音状态（mute_until 在未来即为静音）。"""
    if notify_settings is None:
        return False
    mute_until = getattr(notify_settings, "mute_until", None)
    if not mute_until:
        return False
    now = datetime.now(timezone.utc)
    if mute_until.tzinfo is None:
        mute_until = mute_until.replace(tzinfo=timezone.utc)
    return mute_until > now


def parse_proxy(raw: str) -> tuple | None:
    """解析代理 URL，如 socks5://127.0.0.1:1080 或 socks5://user:pass@host:1080"""
    raw = raw.strip()
    if not raw:
        return None

    try:
        import socks
    except ImportError as exc:
        raise RuntimeError("使用代理需要安装 PySocks，请运行: pip install PySocks") from exc

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "socks5").lower()
    type_map = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
    }
    if scheme not in type_map:
        raise ValueError(f"不支持的代理类型: {scheme}，请用 socks5 / socks4 / http")

    host = parsed.hostname
    if not host:
        raise ValueError(f"代理地址无效: {raw}")

    default_ports = {"socks5": 1080, "socks4": 1080, "http": 8080}
    port = parsed.port or default_ports[scheme]

    if parsed.username:
        return (type_map[scheme], host, port, True, parsed.username, parsed.password or "")
    return (type_map[scheme], host, port)


def load_config() -> dict[str, str | int | list[str]]:
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(BASE_DIR / "config.example.env")

    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    phone = os.getenv("TELEGRAM_PHONE", "").strip()
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    feishu_app_id = os.getenv("FEISHU_APP_ID", "").strip()
    feishu_app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    feishu_api_base = os.getenv("FEISHU_API_BASE", "").strip()
    telegram_proxy = os.getenv("TELEGRAM_PROXY", "").strip()
    session_name = os.getenv("SESSION_NAME", "telegram_session").strip()
    reconnect_interval = int(os.getenv("RECONNECT_INTERVAL", "10"))
    log_level = os.getenv("LOG_LEVEL", "WARNING").upper()

    group_chats_raw = os.getenv("GROUP_CHATS", "").strip()
    group_chats = [item.strip() for item in group_chats_raw.split(",") if item.strip()]
    group_mode = os.getenv("GROUP_MODE", "manual").strip().lower()
    if group_mode not in ("manual", "unmuted", "manual_unmuted"):
        raise ValueError(
            "GROUP_MODE 仅支持 manual（手动指定群）、"
            "manual_unmuted（列表内且未静音）、unmuted（所有未静音群）"
        )
    group_refresh_interval = int(os.getenv("GROUP_REFRESH_INTERVAL", "300"))
    incoming_only = os.getenv("INCOMING_ONLY", "true").lower() in ("true", "1", "yes")

    feishu_reply_enabled = os.getenv("FEISHU_REPLY_ENABLED", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    feishu_chat_map_raw = os.getenv("FEISHU_CHAT_MAP", "").strip()
    feishu_chat_map = parse_feishu_chat_map(feishu_chat_map_raw)
    feishu_dm_chat_id = os.getenv("FEISHU_DM_CHAT_ID", "").strip()
    feishu_event_host = os.getenv("FEISHU_EVENT_HOST", "0.0.0.0").strip()
    feishu_event_port = int(os.getenv("FEISHU_EVENT_PORT", "8080"))
    feishu_event_path = os.getenv("FEISHU_EVENT_PATH", "/feishu/event").strip()
    feishu_verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip()
    feishu_encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "").strip()
    allowed_open_ids_raw = os.getenv("FEISHU_ALLOWED_OPEN_IDS", "").strip()
    allowed_open_ids = [
        item.strip() for item in allowed_open_ids_raw.split(",") if item.strip()
    ]
    read_sync_enabled = os.getenv("READ_SYNC_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    read_sync_dm_on_view = os.getenv("READ_SYNC_DM_ON_VIEW", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    read_sync_card_buttons = os.getenv("READ_SYNC_CARD_BUTTONS", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    read_poll_enabled = os.getenv("READ_POLL_ENABLED", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    read_watch_enabled = os.getenv("READ_WATCH_ENABLED", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    read_poll_interval = int(os.getenv("READ_POLL_INTERVAL", "15"))
    read_watch_interval = int(os.getenv("READ_WATCH_INTERVAL", "5"))
    read_poll_batch_size = int(os.getenv("READ_POLL_BATCH_SIZE", "8"))
    read_poll_outgoing_batch = int(os.getenv("READ_POLL_OUTGOING_BATCH", "5"))
    read_poll_startup_delay = int(os.getenv("READ_POLL_STARTUP_DELAY", "30"))
    feishu_api_min_interval = float(os.getenv("FEISHU_API_MIN_INTERVAL", "1.0"))
    feishu_avatar_enabled = os.getenv("FEISHU_AVATAR_ENABLED", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    log_verbose = os.getenv("LOG_VERBOSE", "false").lower() in ("true", "1", "yes")
    read_debug = os.getenv("READ_DEBUG", "true").lower() in ("true", "1", "yes")
    log_retention_days = int(os.getenv("LOG_RETENTION_DAYS", "3"))
    log_max_mb = int(os.getenv("LOG_MAX_MB", "5"))
    log_cleanup_interval = int(os.getenv("LOG_CLEANUP_INTERVAL_HOURS", "24"))
    feishu_recall_user_reply = os.getenv("FEISHU_RECALL_USER_REPLY", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    feishu_hide_outgoing_echo = os.getenv("FEISHU_HIDE_OUTGOING_ECHO", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    missing = []
    if not api_id:
        missing.append("TELEGRAM_API_ID")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not phone:
        missing.append("TELEGRAM_PHONE")
    if not webhook:
        missing.append("FEISHU_WEBHOOK_URL")

    if missing:
        raise ValueError(f"缺少必要配置: {', '.join(missing)}，请复制 config.example.env 为 .env 并填写")

    if feishu_reply_enabled:
        reply_missing = []
        if not feishu_app_id:
            reply_missing.append("FEISHU_APP_ID")
        if not feishu_app_secret:
            reply_missing.append("FEISHU_APP_SECRET")
        if not feishu_chat_map and not feishu_dm_chat_id:
            reply_missing.append("FEISHU_CHAT_MAP 或 FEISHU_DM_CHAT_ID")
        if not feishu_verification_token:
            reply_missing.append("FEISHU_VERIFICATION_TOKEN")
        if reply_missing:
            raise ValueError(
                f"启用飞书回复需配置: {', '.join(reply_missing)}"
            )

    if read_sync_enabled and not feishu_reply_enabled:
        raise ValueError(
            "READ_SYNC_ENABLED 需配合 FEISHU_REPLY_ENABLED=true（Bot 发卡片并获取 message_id）"
        )

    return {
        "api_id": int(api_id),
        "api_hash": api_hash,
        "phone": phone,
        "webhook": webhook,
        "feishu_app_id": feishu_app_id,
        "feishu_app_secret": feishu_app_secret,
        "feishu_api_base": feishu_api_base,
        "telegram_proxy": telegram_proxy,
        "session_name": session_name,
        "reconnect_interval": reconnect_interval,
        "log_level": log_level,
        "group_chats": group_chats,
        "group_mode": group_mode,
        "group_refresh_interval": group_refresh_interval,
        "incoming_only": incoming_only,
        "feishu_reply_enabled": feishu_reply_enabled,
        "feishu_chat_map": feishu_chat_map,
        "feishu_dm_chat_id": feishu_dm_chat_id,
        "feishu_event_host": feishu_event_host,
        "feishu_event_port": feishu_event_port,
        "feishu_event_path": feishu_event_path,
        "feishu_verification_token": feishu_verification_token,
        "feishu_encrypt_key": feishu_encrypt_key,
        "feishu_allowed_open_ids": allowed_open_ids,
        "read_sync_enabled": read_sync_enabled,
        "read_sync_dm_on_view": read_sync_dm_on_view,
        "read_sync_card_buttons": read_sync_card_buttons,
        "read_poll_enabled": read_poll_enabled,
        "read_watch_enabled": read_watch_enabled,
        "read_poll_interval": read_poll_interval,
        "read_watch_interval": read_watch_interval,
        "read_poll_batch_size": read_poll_batch_size,
        "read_poll_outgoing_batch": read_poll_outgoing_batch,
        "read_poll_startup_delay": read_poll_startup_delay,
        "feishu_api_min_interval": feishu_api_min_interval,
        "feishu_avatar_enabled": feishu_avatar_enabled,
        "feishu_recall_user_reply": feishu_recall_user_reply,
        "feishu_hide_outgoing_echo": feishu_hide_outgoing_echo,
        "log_verbose": log_verbose,
        "read_debug": read_debug,
        "log_retention_days": log_retention_days,
        "log_max_mb": log_max_mb,
        "log_cleanup_interval": log_cleanup_interval,
    }


def setup_logging_from_config(config: dict) -> None:
    from log_config import set_log_verbose, set_read_debug, setup_logging

    set_log_verbose(bool(config.get("log_verbose")))
    set_read_debug(bool(config.get("read_debug")))
    setup_logging(
        config.get("log_level", "WARNING"),
        max_mb=int(config.get("log_max_mb", 5)),
    )


def _extract_body(event: events.NewMessage.Event) -> tuple[str, str | None]:
    """提取消息正文和附件类型说明。"""
    msg = event.message
    # Telethon 多种文本字段，按优先级尝试
    text = ""
    for candidate in (
        getattr(event, "raw_text", None),
        getattr(msg, "text", None),
        getattr(msg, "message", None),
        getattr(event, "text", None),
    ):
        if candidate and str(candidate).strip():
            text = str(candidate).strip()
            break

    media_label: str | None = None
    if msg.media:
        if isinstance(msg.media, MessageMediaPhoto):
            media_label = "图片"
        elif isinstance(msg.media, MessageMediaDocument):
            doc = msg.media.document
            is_sticker = False
            if doc:
                for attr in (doc.attributes or []):
                    if isinstance(attr, DocumentAttributeSticker):
                        is_sticker = True
                        break
            media_label = "Sticker贴纸" if is_sticker else "文件"
        else:
            media_label = type(msg.media).__name__

    return text, media_label


def _user_display_name(user: User) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


async def _resolve_tg_user(client: TelegramClient, user_id: int) -> User | None:
    """解析 TG 用户（已读头像/首字），get_entity 失败时回退 GetUsers。"""
    if user_id <= 0:
        return None
    try:
        entity = await client.get_entity(user_id)
        if isinstance(entity, User):
            return entity
    except Exception:
        pass
    try:
        result = await client(GetUsersRequest(id=[InputUser(user_id, access_hash=0)]))
        for item in result.users:
            if isinstance(item, User) and item.id == user_id:
                return item
    except Exception:
        pass
    return None


def _sender_info_from_user(sender: object | None) -> tuple[int, str, str]:
    if not isinstance(sender, User):
        return 0, "", ""
    username = (sender.username or "").strip()
    return sender.id, username, _user_display_name(sender)


def _text_starts_with_mention(text: str, mention: str) -> bool:
    stripped = text.lstrip()
    if not mention:
        return False
    return stripped.lower().startswith(mention.lower())


def _build_reply_with_mention(
    reply_text: str,
    mapping: TgMessageRef,
) -> tuple[str, list[TypeMessageEntity] | None, int | None]:
    """根据原 TG 消息映射，生成带 @ 的回复文本与 reply_to。"""
    reply_to = mapping.tg_msg_id

    if mapping.tg_sender_username:
        mention = f"@{mapping.tg_sender_username}"
        if _text_starts_with_mention(reply_text, mention):
            return reply_text, None, reply_to
        combined = f"{mention} {reply_text}".strip() if reply_text else mention
        return combined, None, reply_to

    if mapping.tg_sender_id:
        mention_text = mapping.tg_sender_name or str(mapping.tg_sender_id)
        if _text_starts_with_mention(reply_text, mention_text):
            return reply_text, None, reply_to
        combined = f"{mention_text} {reply_text}".strip() if reply_text else mention_text
        entities: list[TypeMessageEntity] = [
            MessageEntityMentionName(
                offset=0,
                length=len(mention_text),
                user_id=mapping.tg_sender_id,
            )
        ]
        return combined, entities, reply_to

    return reply_text, None, reply_to


def _format_msg_time(event: events.NewMessage.Event) -> str:
    msg_date = event.message.date
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    return msg_date.astimezone().strftime("%m-%d %H:%M:%S")


async def format_message(
    event: events.NewMessage.Event,
    client: TelegramClient,
    chat_titles: dict[int, str],
) -> tuple[str, str, str]:
    """格式化消息为 (时间, 来源信息, 正文)。"""
    chat = event.chat
    if chat is None:
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None

    is_group = event.chat_id < 0
    chat_label = chat_titles.get(event.chat_id, "")

    if isinstance(chat, User):
        chat_label = _user_display_name(chat)
        is_group = False
    elif isinstance(chat, (Channel, Chat)):
        chat_label = getattr(chat, "title", None) or chat_label
        if chat_label:
            chat_titles[event.chat_id] = chat_label

    if not chat_label:
        try:
            entity = await client.get_entity(event.chat_id)
            if isinstance(entity, User):
                chat_label = _user_display_name(entity)
                is_group = False
            else:
                chat_label = getattr(entity, "title", None) or str(event.chat_id)
                is_group = True
            if chat_label:
                chat_titles[event.chat_id] = chat_label
        except Exception:
            chat_label = str(event.chat_id)

    sender = event.sender
    if sender is None:
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None

    if isinstance(sender, User):
        sender_label = _user_display_name(sender)
    else:
        sender_label = str(event.sender_id) if event.sender_id else "未知"

    time_str = _format_msg_time(event)
    if is_group:
        info = f"{chat_label} · {sender_label}"
    else:
        info = sender_label

    text, media_label = _extract_body(event)
    if text:
        body = text
    elif media_label:
        body = f"[{media_label}]"
    else:
        body = "[空消息]"

    return time_str, info, body


async def format_album_message(
    event: events.Album.Event,
    client: TelegramClient,
    chat_titles: dict[int, str],
) -> tuple[str, str, str]:
    """格式化相册消息为 (时间, 来源信息, 正文)。"""
    first = event.messages[0]
    chat = await event.get_chat()
    chat_id = event.chat_id
    is_group = chat_id < 0
    chat_label = chat_titles.get(chat_id, "")

    if isinstance(chat, User):
        chat_label = _user_display_name(chat)
        is_group = False
    elif isinstance(chat, (Channel, Chat)):
        chat_label = getattr(chat, "title", None) or chat_label
        if chat_label:
            chat_titles[chat_id] = chat_label

    if not chat_label:
        chat_label = str(chat_id)

    sender = await event.get_sender()
    if isinstance(sender, User):
        sender_label = _user_display_name(sender)
    else:
        sender_label = str(event.sender_id) if event.sender_id else "未知"

    msg_date = first.date
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    time_str = msg_date.astimezone().strftime("%m-%d %H:%M:%S")

    if is_group:
        info = f"{chat_label} · {sender_label}"
    else:
        info = sender_label

    text = (event.text or event.raw_text or "").strip()
    if text:
        body = text
    else:
        count = len(event.messages)
        body = f"[相册 {count} 张]" if count > 1 else "[图片]"

    return time_str, info, body


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


def _normalize_image_path(file_path: Path) -> Path | None:
    if not file_path.exists() or file_path.stat().st_size == 0:
        return None
    if file_path.suffix.lower() in IMAGE_EXTENSIONS:
        return file_path

    data = file_path.read_bytes()
    dest = file_path.with_suffix(_guess_image_suffix(data))
    if dest != file_path:
        dest.write_bytes(data)
        file_path.unlink(missing_ok=True)
    return dest


async def download_forwardable_image(
    client: TelegramClient,
    chat_id: int,
    msg,
) -> Path | None:
    """下载可转发到 Lark 的图片/贴纸，返回临时文件路径。"""
    if not msg.media:
        return None

    tmp_dir = Path(tempfile.gettempdir()) / "tg-feishu-media"
    tmp_dir.mkdir(exist_ok=True)
    base = tmp_dir / f"{chat_id}_{msg.id}"

    async def _download(message, suffix: str = "", **kwargs) -> Path | None:
        target = str(base) + suffix
        result = await client.download_media(message, file=target, **kwargs)
        if isinstance(result, bytes):
            path = Path(str(base) + suffix + _guess_image_suffix(result))
            path.write_bytes(result)
            return _normalize_image_path(path)
        if result:
            return _normalize_image_path(Path(result))
        return None

    for attempt in range(3):
        try:
            path = await _download(msg)
            if path:
                logger.info("媒体已下载: %s (%d bytes)", path.name, path.stat().st_size)
                return path

            if isinstance(msg.media, MessageMediaDocument):
                doc = msg.media.document
                if doc and doc.thumbs:
                    path = await _download(msg, suffix="_thumb", thumb=-1)
                    if path:
                        logger.info("媒体缩略图已下载: %s", path.name)
                        return path
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            return None
        except FileMigrateError as exc:
            logger.info("文件在 DC %s，切换后重试 (%d/3)", exc.new_dc, attempt + 1)
            try:
                await client._borrow_exported_sender(exc.new_dc)
            except Exception:
                logger.exception("切换 DC 失败")
            await asyncio.sleep(1)
        except FileReferenceExpiredError:
            logger.info("文件引用过期，刷新消息后重试 (%d/3)", attempt + 1)
            refreshed = await client.get_messages(chat_id, ids=msg.id)
            if refreshed:
                msg = refreshed
            await asyncio.sleep(0.5)
        except Exception:
            logger.exception("下载 Telegram 媒体失败")
            return None

    logger.warning("媒体下载重试耗尽")
    return None


class TelegramListener:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.session_path = str(BASE_DIR / config["session_name"])
        proxy = parse_proxy(config.get("telegram_proxy", ""))
        self.client = TelegramClient(
            self.session_path,
            config["api_id"],
            config["api_hash"],
            proxy=proxy,
            connection_retries=None,
            retry_delay=5,
            auto_reconnect=True,
        )
        self._proxy_configured = proxy is not None
        self.feishu = FeishuClient(
            config["webhook"],
            app_id=config.get("feishu_app_id", ""),
            app_secret=config.get("feishu_app_secret", ""),
            api_base=config.get("feishu_api_base", ""),
            api_min_interval=float(config.get("feishu_api_min_interval", 0.25)),
        )
        self.tg_to_feishu: dict[int, str] = dict(config.get("feishu_chat_map", {}))
        self.feishu_to_tg = invert_chat_map(self.tg_to_feishu)
        self.feishu_dm_chat_id = config.get("feishu_dm_chat_id", "")
        self.message_store = MessageMappingStore(BASE_DIR / "data" / "message_mappings.db")
        self.feishu_event_server: FeishuEventServer | None = None
        self.monitored_group_ids: set[int] = set()
        self.monitored_group_usernames: set[str] = set()
        self.chat_titles: dict[int, str] = {}
        self._handlers_registered = False
        self._running = True
        self._unmuted_group_count = 0
        self._unmuted_primary_titles: dict[int, str] = {}
        self._avatar_cache: dict[int, str] = {}
        self._avatar_miss_cache: set[int] = set()
        self._read_watches: set[str] = set()
        self._feishu_sent_echo_keys: set[tuple[int, int]] = set()
        self._parse_group_chats()

    def _parse_group_chats(self) -> None:
        for item in self.config["group_chats"]:
            if item.startswith("@"):
                self.monitored_group_usernames.add(item[1:].lower())
            else:
                try:
                    self.monitored_group_ids.add(int(item))
                except ValueError:
                    logger.warning("无法解析群聊配置项: %s", item)

    def _group_id_variants(self, raw: str) -> list[int | str]:
        """尝试多种群 ID 格式（机器人返回的 ID 常需加 -100 前缀）。"""
        if raw.startswith("@"):
            return [raw]
        try:
            gid = int(raw)
        except ValueError:
            return [raw]

        variants: list[int | str] = [gid]
        abs_str = str(abs(gid))
        if not str(gid).startswith("-100"):
            variants.append(int(f"-100{abs_str}"))
        return variants

    def _collect_entity_ids(
        self,
        entity,
        group_ids: set[int],
        usernames: set[str],
        chat_titles: dict[int, str],
    ) -> int:
        """把群实体对应的多种 ID 格式写入集合，返回主 ID。"""
        chat_id = tg_utils.get_peer_id(entity)
        group_ids.add(chat_id)
        abs_str = str(abs(chat_id))
        if str(chat_id).startswith("-100"):
            group_ids.add(int(f"-{abs_str[3:]}"))
        else:
            group_ids.add(int(f"-100{abs_str}"))

        username = getattr(entity, "username", None)
        if username:
            usernames.add(username.lower())

        title = getattr(entity, "title", None)
        if title:
            chat_titles[chat_id] = title
            if str(chat_id).startswith("-100"):
                chat_titles[int(f"-{abs_str[3:]}")] = title
            else:
                chat_titles[int(f"-100{abs_str}")] = title
        return chat_id

    def _add_monitored_entity(self, entity) -> None:
        """注册群实体，并加入多种 ID 格式以便匹配。"""
        chat_id = self._collect_entity_ids(
            entity,
            self.monitored_group_ids,
            self.monitored_group_usernames,
            self.chat_titles,
        )
        logger.info(
            "已添加监听群聊: %s (ID: %s)",
            getattr(entity, "title", chat_id),
            chat_id,
        )

    def _target_ids_for_config(self) -> set[int]:
        ids: set[int] = set()
        for item in self.config["group_chats"]:
            for variant in self._group_id_variants(item):
                if isinstance(variant, int):
                    ids.add(variant)
        return ids

    def _dialog_matches_config(self, dialog) -> bool:
        """判断会话是否在 GROUP_CHATS 配置范围内。"""
        if not self.config["group_chats"]:
            return False

        chat_id = dialog.id
        target_ids = self._target_ids_for_config()
        if chat_id in target_ids:
            return True

        abs_str = str(abs(chat_id))
        if str(chat_id).startswith("-100"):
            if int(f"-{abs_str[3:]}") in target_ids:
                return True
        elif int(f"-100{abs_str}") in target_ids:
            return True

        entity = dialog.entity
        username = getattr(entity, "username", None)
        if username:
            uname = username.lower()
            for item in self.config["group_chats"]:
                if item.startswith("@") and item[1:].lower() == uname:
                    return True
        return False

    async def _scan_unmuted_groups(
        self,
    ) -> tuple[set[int], set[str], dict[int, str], dict[int, str], int, int]:
        """扫描未静音群，返回 (ids, usernames, titles, primary_titles, loaded, total)。"""
        group_ids: set[int] = set()
        usernames: set[str] = set()
        chat_titles: dict[int, str] = {}
        primary_titles: dict[int, str] = {}
        total_groups = 0
        loaded_groups = 0

        async for dialog in self.client.iter_dialogs():
            if not dialog.is_group:
                continue
            total_groups += 1
            if _is_peer_muted(dialog.dialog.notify_settings):
                logger.debug("跳过静音群: %s (ID: %s)", dialog.title, dialog.id)
                continue
            chat_id = self._collect_entity_ids(
                dialog.entity, group_ids, usernames, chat_titles
            )
            primary_titles[chat_id] = dialog.title or str(chat_id)
            loaded_groups += 1

        return group_ids, usernames, chat_titles, primary_titles, loaded_groups, total_groups

    async def _scan_manual_unmuted_groups(
        self,
    ) -> tuple[set[int], set[str], dict[int, str], dict[int, str], int, int]:
        """扫描 GROUP_CHATS 列表内且未静音的群。"""
        group_ids: set[int] = set()
        usernames: set[str] = set()
        chat_titles: dict[int, str] = {}
        primary_titles: dict[int, str] = {}
        configured_count = len(self.config["group_chats"])
        loaded_groups = 0

        async for dialog in self.client.iter_dialogs():
            if not dialog.is_group:
                continue
            if not self._dialog_matches_config(dialog):
                continue
            if _is_peer_muted(dialog.dialog.notify_settings):
                logger.debug("跳过静音群(在列表中): %s (ID: %s)", dialog.title, dialog.id)
                continue
            chat_id = self._collect_entity_ids(
                dialog.entity, group_ids, usernames, chat_titles
            )
            primary_titles[chat_id] = dialog.title or str(chat_id)
            loaded_groups += 1

        return (
            group_ids,
            usernames,
            chat_titles,
            primary_titles,
            loaded_groups,
            configured_count,
        )

    def _apply_unmuted_groups(
        self,
        group_ids: set[int],
        usernames: set[str],
        chat_titles: dict[int, str],
        primary_titles: dict[int, str],
        loaded_groups: int,
        total_groups: int,
        *,
        initial: bool = False,
        filter_mode: str = "unmuted",
    ) -> None:
        """应用未静音群扫描结果，并记录新增/移除。"""
        old_primary = self._unmuted_primary_titles
        added = sorted(
            ((cid, title) for cid, title in primary_titles.items() if cid not in old_primary),
            key=lambda item: item[1],
        )
        removed = sorted(
            ((cid, title) for cid, title in old_primary.items() if cid not in primary_titles),
            key=lambda item: item[1],
        )

        self.monitored_group_ids = group_ids
        self.monitored_group_usernames = usernames
        self.chat_titles = chat_titles
        self._unmuted_primary_titles = primary_titles
        self._unmuted_group_count = loaded_groups

        if initial:
            for _, title in sorted(primary_titles.items(), key=lambda item: item[1]):
                logger.info("已添加监听群聊: %s", title)
            if filter_mode == "manual_unmuted":
                logger.info(
                    "手动+未静音模式: 配置 %d 个群，当前监听 %d 个未静音群",
                    total_groups,
                    loaded_groups,
                )
            else:
                logger.info(
                    "未静音群模式: 共 %d 个群，监听 %d 个未静音群",
                    total_groups,
                    loaded_groups,
                )
            interval = self.config.get("group_refresh_interval", 300)
            if interval > 0:
                logger.info("静音状态将每 %d 秒自动刷新", interval)
            return

        if not added and not removed:
            logger.debug("未静音群列表无变化 (%d 个)", loaded_groups)
            return

        for _, title in added:
            logger.info("新增监听群: %s", title)
        for _, title in removed:
            logger.info("移除监听群: %s", title)
        logger.info(
            "未静音群列表已刷新: 共 %d 个群，当前监听 %d 个",
            total_groups,
            loaded_groups,
        )

    async def _load_unmuted_groups(self) -> None:
        """首次加载所有未静音的群聊。"""
        result = await self._scan_unmuted_groups()
        self._apply_unmuted_groups(*result, initial=True, filter_mode="unmuted")

    async def _refresh_unmuted_groups(self) -> None:
        """轮询刷新未静音群列表。"""
        result = await self._scan_unmuted_groups()
        self._apply_unmuted_groups(*result, initial=False, filter_mode="unmuted")

    async def _load_manual_unmuted_groups(self) -> None:
        """首次加载 GROUP_CHATS 列表内且未静音的群聊。"""
        result = await self._scan_manual_unmuted_groups()
        self._apply_unmuted_groups(*result, initial=True, filter_mode="manual_unmuted")

    async def _refresh_manual_unmuted_groups(self) -> None:
        """轮询刷新列表内未静音群。"""
        result = await self._scan_manual_unmuted_groups()
        self._apply_unmuted_groups(*result, initial=False, filter_mode="manual_unmuted")

    async def _unmuted_groups_refresh_loop(self) -> None:
        """后台定时刷新未静音群。"""
        interval = self.config.get("group_refresh_interval", 300)
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                if self.config.get("group_mode") == "manual_unmuted":
                    await self._refresh_manual_unmuted_groups()
                else:
                    await self._refresh_unmuted_groups()
            except Exception:
                logger.exception("刷新未静音群列表失败")

    async def _resolve_group_entities(self) -> None:
        group_mode = self.config.get("group_mode")
        if group_mode == "unmuted":
            await self._load_unmuted_groups()
            return
        if group_mode == "manual_unmuted":
            if not self.config["group_chats"]:
                logger.warning("manual_unmuted 模式需要配置 GROUP_CHATS")
                return
            await self._load_manual_unmuted_groups()
            return

        if not self.config["group_chats"]:
            return

        target_ids = self._target_ids_for_config()
        found_ids: set[int] = set()

        for item in self.config["group_chats"]:
            for variant in self._group_id_variants(item):
                try:
                    entity = await self.client.get_entity(variant)
                    self._add_monitored_entity(entity)
                    found_ids.add(entity.id)
                    break
                except Exception:
                    continue

        async for dialog in self.client.iter_dialogs():
            if not isinstance(dialog.entity, (Channel, Chat)):
                continue
            if dialog.id in target_ids and dialog.id not in found_ids:
                self._add_monitored_entity(dialog.entity)
                found_ids.add(dialog.id)

        for item in self.config["group_chats"]:
            matched = any(
                tid in self.monitored_group_ids for tid in self._group_id_variants(item)
                if isinstance(tid, int)
            ) or (item.startswith("@") and item[1:].lower() in self.monitored_group_usernames)
            if not matched:
                logger.error("解析群聊失败: %s（请确认账号已在群内）", item)

        if not found_ids and self.config["group_chats"]:
            logger.info("当前账号可见的群聊列表（供核对 ID）：")
            async for dialog in self.client.iter_dialogs():
                if isinstance(dialog.entity, (Channel, Chat)):
                    logger.info("  - %s (ID: %s)", dialog.title, dialog.id)

    def _should_forward_chat(self, chat_id: int, chat=None) -> bool:
        if chat_id > 0:
            return True

        if chat_id in self.monitored_group_ids:
            return True

        if isinstance(chat, (Channel, Chat)):
            username = getattr(chat, "username", None)
            if username and username.lower() in self.monitored_group_usernames:
                return True

        return False

    def _should_forward(self, event: events.NewMessage.Event) -> bool:
        return self._should_forward_chat(event.chat_id, event.chat)

    async def _upload_message_images(
        self,
        chat_id: int,
        messages,
    ) -> tuple[list[str], list[Path]]:
        """下载并上传消息中的图片，返回 (image_keys, 临时文件路径列表)。"""
        image_keys: list[str] = []
        media_paths: list[Path] = []
        if not self.feishu.media_enabled:
            return image_keys, media_paths
        if self.feishu.api_rate_limited:
            logger.warning("飞书 API 限流中，跳过消息图片上传 chat=%s", chat_id)
            return image_keys, media_paths

        for msg in messages:
            if not msg.media:
                continue
            media_path = await download_forwardable_image(self.client, chat_id, msg)
            if not media_path:
                logger.warning("Telegram 媒体下载失败: chat=%s msg=%s", chat_id, msg.id)
                continue
            media_paths.append(media_path)
            image_key = await self.feishu.upload_image(media_path)
            if image_key:
                image_keys.append(image_key)
            else:
                logger.error("Lark 图片上传失败: chat=%s msg=%s", chat_id, msg.id)

        return image_keys, media_paths

    async def _get_sender_avatar_key(self, sender: object | None) -> str | None:
        """下载 TG 用户头像并上传到飞书，返回 img_key（带内存缓存）。"""
        if not self.config.get("feishu_avatar_enabled"):
            return None
        if not self.feishu.media_enabled or not isinstance(sender, User):
            return None
        if self.feishu.api_rate_limited:
            return None

        user_id = sender.id
        if user_id in self._avatar_miss_cache:
            return None
        cached = self._avatar_cache.get(user_id)
        if cached:
            return cached

        tmp_dir = Path(tempfile.gettempdir()) / "tg-feishu-avatars"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / f"avatar_{user_id}"

        try:
            result = await self.client.download_profile_photo(sender, file=str(target))
            if not result:
                self._avatar_miss_cache.add(user_id)
                return None

            photo_path = Path(str(result))
            normalized = _normalize_image_path(photo_path)
            if not normalized:
                self._avatar_miss_cache.add(user_id)
                return None

            image_key = await self.feishu.upload_image(normalized, optional=True)
            normalized.unlink(missing_ok=True)
            if not image_key:
                if self.feishu.api_rate_limited:
                    self._avatar_miss_cache.add(user_id)
                return None

            self._avatar_cache[user_id] = image_key
            logger.debug("TG 头像已缓存 user_id=%s", user_id)
            return image_key
        except Exception:
            logger.exception("下载/上传 TG 头像失败 user_id=%s", user_id)
            return None

    def _target_feishu_chat(self, tg_chat_id: int) -> str | None:
        if tg_chat_id > 0:
            return self.feishu_dm_chat_id or None
        return lookup_feishu_chat(tg_chat_id, self.tg_to_feishu)

    async def _deliver_to_feishu(
        self,
        *,
        time_str: str,
        info: str,
        body: str,
        image_keys: list[str],
        tg_chat_id: int,
        tg_msg_id: int,
        tg_sender_id: int = 0,
        tg_sender_username: str = "",
        tg_sender_name: str = "",
        avatar_key: str | None = None,
        is_outgoing: bool = False,
    ) -> bool:
        reply_enabled = bool(self.config.get("feishu_reply_enabled"))
        read_sync_enabled = bool(self.config.get("read_sync_enabled"))
        target_chat_id = self._target_feishu_chat(tg_chat_id)
        is_group = tg_chat_id < 0
        read_status = ReadStatus() if is_outgoing and read_sync_enabled else None
        updatable = read_sync_enabled and reply_enabled
        if self.feishu.api_rate_limited:
            avatar_key = None

        existing = self.message_store.get_card_by_tg(tg_chat_id, tg_msg_id)
        if existing:
            logger.debug(
                "跳过重复 TG 卡片 chat=%s msg=%s feishu=%s",
                tg_chat_id,
                tg_msg_id,
                existing.feishu_message_id,
            )
            if read_sync_enabled:
                if existing.is_outgoing:
                    self._start_outgoing_read_watch(existing.feishu_message_id)
                else:
                    self._start_incoming_read_watch(existing.feishu_message_id)
            return True

        if reply_enabled and not target_chat_id:
            logger.warning("未配置飞书群映射，跳过转发 chat_id=%s", tg_chat_id)
            return False

        prefer_bot = reply_enabled and bool(target_chat_id)
        show_read_buttons = bool(
            read_sync_enabled
            and self.config.get("read_sync_card_buttons")
            and not is_outgoing
        )
        success, feishu_message_id = await self.feishu.send_compact(
            time_str,
            info,
            body,
            image_keys=image_keys,
            avatar_key=avatar_key,
            prefer_bot=prefer_bot,
            target_chat_id=target_chat_id or "",
            is_outgoing=is_outgoing,
            is_group=is_group,
            read_status=read_status,
            updatable=updatable,
            show_read_buttons=show_read_buttons,
        )

        if success and feishu_message_id:
            self.message_store.save(
                feishu_message_id,
                tg_chat_id,
                tg_msg_id,
                info,
                tg_sender_id=tg_sender_id,
                tg_sender_username=tg_sender_username,
                tg_sender_name=tg_sender_name,
            )
            if read_sync_enabled:
                self.message_store.save_card_snapshot(
                    feishu_message_id,
                    tg_chat_id=tg_chat_id,
                    tg_msg_id=tg_msg_id,
                    time_str=time_str,
                    info=info,
                    body=body,
                    image_keys=image_keys,
                    avatar_key=avatar_key or "",
                    is_outgoing=is_outgoing,
                    is_group=is_group,
                    read_status_json=read_status.to_json() if read_status else "",
                    tg_read_synced=False,
                )
                if is_outgoing:
                    self._start_outgoing_read_watch(feishu_message_id)
                else:
                    self._start_incoming_read_watch(feishu_message_id)
            forward_log(
                logger,
                "已发送到飞书 chat=%s tg_msg=%s feishu_msg=%s outgoing=%s",
                tg_chat_id,
                tg_msg_id,
                feishu_message_id,
                is_outgoing,
            )
            return True

        if prefer_bot and not success:
            if self.config.get("feishu_reply_enabled"):
                logger.error(
                    "Bot 发送失败 chat_id=%s msg=%s（Bot 模式不降级 Webhook；"
                    "若持续限流请稍后重试或调大 FEISHU_API_MIN_INTERVAL）",
                    tg_chat_id,
                    tg_msg_id,
                )
            else:
                logger.warning("Bot 发送失败，尝试 Webhook 降级 chat_id=%s", tg_chat_id)
                success, _ = await self.feishu.send_compact(
                    time_str,
                    info,
                    body,
                    image_keys=image_keys,
                    avatar_key=avatar_key,
                    prefer_bot=False,
                )
        return success

    async def _retry_deliver(
        self,
        *,
        attempt: int = 0,
        time_str: str,
        info: str,
        body: str,
        image_keys: list[str],
        tg_chat_id: int,
        tg_msg_id: int,
        tg_sender_id: int = 0,
        tg_sender_username: str = "",
        tg_sender_name: str = "",
        avatar_key: str | None = None,
        is_outgoing: bool = False,
    ) -> None:
        max_attempts = 8
        if attempt >= max_attempts or not self._running:
            logger.error("转发重试耗尽 chat=%s msg=%s", tg_chat_id, tg_msg_id)
            return

        for _ in range(24):
            if not self._running:
                return
            if not self.feishu.api_rate_limited:
                break
            await asyncio.sleep(5)
        else:
            logger.warning(
                "限流等待超时，继续重试 chat=%s msg=%s attempt=%d",
                tg_chat_id,
                tg_msg_id,
                attempt + 1,
            )

        await asyncio.sleep(max(float(self.config.get("feishu_api_min_interval", 1.0)), 1.0))
        success = await self._deliver_to_feishu(
            time_str=time_str,
            info=info,
            body=body,
            image_keys=image_keys,
            tg_chat_id=tg_chat_id,
            tg_msg_id=tg_msg_id,
            tg_sender_id=tg_sender_id,
            tg_sender_username=tg_sender_username,
            tg_sender_name=tg_sender_name,
            avatar_key=avatar_key,
            is_outgoing=is_outgoing,
        )
        if success:
            logger.info("限流恢复后转发成功 chat=%s msg=%s", tg_chat_id, tg_msg_id)
            return
        if self.feishu.api_rate_limited and self._running:
            logger.warning(
                "转发仍被限流，60s 后重试 chat=%s msg=%s attempt=%d",
                tg_chat_id,
                tg_msg_id,
                attempt + 1,
            )
            await asyncio.sleep(60)
            await self._retry_deliver(
                attempt=attempt + 1,
                time_str=time_str,
                info=info,
                body=body,
                image_keys=image_keys,
                tg_chat_id=tg_chat_id,
                tg_msg_id=tg_msg_id,
                tg_sender_id=tg_sender_id,
                tg_sender_username=tg_sender_username,
                tg_sender_name=tg_sender_name,
                avatar_key=avatar_key,
                is_outgoing=is_outgoing,
            )
            return
        logger.error("转发失败 chat=%s msg=%s", tg_chat_id, tg_msg_id)

    def _card_from_snapshot(self, snapshot: CardSnapshot) -> dict:
        read_status = ReadStatus.from_json(snapshot.read_status_json)
        read_sync = bool(self.config.get("read_sync_enabled"))
        use_buttons = bool(
            read_sync
            and self.config.get("read_sync_card_buttons")
            and not snapshot.is_outgoing
            and not snapshot.tg_read_synced
        )
        return self.feishu.build_compact_card(
            snapshot.time_str,
            snapshot.info,
            snapshot.body,
            image_keys=snapshot.image_keys,
            avatar_key=snapshot.avatar_key or None,
            is_outgoing=snapshot.is_outgoing,
            is_group=snapshot.is_group,
            read_status=read_status if snapshot.is_outgoing else None,
            updatable=True,
            show_read_buttons=use_buttons,
        )

    async def _patch_card_snapshot(self, snapshot: CardSnapshot) -> bool:
        card = self._card_from_snapshot(snapshot)
        return await self.feishu.patch_message_card(snapshot.feishu_message_id, card)

    async def _fetch_private_read_status(
        self,
        snapshot: CardSnapshot,
    ) -> ReadStatus | None:
        try:
            result = await self.client(
                GetOutboxReadDateRequest(
                    peer=snapshot.tg_chat_id,
                    msg_id=snapshot.tg_msg_id,
                )
            )
        except RPCError as exc:
            if "MESSAGE_NOT_READ_YET" in str(exc):
                return ReadStatus(is_read=False)
            logger.debug(
                "私聊已读时间不可用 chat=%s msg=%s err=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                exc,
            )
            return ReadStatus(is_read=True)
        except Exception:
            logger.exception(
                "查询私聊已读失败 chat=%s msg=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
            )
            return None

        read_ts = 0.0
        if getattr(result, "date", None):
            read_ts = result.date.timestamp()
        return ReadStatus(is_read=True, read_ts=read_ts)

    async def _fetch_group_read_status(
        self,
        snapshot: CardSnapshot,
    ) -> ReadStatus | None:
        try:
            participants = await self.client(
                GetMessageReadParticipantsRequest(
                    peer=snapshot.tg_chat_id,
                    msg_id=snapshot.tg_msg_id,
                )
            )
        except RPCError as exc:
            if any(
                token in str(exc)
                for token in ("CHAT_TOO_BIG", "MSG_TOO_OLD", "MSG_ID_INVALID")
            ):
                read_skip(
                    logger,
                    "群已读 API 不可用 chat=%s msg=%s err=%s",
                    snapshot.tg_chat_id,
                    snapshot.tg_msg_id,
                    exc,
                )
                return None
            logger.warning(
                "查询小群已读失败 chat=%s msg=%s err=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                exc,
            )
            return None
        except Exception:
            logger.exception(
                "查询小群已读异常 chat=%s msg=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
            )
            return None

        if not participants:
            return ReadStatus(is_read=False)

        previous = ReadStatus.from_json(snapshot.read_status_json)
        prev_by_id = {item.user_id: item for item in previous.readers if item.user_id}
        readers: list[ReadParticipant] = []
        for item in participants:
            user_id = getattr(item, "user_id", 0) or 0
            read_ts = 0.0
            if getattr(item, "date", None):
                read_ts = item.date.timestamp()
            name = ""
            avatar_key = ""
            prev_reader = prev_by_id.get(user_id)
            if prev_reader:
                name = prev_reader.name
                avatar_key = prev_reader.avatar_key
            if user_id and not name:
                user = await _resolve_tg_user(self.client, user_id)
                if user is not None:
                    name = _user_display_name(user)
            if user_id and not avatar_key and not self.feishu.api_rate_limited:
                user = await _resolve_tg_user(self.client, user_id)
                if user is not None:
                    if not name:
                        name = _user_display_name(user)
                    avatar_key = await self._get_sender_avatar_key(user) or ""
            readers.append(
                ReadParticipant(
                    user_id=user_id,
                    name=name,
                    avatar_key=avatar_key,
                    read_ts=read_ts,
                )
            )

        return ReadStatus(
            is_read=bool(readers),
            read_ts=max((reader.read_ts for reader in readers), default=0.0),
            readers=readers,
        )

    def _group_read_watch_done(
        self,
        snapshot: CardSnapshot,
        previous: ReadStatus,
        current: ReadStatus,
        *,
        stable_rounds: int,
    ) -> bool:
        if not snapshot.is_group:
            return current.is_read
        if not current.readers:
            return False
        if len(current.readers) > len(previous.readers):
            return False
        return stable_rounds >= 3

    async def _refresh_outgoing_read_status(self, snapshot: CardSnapshot) -> None:
        if not snapshot.is_outgoing:
            read_skip(
                logger,
                "非 outgoing 卡片 feishu_msg=%s chat=%s",
                snapshot.feishu_message_id,
                snapshot.tg_chat_id,
            )
            return
        if self.feishu.api_rate_limited:
            read_skip(
                logger,
                "TG→飞书 限流中跳过 PATCH feishu_msg=%s chat=%s tg_msg=%s",
                snapshot.feishu_message_id,
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
            )
            return

        if snapshot.is_group:
            read_status = await self._fetch_group_read_status(snapshot)
        else:
            read_status = await self._fetch_private_read_status(snapshot)

        if read_status is None:
            read_skip(
                logger,
                "TG 已读查询无结果 feishu_msg=%s chat=%s tg_msg=%s group=%s",
                snapshot.feishu_message_id,
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                snapshot.is_group,
            )
            return

        previous = ReadStatus.from_json(snapshot.read_status_json)
        if snapshot.is_group and read_status.readers:
            read_status = ReadStatus(
                is_read=read_status.is_read,
                read_ts=read_status.read_ts,
                readers=merge_read_participants(read_status.readers, previous.readers),
                ui_expanded=previous.ui_expanded,
            )
        if read_status_equal(read_status, previous):
            read_skip(
                logger,
                "TG→飞书 状态无变化 feishu_msg=%s chat=%s tg_msg=%s read=%s readers=%d",
                snapshot.feishu_message_id,
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                read_status.is_read,
                len(read_status.readers),
            )
            return

        self.message_store.update_read_status(
            snapshot.feishu_message_id,
            read_status.to_json(),
        )
        updated = CardSnapshot(
            feishu_message_id=snapshot.feishu_message_id,
            tg_chat_id=snapshot.tg_chat_id,
            tg_msg_id=snapshot.tg_msg_id,
            time_str=snapshot.time_str,
            info=snapshot.info,
            body=snapshot.body,
            image_keys=snapshot.image_keys,
            avatar_key=snapshot.avatar_key,
            is_outgoing=snapshot.is_outgoing,
            is_group=snapshot.is_group,
            read_status_json=read_status.to_json(),
            tg_read_synced=snapshot.tg_read_synced,
        )
        patched = await self._patch_card_snapshot(updated)
        if not patched:
            read_skip(
                logger,
                "TG→飞书 PATCH 失败 feishu_msg=%s chat=%s tg_msg=%s",
                snapshot.feishu_message_id,
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
            )
            return
        read_log(
            logger,
            "TG→飞书已读更新 chat=%s msg=%s read=%s readers=%d feishu_msg=%s",
            snapshot.tg_chat_id,
            snapshot.tg_msg_id,
            read_status.is_read,
            len(read_status.readers),
            snapshot.feishu_message_id,
        )

    def _start_incoming_read_watch(self, feishu_message_id: str) -> None:
        if not self.config.get("read_sync_enabled"):
            return
        if not self.config.get("read_watch_enabled"):
            return
        if feishu_message_id in self._read_watches:
            return
        self._read_watches.add(feishu_message_id)
        asyncio.create_task(self._watch_incoming_feishu_read(feishu_message_id))

    def _start_outgoing_read_watch(self, feishu_message_id: str) -> None:
        if not self.config.get("read_sync_enabled"):
            return
        if not self.config.get("read_watch_enabled"):
            return
        if feishu_message_id in self._read_watches:
            return
        self._read_watches.add(feishu_message_id)
        asyncio.create_task(self._watch_outgoing_tg_read(feishu_message_id))

    async def _watch_incoming_feishu_read(self, feishu_message_id: str) -> None:
        interval = max(int(self.config.get("read_watch_interval", 5)), 3)
        try:
            for _ in range(60):
                if not self._running:
                    return
                if self.feishu.api_rate_limited:
                    await asyncio.sleep(interval)
                    continue
                snapshot = self.message_store.get_card_snapshot(feishu_message_id)
                if not snapshot or snapshot.tg_read_synced:
                    return
                readers = await self.feishu.get_message_read_users(feishu_message_id)
                for open_id, _read_ts in readers:
                    if self._feishu_reader_allowed(open_id):
                        await self._sync_feishu_read_to_tg(
                            feishu_message_id,
                            reader_open_id=open_id,
                        )
                        return
                await asyncio.sleep(interval)
        finally:
            self._read_watches.discard(feishu_message_id)

    async def _watch_outgoing_tg_read(self, feishu_message_id: str) -> None:
        interval = max(int(self.config.get("read_watch_interval", 5)), 3)
        stable_rounds = 0
        last_reader_count = -1
        try:
            for _ in range(360):
                if not self._running:
                    return
                if self.feishu.api_rate_limited:
                    await asyncio.sleep(interval)
                    continue
                snapshot = self.message_store.get_card_snapshot(feishu_message_id)
                if not snapshot or not snapshot.is_outgoing:
                    return
                previous = ReadStatus.from_json(snapshot.read_status_json)
                await self._refresh_outgoing_read_status(snapshot)
                snapshot = self.message_store.get_card_snapshot(feishu_message_id)
                if not snapshot:
                    return
                current = ReadStatus.from_json(snapshot.read_status_json)
                reader_count = len(current.readers)
                if snapshot.is_group:
                    if reader_count == last_reader_count and reader_count > 0:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                    last_reader_count = reader_count
                if self._group_read_watch_done(
                    snapshot,
                    previous,
                    current,
                    stable_rounds=stable_rounds,
                ):
                    return
                await asyncio.sleep(interval)
        finally:
            self._read_watches.discard(feishu_message_id)

    async def _maybe_recall_feishu_user_message(self, feishu_message_id: str) -> None:
        if not self.config.get("feishu_recall_user_reply"):
            return
        if not feishu_message_id:
            return
        recalled = await self.feishu.recall_message(feishu_message_id)
        if not recalled:
            logger.info(
                "未能撤回飞书用户消息 message_id=%s（需群主通过 API 指定机器人为管理员）",
                feishu_message_id,
            )

    async def _after_feishu_send_to_tg(
        self,
        *,
        feishu_message_id: str,
        tg_chat_id: int,
        tg_msg_ids: list[int],
        echo_time: str,
        echo_info: str,
        echo_body: str,
    ) -> None:
        await self._maybe_recall_feishu_user_message(feishu_message_id)
        if not self.config.get("read_sync_enabled"):
            return
        if self.config.get("feishu_hide_outgoing_echo"):
            logger.debug(
                "跳过飞书 outgoing 回显卡片 chat=%s msgs=%s",
                tg_chat_id,
                tg_msg_ids,
            )
            return
        await self._deliver_feishu_outgoing_cards(
            tg_chat_id=tg_chat_id,
            tg_msg_ids=tg_msg_ids,
            time_str=echo_time,
            info=echo_info,
            body=echo_body,
        )

    async def _deliver_feishu_outgoing_cards(
        self,
        *,
        tg_chat_id: int,
        tg_msg_ids: list[int],
        time_str: str,
        info: str,
        body: str,
        image_keys: list[str] | None = None,
        avatar_key: str | None = None,
    ) -> None:
        """飞书发到 TG 后立即发一张 outgoing 卡片，并阻止 TG 回显重复转发。"""
        for tg_msg_id in tg_msg_ids:
            success = await self._deliver_to_feishu(
                time_str=time_str,
                info=info,
                body=body,
                image_keys=image_keys or [],
                tg_chat_id=tg_chat_id,
                tg_msg_id=tg_msg_id,
                avatar_key=avatar_key,
                is_outgoing=True,
            )
            if success:
                for variant in tg_chat_id_variants(tg_chat_id):
                    self._feishu_sent_echo_keys.add((variant, tg_msg_id))

    def _should_skip_feishu_echo(self, chat_id: int, msg_id: int) -> bool:
        for variant in tg_chat_id_variants(chat_id):
            key = (variant, msg_id)
            if key in self._feishu_sent_echo_keys:
                self._feishu_sent_echo_keys.discard(key)
                return True
        return False

    async def _on_tg_message_read(self, event: events.MessageRead.Event) -> None:
        if not self.config.get("read_sync_enabled") or not event.outbox:
            return
        if self.feishu.api_rate_limited:
            read_skip(logger, "TG→飞书 限流中跳过 chat=%s", event.chat_id)
            return

        if not event.max_id:
            read_skip(logger, "TG→飞书 无 max_id chat=%s", event.chat_id)
            return

        snapshots = self.message_store.list_outgoing_cards_up_to(
            event.chat_id,
            event.max_id,
        )
        read_log(
            logger,
            "TG MessageRead chat=%s max_id=%s outgoing_cards=%d",
            event.chat_id,
            event.max_id,
            len(snapshots),
        )
        for snapshot in snapshots:
            await self._refresh_outgoing_read_status(snapshot)

        latest = self.message_store.get_card_by_tg(event.chat_id, event.max_id)
        if latest and latest.is_outgoing:
            await self._refresh_outgoing_read_status(latest)

        if snapshots:
            asyncio.create_task(
                self._delayed_refresh_outgoing_read(
                    [snapshot.feishu_message_id for snapshot in snapshots],
                    delay=3.0,
                )
            )

    async def _delayed_refresh_outgoing_read(
        self,
        feishu_message_ids: list[str],
        *,
        delay: float = 3.0,
    ) -> None:
        """MessageRead 后延迟重查，避免 TG API 读者列表尚未就绪。"""
        await asyncio.sleep(delay)
        if not self._running:
            return
        read_log(
            logger,
            "延迟重查 TG 已读 count=%d delay=%ss",
            len(feishu_message_ids),
            delay,
        )
        for feishu_message_id in feishu_message_ids:
            snapshot = self.message_store.get_card_snapshot(feishu_message_id)
            if snapshot and snapshot.is_outgoing:
                await self._refresh_outgoing_read_status(snapshot)

    def _feishu_reader_allowed(self, reader_open_id: str) -> bool:
        allowed = set(self.config.get("feishu_allowed_open_ids", []))
        if not allowed:
            return bool(reader_open_id)
        return reader_open_id in allowed

    async def _sync_feishu_read_to_tg(
        self,
        feishu_message_id: str,
        *,
        reader_open_id: str = "",
        via_reply: bool = False,
    ) -> None:
        snapshot = self.message_store.get_card_snapshot(feishu_message_id)
        if snapshot is None:
            read_skip(logger, "飞书→TG 无映射 feishu_msg=%s", feishu_message_id)
            return
        if snapshot.is_outgoing:
            read_skip(
                logger,
                "飞书→TG 跳过 outgoing feishu_msg=%s chat=%s",
                feishu_message_id,
                snapshot.tg_chat_id,
            )
            return
        if (
            not snapshot.is_group
            and not via_reply
            and not self.config.get("read_sync_dm_on_view", False)
        ):
            read_skip(
                logger,
                "飞书→TG 私聊仅回复时同步已读（READ_SYNC_DM_ON_VIEW=false）"
                " feishu_msg=%s tg_user=%s",
                feishu_message_id,
                snapshot.tg_chat_id,
            )
            return
        if snapshot.tg_read_synced:
            read_skip(
                logger,
                "飞书→TG 已同步过 chat=%s tg_msg=%s feishu_msg=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                feishu_message_id,
            )
            return
        if reader_open_id and not self._feishu_reader_allowed(reader_open_id):
            read_skip(
                logger,
                "飞书→TG 读者不在白名单 reader=%s feishu_msg=%s",
                reader_open_id,
                feishu_message_id,
            )
            return

        try:
            await self.client.send_read_acknowledge(
                snapshot.tg_chat_id,
                max_id=snapshot.tg_msg_id,
            )
            self.message_store.mark_tg_read_synced(feishu_message_id)
            read_log(
                logger,
                "飞书→TG 已读同步 chat=%s tg_msg=%s feishu_msg=%s reader=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                feishu_message_id,
                reader_open_id or "-",
            )
        except Exception:
            logger.exception(
                "飞书→TG 已读同步失败 chat=%s msg=%s feishu_msg=%s",
                snapshot.tg_chat_id,
                snapshot.tg_msg_id,
                feishu_message_id,
            )

    async def _sync_incoming_chat_read_for_tg_chat(
        self,
        tg_chat_id: int,
        *,
        reader_open_id: str = "",
        trigger: str = "",
    ) -> list[str]:
        """同 TG 会话所有未读入站消息一并标已读（与会话已读行为一致）。"""
        if reader_open_id and not self._feishu_reader_allowed(reader_open_id):
            read_skip(
                logger,
                "飞书→TG 读者不在白名单 reader=%s chat=%s",
                reader_open_id,
                tg_chat_id,
            )
            return []

        pending = self.message_store.list_incoming_unsynced_for_tg_chat(tg_chat_id)
        if not pending:
            read_skip(
                logger,
                "飞书→TG 会话无未读 chat=%s trigger=%s",
                tg_chat_id,
                trigger or "-",
            )
            return []

        max_msg_id = max(item.tg_msg_id for item in pending)
        try:
            await self.client.send_read_acknowledge(
                tg_chat_id,
                max_id=max_msg_id,
            )
        except Exception:
            logger.exception(
                "飞书→TG 会话已读失败 chat=%s max_msg=%s",
                tg_chat_id,
                max_msg_id,
            )
            return []

        synced_ids: list[str] = []
        for item in pending:
            self.message_store.mark_tg_read_synced(item.feishu_message_id)
            synced_ids.append(item.feishu_message_id)
        read_log(
            logger,
            "飞书→TG 会话已读 chat=%s max_msg=%s count=%d trigger=%s reader=%s",
            tg_chat_id,
            max_msg_id,
            len(synced_ids),
            trigger or "-",
            reader_open_id or "-",
        )
        return synced_ids

    async def _sync_incoming_chat_read_to_tg(
        self,
        feishu_message_id: str,
        *,
        reader_open_id: str = "",
    ) -> list[str]:
        """点 Rd / 回复卡片：以该卡片所属会话触发已读。"""
        snapshot = self.message_store.get_card_snapshot(feishu_message_id)
        if snapshot is None:
            read_skip(logger, "飞书→TG 无映射 feishu_msg=%s", feishu_message_id)
            return []
        if snapshot.is_outgoing:
            return []
        return await self._sync_incoming_chat_read_for_tg_chat(
            snapshot.tg_chat_id,
            reader_open_id=reader_open_id,
            trigger=feishu_message_id,
        )

    async def _handle_feishu_message_read(
        self,
        message_ids: list[str],
        reader_open_id: str,
        _read_ts: float,
    ) -> None:
        if self.config.get("read_sync_card_buttons"):
            read_skip(
                logger,
                "已启用卡片按钮，忽略飞书 message_read 事件 count=%d",
                len(message_ids),
            )
            return
        read_log(
            logger,
            "处理飞书已读事件 reader=%s count=%d ids=%s",
            reader_open_id,
            len(message_ids),
            ",".join(message_ids[:5]) + ("..." if len(message_ids) > 5 else ""),
        )
        for message_id in message_ids:
            await self._sync_feishu_read_to_tg(
                message_id,
                reader_open_id=reader_open_id,
            )

    async def _patch_synced_cards_background(
        self,
        feishu_message_ids: list[str],
    ) -> None:
        interval = max(float(self.config.get("feishu_api_min_interval", 1.0)), 0.5)
        for feishu_message_id in feishu_message_ids:
            if not self._running:
                return
            snapshot = self.message_store.get_card_snapshot(feishu_message_id)
            if snapshot is None or not snapshot.tg_read_synced:
                continue
            await self._patch_card_snapshot(snapshot)
            await asyncio.sleep(interval)

    async def _handle_feishu_card_action(
        self,
        feishu_message_id: str,
        value: dict,
        reader_open_id: str,
    ) -> tuple[str, dict | None]:
        if not self.config.get("read_sync_enabled"):
            return "未启用已读同步", None

        action = str(value.get("action", "")).strip()
        snapshot = self.message_store.get_card_snapshot(feishu_message_id)
        if snapshot is None:
            return "找不到消息映射", None

        if action == "read_one":
            if snapshot.is_outgoing:
                return "", None
            if reader_open_id and not self._feishu_reader_allowed(reader_open_id):
                return "无操作权限", None
            pending = self.message_store.list_incoming_unsynced_for_tg_chat(
                snapshot.tg_chat_id
            )
            if not pending:
                return "", None
            for item in pending:
                self.message_store.mark_tg_read_synced(item.feishu_message_id)
            all_ids = [item.feishu_message_id for item in pending]
            asyncio.create_task(
                self._complete_read_one_to_tg(snapshot, pending, all_ids)
            )
            return "", None

        return "未知操作", None

    async def _complete_read_one_to_tg(
        self,
        snapshot: CardSnapshot,
        pending: list[CardSnapshot],
        feishu_message_ids: list[str],
    ) -> None:
        """read_one 回调已响应 {} 后：PATCH 去掉 ✓，再同步 TG 已读。"""
        for feishu_message_id in feishu_message_ids:
            if not self._running:
                return
            updated = self.message_store.get_card_snapshot(feishu_message_id)
            if updated:
                await self._patch_card_snapshot(updated)
            interval = max(float(self.config.get("feishu_api_min_interval", 1.0)), 0.5)
            if len(feishu_message_ids) > 1:
                await asyncio.sleep(interval)

        max_msg_id = max(item.tg_msg_id for item in pending)
        try:
            await self.client.send_read_acknowledge(
                snapshot.tg_chat_id,
                max_id=max_msg_id,
            )
            read_log(
                logger,
                "飞书→TG 会话已读 chat=%s max_msg=%s count=%d trigger=%s",
                snapshot.tg_chat_id,
                max_msg_id,
                len(pending),
                snapshot.feishu_message_id,
            )
        except Exception:
            logger.exception(
                "飞书→TG 会话已读失败 chat=%s max_msg=%s",
                snapshot.tg_chat_id,
                max_msg_id,
            )
            return

    async def _poll_read_status_loop(self) -> None:
        interval = max(int(self.config.get("read_poll_interval", 15)), 10)
        batch_size = max(int(self.config.get("read_poll_batch_size", 8)), 1)
        outgoing_batch = max(int(self.config.get("read_poll_outgoing_batch", 5)), 1)
        startup_delay = max(int(self.config.get("read_poll_startup_delay", 30)), 0)
        if startup_delay:
            read_log(
                logger,
                "已读轮询将在 %d 秒后启动，避免重启后挤占飞书 API 配额",
                startup_delay,
            )
            await asyncio.sleep(startup_delay)
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self.config.get("read_sync_enabled"):
                    continue
                if self.feishu.api_rate_limited:
                    logger.warning("飞书 API 限流中，跳过本轮已读轮询")
                    continue

                pending = self.message_store.list_incoming_unsynced_tg_read(limit=batch_size)
                if pending:
                    logger.debug("已读轮询: %d 条入站消息待同步到 TG", len(pending))
                for snapshot in pending:
                    if self.feishu.api_rate_limited:
                        break
                    readers = await self.feishu.get_message_read_users(
                        snapshot.feishu_message_id
                    )
                    if readers:
                        read_log(
                            logger,
                            "飞书已读查询 feishu_msg=%s readers=%d",
                            snapshot.feishu_message_id,
                            len(readers),
                        )
                    for open_id, _read_ts in readers:
                        if self._feishu_reader_allowed(open_id):
                            await self._sync_feishu_read_to_tg(
                                snapshot.feishu_message_id,
                                reader_open_id=open_id,
                            )
                            break

                for snapshot in self.message_store.list_recent_outgoing_group_cards(
                    limit=outgoing_batch
                ):
                    if self.feishu.api_rate_limited:
                        break
                    await self._refresh_outgoing_read_status(snapshot)

                for snapshot in self.message_store.list_recent_outgoing_private_cards(
                    limit=outgoing_batch
                ):
                    if self.feishu.api_rate_limited:
                        break
                    await self._refresh_outgoing_read_status(snapshot)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("已读状态轮询异常")

    async def _log_cleanup_loop(self) -> None:
        from log_config import cleanup_old_logs

        hours = max(int(self.config.get("log_cleanup_interval", 24)), 1)
        retention = max(int(self.config.get("log_retention_days", 3)), 1)
        while self._running:
            try:
                await asyncio.sleep(hours * 3600)
                if not self._running:
                    return
                removed = cleanup_old_logs(retention_days=retention)
                if removed:
                    logger.info("已清理 %d 个过期日志文件", removed)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("日志清理异常")

    async def _download_feishu_images(
        self,
        feishu_message_id: str,
        image_keys: list[str],
    ) -> list[Path]:
        image_paths: list[Path] = []
        if not image_keys or not feishu_message_id:
            return image_paths

        for image_key in image_keys:
            image_path = await self.feishu.download_message_image(
                feishu_message_id,
                image_key,
            )
            if image_path:
                image_paths.append(image_path)
        return image_paths

    async def _send_content_to_tg(
        self,
        tg_chat_id: int,
        reply_text: str,
        image_paths: list[Path],
        *,
        reply_to: int | None = None,
        formatting_entities: list[TypeMessageEntity] | None = None,
    ) -> list[int]:
        kwargs: dict = {}
        if reply_to is not None:
            kwargs["reply_to"] = reply_to
        if formatting_entities:
            kwargs["formatting_entities"] = formatting_entities

        sent_ids: list[int] = []
        if image_paths:
            if len(image_paths) == 1:
                if reply_text:
                    msg = await self.client.send_file(
                        tg_chat_id,
                        image_paths[0],
                        caption=reply_text,
                        **kwargs,
                    )
                else:
                    msg = await self.client.send_file(tg_chat_id, image_paths[0], **kwargs)
            else:
                msg = await self.client.send_file(
                    tg_chat_id,
                    image_paths,
                    caption=reply_text or None,
                    **kwargs,
                )
        elif reply_text:
            msg = await self.client.send_message(tg_chat_id, reply_text, **kwargs)
        else:
            return sent_ids

        if msg is None:
            return sent_ids
        if isinstance(msg, list):
            sent_ids.extend(item.id for item in msg if getattr(item, "id", None))
        elif getattr(msg, "id", None):
            sent_ids.append(msg.id)
        return sent_ids

    async def _handle_feishu_message(
        self,
        feishu_chat_id: str,
        reply_text: str,
        sender_open_id: str,
        parent_message_id: str | None,
        feishu_message_id: str,
        image_keys: list[str],
    ) -> None:
        if parent_message_id and self.config.get("read_sync_enabled"):
            read_log(
                logger,
                "飞书回复触发会话已读 parent=%s sender=%s",
                parent_message_id,
                sender_open_id,
            )
            synced_ids = await self._sync_incoming_chat_read_to_tg(
                parent_message_id,
                reader_open_id=sender_open_id,
            )
            if self.config.get("read_sync_card_buttons") and synced_ids:
                others = [
                    mid for mid in synced_ids if mid != parent_message_id
                ]
                parent_snap = self.message_store.get_card_snapshot(parent_message_id)
                if parent_snap and parent_snap.tg_read_synced:
                    asyncio.create_task(self._patch_card_snapshot(parent_snap))
                if others:
                    asyncio.create_task(self._patch_synced_cards_background(others))

        image_paths = await self._download_feishu_images(feishu_message_id, image_keys)
        if image_keys and not image_paths:
            await self.feishu.send_bot_text(
                "❌ 图片下载失败，无法发送到 Telegram，请稍后重试。",
                feishu_chat_id,
            )
            return

        # 群聊：在对应飞书群里直接发消息即可回到 TG 群
        if feishu_chat_id in self.feishu_to_tg:
            tg_chat_id = self.feishu_to_tg[feishu_chat_id]

            if self.config.get("read_sync_enabled") and not parent_message_id:
                read_log(
                    logger,
                    "飞书群直接发送触发会话已读 chat=%s sender=%s",
                    tg_chat_id,
                    sender_open_id,
                )
                synced_ids = await self._sync_incoming_chat_read_for_tg_chat(
                    tg_chat_id,
                    reader_open_id=sender_open_id,
                    trigger=feishu_message_id,
                )
                if self.config.get("read_sync_card_buttons") and synced_ids:
                    asyncio.create_task(
                        self._patch_synced_cards_background(synced_ids)
                    )

            final_text = reply_text
            reply_to: int | None = None
            formatting_entities: list[TypeMessageEntity] | None = None

            if parent_message_id:
                mapping = self.message_store.get(parent_message_id)
                if mapping is None:
                    logger.info(
                        "飞书回复未命中映射 parent_id=%s，按普通消息发送",
                        parent_message_id,
                    )
                elif mapping.tg_chat_id != tg_chat_id:
                    logger.warning(
                        "飞书回复映射群不匹配 parent_id=%s expected=%s actual=%s",
                        parent_message_id,
                        tg_chat_id,
                        mapping.tg_chat_id,
                    )
                else:
                    final_text, formatting_entities, reply_to = _build_reply_with_mention(
                        reply_text,
                        mapping,
                    )
                    logger.info(
                        "飞书回复 TG 卡片：自动 @ 原发言者 sender_id=%s username=%s reply_to=%s",
                        mapping.tg_sender_id,
                        mapping.tg_sender_username or "-",
                        reply_to,
                    )

            try:
                sent_ids = await self._send_content_to_tg(
                    tg_chat_id,
                    final_text,
                    image_paths,
                    reply_to=reply_to,
                    formatting_entities=formatting_entities,
                )
                logger.info(
                    "飞书群消息已发到 TG chat=%s msg_ids=%s images=%d reply_to=%s",
                    tg_chat_id,
                    ",".join(str(item) for item in sent_ids) if sent_ids else "-",
                    len(image_paths),
                    reply_to or "-",
                )
                if sent_ids:
                    me = await self.client.get_me()
                    echo_time = datetime.now(timezone.utc).astimezone().strftime(
                        "%m-%d %H:%M:%S"
                    )
                    chat_title = self.chat_titles.get(tg_chat_id, str(tg_chat_id))
                    echo_info = f"{chat_title} · {me.first_name or '我'}"
                    echo_body = final_text or ("[图片]" if image_paths else "")
                    await self._after_feishu_send_to_tg(
                        feishu_message_id=feishu_message_id,
                        tg_chat_id=tg_chat_id,
                        tg_msg_ids=sent_ids,
                        echo_time=echo_time,
                        echo_info=echo_info,
                        echo_body=echo_body,
                    )
            except Exception:
                logger.exception("发送到 TG 群失败 chat=%s", tg_chat_id)
                await self.feishu.send_bot_text(
                    "❌ 发送到 Telegram 群失败，请稍后重试。",
                    feishu_chat_id,
                )
            finally:
                for image_path in image_paths:
                    image_path.unlink(missing_ok=True)
            return

        # 私聊汇总群：可回复卡片指定联系人，否则发到最近入站私聊用户
        if self.feishu_dm_chat_id and feishu_chat_id == self.feishu_dm_chat_id:
            tg_chat_id: int | None = None
            if parent_message_id:
                mapping = self.message_store.get(parent_message_id)
                if mapping is None:
                    logger.warning("未找到私聊映射 parent_id=%s", parent_message_id)
                    await self.feishu.send_bot_text(
                        "未能回复：请直接回复机器人转发的那条消息卡片。",
                        feishu_chat_id,
                    )
                    for image_path in image_paths:
                        image_path.unlink(missing_ok=True)
                    return
                tg_chat_id = mapping.tg_chat_id
            else:
                tg_chat_id = self.message_store.get_latest_incoming_private_chat_id()
                if not tg_chat_id:
                    await self.feishu.send_bot_text(
                        "暂无可回复的私聊：请先收到对方消息，或使用「回复」指定联系人。",
                        feishu_chat_id,
                    )
                    for image_path in image_paths:
                        image_path.unlink(missing_ok=True)
                    return
                logger.info(
                    "私聊未回复发送：路由到最近入站用户 chat=%s",
                    tg_chat_id,
                )

            if self.config.get("read_sync_enabled") and not parent_message_id:
                read_log(
                    logger,
                    "飞书私聊直接发送触发会话已读 chat=%s sender=%s",
                    tg_chat_id,
                    sender_open_id,
                )
                synced_ids = await self._sync_incoming_chat_read_for_tg_chat(
                    tg_chat_id,
                    reader_open_id=sender_open_id,
                    trigger=feishu_message_id,
                )
                if self.config.get("read_sync_card_buttons") and synced_ids:
                    asyncio.create_task(
                        self._patch_synced_cards_background(synced_ids)
                    )

            try:
                sent_ids = await self._send_content_to_tg(
                    tg_chat_id,
                    reply_text,
                    image_paths,
                )
                logger.info(
                    "私聊已发到 TG chat=%s msg_ids=%s images=%d parent=%s",
                    tg_chat_id,
                    ",".join(str(item) for item in sent_ids) if sent_ids else "-",
                    len(image_paths),
                    parent_message_id or "-",
                )
                if sent_ids:
                    me = await self.client.get_me()
                    echo_time = datetime.now(timezone.utc).astimezone().strftime(
                        "%m-%d %H:%M:%S"
                    )
                    echo_info = me.first_name or "我"
                    echo_body = reply_text or ("[图片]" if image_paths else "")
                    await self._after_feishu_send_to_tg(
                        feishu_message_id=feishu_message_id,
                        tg_chat_id=tg_chat_id,
                        tg_msg_ids=sent_ids,
                        echo_time=echo_time,
                        echo_info=echo_info,
                        echo_body=echo_body,
                    )
            except Exception:
                logger.exception(
                    "私聊回复 TG 失败 user=%s",
                    tg_chat_id,
                )
                await self.feishu.send_bot_text(
                    "❌ 回复 Telegram 私聊失败，请稍后重试。",
                    feishu_chat_id,
                )
            finally:
                for image_path in image_paths:
                    image_path.unlink(missing_ok=True)

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        if event.message.grouped_id:
            logger.debug(
                "跳过相册分片 chat=%s msg=%s grouped_id=%s",
                event.chat_id,
                event.message.id,
                event.message.grouped_id,
            )
            return

        text, media_label = _extract_body(event)
        direction = "发出" if event.out else "收到"

        if is_log_verbose():
            forward_log(
                logger,
                "TG消息 [%s] chat=%s msg=%s text=%r media=%s",
                direction,
                event.chat_id,
                event.message.id,
                text[:80] if text else "",
                media_label or "none",
            )
        else:
            forward_log(
                logger,
                "TG消息 [%s] chat=%s msg=%s media=%s",
                direction,
                event.chat_id,
                event.message.id,
                media_label or "none",
            )

        if event.out and self._should_skip_feishu_echo(event.chat_id, event.message.id):
            forward_skip(
                logger,
                "Feishu 发出消息的 TG 回显 chat=%s msg=%s",
                event.chat_id,
                event.message.id,
            )
            return

        if event.out and self.config.get("incoming_only", True):
            forward_skip(
                logger,
                "自己发出的消息（如需转发请设 INCOMING_ONLY=false）chat=%s msg=%s",
                event.chat_id,
                event.message.id,
            )
            return

        if not self._should_forward(event):
            forward_skip(
                logger,
                "不在监听范围 chat_id=%s msg=%s",
                event.chat_id,
                event.message.id,
            )
            return

        time_str, info, body = await format_message(event, self.client, self.chat_titles)
        forward_log(
            logger,
            "转发到 Lark chat=%s msg=%s outgoing=%s",
            event.chat_id,
            event.message.id,
            event.out,
        )

        sender = event.sender
        if sender is None:
            with contextlib.suppress(Exception):
                sender = await event.get_sender()
        tg_sender_id, tg_sender_username, tg_sender_name = _sender_info_from_user(sender)

        media_paths: list[Path] = []
        image_keys: list[str] = []
        try:
            if event.message.media:
                if self.feishu.media_enabled:
                    image_keys, media_paths = await self._upload_message_images(
                        event.chat_id,
                        [event.message],
                    )
                    if image_keys and body.startswith("[") and body.endswith("]"):
                        body = ""
                else:
                    logger.debug("未配置 FEISHU_APP_ID/SECRET，跳过图片转发")

            avatar_key = await self._get_sender_avatar_key(sender)

            success = await self._deliver_to_feishu(
                time_str=time_str,
                info=info,
                body=body,
                image_keys=image_keys,
                tg_chat_id=event.chat_id,
                tg_msg_id=event.message.id,
                tg_sender_id=tg_sender_id,
                tg_sender_username=tg_sender_username,
                tg_sender_name=tg_sender_name,
                avatar_key=avatar_key,
                is_outgoing=event.out,
            )
        finally:
            for media_path in media_paths:
                if media_path.exists():
                    media_path.unlink(missing_ok=True)

        if success:
            forward_log(
                logger,
                "已转发消息 chat_id=%s msg_id=%s outgoing=%s",
                event.chat_id,
                event.message.id,
                event.out,
            )
        elif self.feishu.api_rate_limited:
            logger.warning(
                "转发因限流失败，已安排重试 chat=%s msg=%s",
                event.chat_id,
                event.message.id,
            )
            asyncio.create_task(
                self._retry_deliver(
                    time_str=time_str,
                    info=info,
                    body=body,
                    image_keys=image_keys,
                    tg_chat_id=event.chat_id,
                    tg_msg_id=event.message.id,
                    tg_sender_id=tg_sender_id,
                    tg_sender_username=tg_sender_username,
                    tg_sender_name=tg_sender_name,
                    avatar_key=avatar_key,
                    is_outgoing=event.out,
                )
            )
        else:
            logger.error("转发失败 chat_id=%s msg_id=%s", event.chat_id, event.message.id)

    async def _on_album(self, event: events.Album.Event) -> None:
        first = event.messages[0]
        direction = "发出" if first.out else "收到"
        caption = (event.text or event.raw_text or "").strip()

        if is_log_verbose():
            logger.info(
                "TG相册 [%s] chat=%s grouped_id=%s count=%d caption=%r",
                direction,
                event.chat_id,
                event.grouped_id,
                len(event.messages),
                caption[:80] if caption else "",
            )
        else:
            logger.info(
                "TG相册 [%s] chat=%s grouped_id=%s count=%d first_msg=%s",
                direction,
                event.chat_id,
                event.grouped_id,
                len(event.messages),
                first.id,
            )

        if first.out and self.config.get("incoming_only", True):
            logger.info("跳过：自己发出的相册（如需转发请设 INCOMING_ONLY=false）")
            return

        chat = await event.get_chat()
        if not self._should_forward_chat(event.chat_id, chat):
            logger.info("跳过：不在监听范围 chat_id=%s", event.chat_id)
            return

        time_str, info, body = await format_album_message(
            event, self.client, self.chat_titles
        )
        if is_log_verbose():
            logger.info(
                "转发相册到 Lark chat=%s msg_id=%s count=%d info=%s",
                event.chat_id,
                first.id,
                len(event.messages),
                info,
            )
        else:
            logger.info(
                "转发相册到 Lark chat=%s msg_id=%s count=%d",
                event.chat_id,
                first.id,
                len(event.messages),
            )

        sender = first.sender
        if sender is None:
            with contextlib.suppress(Exception):
                sender = await first.get_sender()
        tg_sender_id, tg_sender_username, tg_sender_name = _sender_info_from_user(sender)

        media_paths: list[Path] = []
        image_keys: list[str] = []
        try:
            if self.feishu.media_enabled:
                image_keys, media_paths = await self._upload_message_images(
                    event.chat_id,
                    event.messages,
                )
                if image_keys and body.startswith("[") and body.endswith("]"):
                    body = ""
            else:
                logger.debug("未配置 FEISHU_APP_ID/SECRET，跳过图片转发")

            avatar_key = await self._get_sender_avatar_key(sender)

            success = await self._deliver_to_feishu(
                time_str=time_str,
                info=info,
                body=body,
                image_keys=image_keys,
                tg_chat_id=event.chat_id,
                tg_msg_id=first.id,
                tg_sender_id=tg_sender_id,
                tg_sender_username=tg_sender_username,
                tg_sender_name=tg_sender_name,
                avatar_key=avatar_key,
                is_outgoing=first.out,
            )
        finally:
            for media_path in media_paths:
                if media_path.exists():
                    media_path.unlink(missing_ok=True)

        msg_ids = ",".join(str(m.id) for m in event.messages)
        if success:
            logger.info("已转发相册 chat_id=%s msg_ids=%s", event.chat_id, msg_ids)
        elif self.feishu.api_rate_limited:
            logger.warning(
                "相册转发因限流失败，已安排重试 chat=%s msg=%s",
                event.chat_id,
                first.id,
            )
            asyncio.create_task(
                self._retry_deliver(
                    time_str=time_str,
                    info=info,
                    body=body,
                    image_keys=image_keys,
                    tg_chat_id=event.chat_id,
                    tg_msg_id=first.id,
                    tg_sender_id=tg_sender_id,
                    tg_sender_username=tg_sender_username,
                    tg_sender_name=tg_sender_name,
                    avatar_key=avatar_key,
                    is_outgoing=first.out,
                )
            )
        else:
            logger.error("转发相册失败 chat_id=%s msg_ids=%s", event.chat_id, msg_ids)

    @staticmethod
    def _is_password_error(exc: RPCError) -> bool:
        message = str(exc).upper()
        return "PASSWORD" in message or "PASSWORD_HASH_INVALID" in message

    async def _prompt_two_factor_password(self) -> None:
        """两步验证：密码错误时允许重试，避免重新请求验证码。"""
        for attempt in range(1, 6):
            password = input("请输入两步验证密码: ").strip()
            try:
                await self.client.sign_in(password=password)
                return
            except RPCError as exc:
                if self._is_password_error(exc):
                    logger.error("两步验证密码错误，请重试 (%d/5)", attempt)
                    continue
                raise
        raise RuntimeError("两步验证密码错误次数过多，请稍后重新运行 ./start.sh login")

    async def _ensure_login(self) -> None:
        await self.client.connect()
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            logger.info("已登录: %s (ID: %s)", me.first_name, me.id)
            return

        logger.info("首次登录，正在发送验证码到 %s ...", self.config["phone"])
        await self.client.send_code_request(self.config["phone"])
        code = input("请输入 Telegram 验证码: ").strip()
        try:
            await self.client.sign_in(self.config["phone"], code)
        except SessionPasswordNeededError:
            await self._prompt_two_factor_password()

        me = await self.client.get_me()
        logger.info("登录成功: %s (ID: %s)", me.first_name, me.id)

    def _register_handlers(self) -> None:
        if self._handlers_registered:
            return

        @self.client.on(events.NewMessage())
        async def handler(event: events.NewMessage.Event) -> None:
            try:
                await self._on_new_message(event)
            except Exception:
                logger.exception("处理消息时出错")

        @self.client.on(events.Album())
        async def album_handler(event: events.Album.Event) -> None:
            try:
                await self._on_album(event)
            except Exception:
                logger.exception("处理相册时出错")

        if self.config.get("read_sync_enabled"):
            @self.client.on(events.MessageRead())
            async def read_handler(event: events.MessageRead.Event) -> None:
                try:
                    await self._on_tg_message_read(event)
                except Exception:
                    logger.exception("处理 TG 已读事件时出错")

        self._handlers_registered = True
        incoming_only = self.config.get("incoming_only", True)
        mode = "仅他人消息" if incoming_only else "全部消息(含自己)"
        logger.info("消息监听模式: %s", mode)
        if self.config.get("read_sync_enabled"):
            channels = ["TG MessageRead 事件"]
            if self.config.get("read_sync_card_buttons"):
                channels.append("卡片按钮(飞书→TG)")
            else:
                channels.append("飞书已读事件")
            if self.config.get("read_watch_enabled"):
                channels.append("快速监听")
            if self.config.get("read_poll_enabled"):
                channels.append("兜底轮询")
            logger.info("已读同步: 已启用（%s）", " + ".join(channels))
            if self.config.get("read_debug"):
                logger.warning(
                    "排查日志已开启（READ_DEBUG=true），严格模式下可见 [转发] / [已读] / [·跳过] 日志"
                )

    async def run_once(self) -> None:
        await self._ensure_login()
        await self._resolve_group_entities()
        self._register_handlers()

        me = await self.client.get_me()
        media_status = "已启用" if self.feishu.media_enabled else "未配置(仅文字)"
        proxy_status = "已配置" if self._proxy_configured else "未配置"
        group_mode = self.config.get("group_mode", "manual")
        if group_mode == "unmuted":
            group_desc = f"未静音群 {self._unmuted_group_count}"
        elif group_mode == "manual_unmuted":
            group_desc = f"列表内未静音 {self._unmuted_group_count}"
        elif self.config["group_chats"]:
            group_desc = str(len(self.monitored_group_ids))
        else:
            group_desc = "未配置"
        reply_status = "未启用"
        if self.config.get("feishu_reply_enabled"):
            mapped = len(self.tg_to_feishu)
            dm = "有" if self.feishu_dm_chat_id else "无"
            reply_status = (
                f"已启用 :{self.config['feishu_event_port']} "
                f"(群映射 {mapped} 个, 私聊汇总群 {dm})"
            )
            for tg_id, feishu_id in self.tg_to_feishu.items():
                title = self.chat_titles.get(tg_id, str(tg_id))
                logger.info("飞书群映射: TG %s (%s) -> %s", title, tg_id, feishu_id)
        logger.warning(
            "服务运行中 | TG=%s | 群=%s | 飞书回调 :%s | 日志级别=%s | 已读排查=%s",
            me.first_name,
            group_desc,
            self.config.get("feishu_event_port", 8080),
            self.config.get("log_level", "WARNING"),
            "开" if self.config.get("read_debug") else "关",
        )
        logger.info(
            "监听已启动 | 用户: %s | 私聊: 全部 | 群聊: %s | 图片转发: %s | 代理: %s | 飞书回复: %s",
            me.first_name,
            group_desc,
            media_status,
            proxy_status,
            reply_status,
        )

        if self.config.get("feishu_reply_enabled"):
            allowed = set(self.config.get("feishu_allowed_open_ids", []))
            group_chat_ids = set(self.feishu_to_tg.keys())
            self.feishu_event_server = FeishuEventServer(
                self.feishu,
                group_chat_ids=group_chat_ids,
                dm_chat_id=self.feishu_dm_chat_id,
                verification_token=self.config["feishu_verification_token"],
                encrypt_key=self.config.get("feishu_encrypt_key", ""),
                allowed_open_ids=allowed,
                on_message=self._handle_feishu_message,
                on_message_read=(
                    self._handle_feishu_message_read
                    if self.config.get("read_sync_enabled")
                    else None
                ),
                on_card_action=(
                    self._handle_feishu_card_action
                    if self.config.get("read_sync_enabled")
                    and self.config.get("read_sync_card_buttons")
                    else None
                ),
                path=self.config.get("feishu_event_path", "/feishu/event"),
            )
            await self.feishu_event_server.start(
                self.config["feishu_event_host"],
                self.config["feishu_event_port"],
            )
            removed = self.message_store.cleanup_old()
            if removed:
                logger.info("已清理 %d 条过期消息映射", removed)
            from log_config import cleanup_old_logs

            log_removed = cleanup_old_logs(
                retention_days=max(int(self.config.get("log_retention_days", 3)), 1),
            )
            if log_removed:
                logger.info("已清理 %d 个过期日志文件", log_removed)

        refresh_task: asyncio.Task | None = None
        read_poll_task: asyncio.Task | None = None
        log_cleanup_task: asyncio.Task | None = None
        if (
            group_mode in ("unmuted", "manual_unmuted")
            and self.config.get("group_refresh_interval", 300) > 0
        ):
            refresh_task = asyncio.create_task(self._unmuted_groups_refresh_loop())
        if self.config.get("read_sync_enabled") and self.config.get("read_poll_enabled"):
            read_poll_task = asyncio.create_task(self._poll_read_status_loop())
        if max(int(self.config.get("log_cleanup_interval", 24)), 0) > 0:
            log_cleanup_task = asyncio.create_task(self._log_cleanup_loop())

        try:
            await self.client.run_until_disconnected()
        finally:
            if self.feishu_event_server is not None:
                await self.feishu_event_server.stop()
                self.feishu_event_server = None
            if refresh_task:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresh_task
            if read_poll_task:
                read_poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await read_poll_task
            if log_cleanup_task:
                log_cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await log_cleanup_task

    async def run_with_reconnect(self) -> None:
        interval = self.config["reconnect_interval"]
        while self._running:
            try:
                await self.run_once()
            except AuthKeyUnregisteredError:
                logger.error("会话已失效，请删除 session 文件后重新登录")
                raise
            except (RuntimeError, EOFError, KeyboardInterrupt):
                raise
            except RPCError as exc:
                if not await self.client.is_user_authorized():
                    logger.error("登录失败: %s", exc)
                    logger.error(
                        "请删除 session 文件后重新运行 ./start.sh login；"
                        "若提示 SEND_CODE_UNAVAILABLE 请等待 15~30 分钟再试"
                    )
                    raise SystemExit(1) from exc
                logger.warning("连接断开 (%s)，%s 秒后重连...", exc, interval)
            except (ConnectionError, OSError) as exc:
                logger.warning("连接断开 (%s)，%s 秒后重连...", exc, interval)
            except asyncio.CancelledError:
                logger.info("收到取消信号，正在退出...")
                break
            except Exception:
                logger.exception("运行异常，%s 秒后重连...", interval)
            finally:
                if self.client.is_connected():
                    await self.client.disconnect()

            if not self._running:
                break

            if self._running:
                await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    def request_shutdown(self, loop: asyncio.AbstractEventLoop) -> None:
        """请求优雅退出：标记停止并断开 TG 连接以结束 run_until_disconnected。"""
        self.stop()

        async def _shutdown() -> None:
            if self.feishu_event_server is not None:
                with contextlib.suppress(Exception):
                    await self.feishu_event_server.stop()
            if self.client.is_connected():
                with contextlib.suppress(Exception):
                    await self.client.disconnect()

        loop.create_task(_shutdown())


async def main() -> None:
    config = load_config()
    setup_logging_from_config(config)

    listener = TelegramListener(config)

    loop = asyncio.get_running_loop()
    shutdown_count = 0

    def _signal_handler() -> None:
        nonlocal shutdown_count
        shutdown_count += 1
        if shutdown_count == 1:
            logger.info("收到退出信号，正在停止...")
            listener.request_shutdown(loop)
            return
        logger.warning("再次收到退出信号，强制退出")
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda *_: listener.request_shutdown(loop))

    await listener.run_with_reconnect()
    logger.info("程序已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已退出")
