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
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeSticker,
    MessageEntityMentionName,
    MessageMediaDocument,
    MessageMediaPhoto,
    TypeMessageEntity,
    User,
)

from chat_map import invert_chat_map, lookup_feishu_chat, parse_feishu_chat_map
from feishu_client import FeishuClient, IMAGE_EXTENSIONS
from feishu_event_server import FeishuEventServer
from message_mapping import MessageMappingStore, TgMessageRef

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
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

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
    }


def setup_logging(level: str) -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "listener.log"

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
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
        if not self.feishu.media_enabled or not isinstance(sender, User):
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

            image_key = await self.feishu.upload_image(normalized)
            normalized.unlink(missing_ok=True)
            if not image_key:
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
    ) -> bool:
        reply_enabled = bool(self.config.get("feishu_reply_enabled"))
        target_chat_id = self._target_feishu_chat(tg_chat_id)

        if reply_enabled and not target_chat_id:
            logger.warning("未配置飞书群映射，跳过转发 chat_id=%s", tg_chat_id)
            return False

        prefer_bot = reply_enabled and bool(target_chat_id)
        success, feishu_message_id = await self.feishu.send_compact(
            time_str,
            info,
            body,
            image_keys=image_keys,
            avatar_key=avatar_key,
            prefer_bot=prefer_bot,
            target_chat_id=target_chat_id or "",
        )

        # 记录飞书卡片映射，供「回复卡片」定位 TG 原消息与发言者
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
            return True

        if prefer_bot and not success:
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
    ) -> None:
        kwargs: dict = {}
        if reply_to is not None:
            kwargs["reply_to"] = reply_to
        if formatting_entities:
            kwargs["formatting_entities"] = formatting_entities

        if image_paths:
            if len(image_paths) == 1:
                if reply_text:
                    await self.client.send_file(
                        tg_chat_id,
                        image_paths[0],
                        caption=reply_text,
                        **kwargs,
                    )
                else:
                    await self.client.send_file(tg_chat_id, image_paths[0], **kwargs)
            else:
                await self.client.send_file(
                    tg_chat_id,
                    image_paths,
                    caption=reply_text or None,
                    **kwargs,
                )
        elif reply_text:
            await self.client.send_message(tg_chat_id, reply_text, **kwargs)

    async def _handle_feishu_message(
        self,
        feishu_chat_id: str,
        reply_text: str,
        sender_open_id: str,
        parent_message_id: str | None,
        feishu_message_id: str,
        image_keys: list[str],
    ) -> None:
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
                await self._send_content_to_tg(
                    tg_chat_id,
                    final_text,
                    image_paths,
                    reply_to=reply_to,
                    formatting_entities=formatting_entities,
                )
                logger.info(
                    "飞书群消息已发到 TG 群 chat=%s <- 飞书用户 %s (text=%s images=%d reply_to=%s)",
                    tg_chat_id,
                    sender_open_id,
                    bool(final_text),
                    len(image_paths),
                    reply_to,
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

        # 私聊汇总群：必须回复某人的消息卡片
        if self.feishu_dm_chat_id and feishu_chat_id == self.feishu_dm_chat_id:
            if not parent_message_id:
                await self.feishu.send_bot_text(
                    "私聊汇总群请使用「回复」功能：点某人的转发消息卡片再发送，才能回到对应 TG 私聊。",
                    feishu_chat_id,
                )
                for image_path in image_paths:
                    image_path.unlink(missing_ok=True)
                return

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

            try:
                await self._send_content_to_tg(
                    mapping.tg_chat_id,
                    reply_text,
                    image_paths,
                )
                logger.info(
                    "私聊已发到 TG user=%s <- 飞书用户 %s (text=%s images=%d)",
                    mapping.tg_chat_id,
                    sender_open_id,
                    bool(reply_text),
                    len(image_paths),
                )
            except Exception:
                logger.exception(
                    "私聊回复 TG 失败 user=%s msg=%s",
                    mapping.tg_chat_id,
                    mapping.tg_msg_id,
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

        logger.info(
            "TG消息 [%s] chat=%s msg=%s | 文字=%r | 附件=%s",
            direction,
            event.chat_id,
            event.message.id,
            text[:80] if text else "",
            media_label or "无",
        )

        if event.out and self.config.get("incoming_only", True):
            logger.info("跳过：自己发出的消息（如需转发请设 INCOMING_ONLY=false）")
            return

        if not self._should_forward(event):
            logger.info("跳过：不在监听范围 chat_id=%s", event.chat_id)
            return

        time_str, info, body = await format_message(event, self.client, self.chat_titles)
        logger.info(">>> 转发到 Lark... %s · %s", time_str, info)

        sender = event.sender
        if sender is None:
            with contextlib.suppress(Exception):
                sender = await event.get_sender()
        tg_sender_id, tg_sender_username, tg_sender_name = _sender_info_from_user(sender)
        avatar_key = await self._get_sender_avatar_key(sender)

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
            )
        finally:
            for media_path in media_paths:
                if media_path.exists():
                    media_path.unlink(missing_ok=True)

        if success:
            logger.info("已转发消息 chat_id=%s msg_id=%s", event.chat_id, event.message.id)
        else:
            logger.error("转发失败 chat_id=%s msg_id=%s", event.chat_id, event.message.id)

    async def _on_album(self, event: events.Album.Event) -> None:
        first = event.messages[0]
        direction = "发出" if first.out else "收到"
        caption = (event.text or event.raw_text or "").strip()

        logger.info(
            "TG相册 [%s] chat=%s grouped_id=%s 共%d张 | 文字=%r",
            direction,
            event.chat_id,
            event.grouped_id,
            len(event.messages),
            caption[:80] if caption else "",
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
        logger.info(">>> 转发相册到 Lark... %s · %s (%d 张)", time_str, info, len(event.messages))

        sender = first.sender
        if sender is None:
            with contextlib.suppress(Exception):
                sender = await first.get_sender()
        tg_sender_id, tg_sender_username, tg_sender_name = _sender_info_from_user(sender)
        avatar_key = await self._get_sender_avatar_key(sender)

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
            )
        finally:
            for media_path in media_paths:
                if media_path.exists():
                    media_path.unlink(missing_ok=True)

        msg_ids = ",".join(str(m.id) for m in event.messages)
        if success:
            logger.info("已转发相册 chat_id=%s msg_ids=%s", event.chat_id, msg_ids)
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

        self._handlers_registered = True
        incoming_only = self.config.get("incoming_only", True)
        mode = "仅他人消息" if incoming_only else "全部消息(含自己)"
        logger.info("消息监听模式: %s", mode)

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
                path=self.config.get("feishu_event_path", "/feishu/event"),
            )
            await self.feishu_event_server.start(
                self.config["feishu_event_host"],
                self.config["feishu_event_port"],
            )
            removed = self.message_store.cleanup_old()
            if removed:
                logger.info("已清理 %d 条过期消息映射", removed)

        refresh_task: asyncio.Task | None = None
        if (
            group_mode in ("unmuted", "manual_unmuted")
            and self.config.get("group_refresh_interval", 300) > 0
        ):
            refresh_task = asyncio.create_task(self._unmuted_groups_refresh_loop())

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
                await self.client.disconnect()

        loop.create_task(_shutdown())


async def main() -> None:
    config = load_config()
    setup_logging(config["log_level"])

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
