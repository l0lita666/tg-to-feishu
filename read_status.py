"""TG 已读状态数据结构与卡片展示辅助。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CHECK_SENT = "✓"
CHECK_READ = "✓/"
MAX_READER_AVATARS = 5
READER_AVATAR_SIZE = "14px 14px"
READER_AVATAR_RADIUS = "7px"


@dataclass
class ReadParticipant:
    user_id: int
    name: str = ""
    avatar_key: str = ""
    read_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "avatar_key": self.avatar_key,
            "read_ts": self.read_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReadParticipant:
        return cls(
            user_id=int(data.get("user_id") or 0),
            name=str(data.get("name") or ""),
            avatar_key=str(data.get("avatar_key") or ""),
            read_ts=float(data.get("read_ts") or 0.0),
        )


@dataclass
class ReadStatus:
    is_read: bool = False
    read_ts: float = 0.0
    readers: list[ReadParticipant] = field(default_factory=list)
    ui_expanded: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "is_read": self.is_read,
                "read_ts": self.read_ts,
                "readers": [item.to_dict() for item in self.readers],
                "ui_expanded": self.ui_expanded,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> ReadStatus:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        readers_raw = data.get("readers") or []
        readers: list[ReadParticipant] = []
        if isinstance(readers_raw, list):
            for item in readers_raw:
                if isinstance(item, dict):
                    readers.append(ReadParticipant.from_dict(item))
        return cls(
            is_read=bool(data.get("is_read")),
            read_ts=float(data.get("read_ts") or 0.0),
            readers=readers,
            ui_expanded=bool(data.get("ui_expanded")),
        )


def merge_read_participants(
    readers: list[ReadParticipant],
    previous: list[ReadParticipant],
) -> list[ReadParticipant]:
    """保留已缓存的头像/昵称，避免重复拉取失败导致读者从卡片消失。"""
    prev_by_id = {item.user_id: item for item in previous if item.user_id}
    merged: list[ReadParticipant] = []
    for reader in readers:
        prev = prev_by_id.get(reader.user_id)
        if prev:
            if not reader.avatar_key and prev.avatar_key:
                reader.avatar_key = prev.avatar_key
            if not reader.name and prev.name:
                reader.name = prev.name
        merged.append(reader)
    return merged


def read_status_equal(left: ReadStatus, right: ReadStatus) -> bool:
    """按已读状态与读者集合比较，忽略 UI 展开状态。"""
    if left.is_read != right.is_read:
        return False
    if abs(left.read_ts - right.read_ts) > 0.001:
        return False
    if len(left.readers) != len(right.readers):
        return False
    left_map = {
        item.user_id: (item.name, item.avatar_key, round(item.read_ts, 3))
        for item in left.readers
    }
    for item in right.readers:
        snapshot = left_map.get(item.user_id)
        if snapshot is None:
            return False
        name, avatar_key, read_ts = snapshot
        if (
            item.name != name
            or item.avatar_key != avatar_key
            or round(item.read_ts, 3) != read_ts
        ):
            return False
    return True


def _format_read_time(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _check_suffix(is_read: bool, read_ts: float) -> str:
    check = CHECK_READ if is_read else CHECK_SENT
    time_str = _format_read_time(read_ts) if is_read else ""
    if time_str:
        return f"{check} {time_str}"
    return check


def format_read_suffix_html(
    read_status: ReadStatus | None,
    *,
    is_outgoing: bool,
    is_group: bool,
) -> str:
    """发出消息已读状态统一在底栏展示，标题行不再附加。"""
    del read_status, is_outgoing, is_group
    return ""


def format_meta_with_read(
    meta_line: str,
    read_status: ReadStatus | None,
    *,
    is_outgoing: bool,
    is_group: bool,
) -> str:
    """兼容旧调用：整行 meta + 已读后缀（默认灰色）。"""
    suffix = format_read_suffix_html(
        read_status,
        is_outgoing=is_outgoing,
        is_group=is_group,
    )
    base = f"<font color='grey'>{meta_line}</font>"
    return f"{base} {suffix}" if suffix else base


def _reader_fallback_label(name: str, user_id: int = 0) -> str:
    label = (name or "").strip()
    if label:
        return label[:1].upper()
    if user_id:
        return str(user_id % 10)
    return "·"


def _reader_initial_element(name: str, *, user_id: int = 0) -> dict[str, Any]:
    label = _reader_fallback_label(name, user_id)
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": label,
            "text_size": "notation",
            "text_align": "center",
            "text_color": "grey",
        },
    }


def _reader_avatar_element(avatar_key: str, name: str) -> dict[str, Any]:
    return {
        "tag": "img",
        "img_key": avatar_key,
        "alt": {"tag": "plain_text", "content": name or "已读"},
        "preview": False,
        "scale_type": "crop_center",
        "size": READER_AVATAR_SIZE,
        "corner_radius": READER_AVATAR_RADIUS,
    }


def _reader_time_element(read_ts: float) -> dict[str, Any] | None:
    time_str = _format_read_time(read_ts)
    if not time_str:
        return None
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": time_str,
            "text_size": "notation",
            "text_align": "center",
            "text_color": "grey",
        },
    }


def _reader_receipt_column(
    reader: ReadParticipant,
    *,
    show_time: bool,
) -> dict[str, Any]:
    """单个读者：头像/首字占位；展开时在头像下方显示已读时间。"""
    if reader.avatar_key:
        elements: list[dict[str, Any]] = [
            _reader_avatar_element(reader.avatar_key, reader.name)
        ]
    else:
        elements = [_reader_initial_element(reader.name, user_id=reader.user_id)]
    if show_time:
        time_element = _reader_time_element(reader.read_ts)
        if time_element:
            elements.append(time_element)
    return {
        "tag": "column",
        "width": "auto",
        "vertical_align": "top",
        "horizontal_align": "center",
        "elements": elements,
    }


def _build_reader_receipt_row(
    readers: list[ReadParticipant],
    *,
    extra: int = 0,
    show_times: bool = False,
    leading_spacer: bool = True,
) -> dict[str, Any]:
    columns = [
        _reader_receipt_column(reader, show_time=show_times) for reader in readers
    ]
    if extra > 0:
        columns.append(
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"<font color='grey'>+{extra}</font>",
                        },
                    }
                ],
            }
        )
    if leading_spacer:
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [],
            },
            *columns,
        ]
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "small",
        "columns": columns,
    }


def _build_outgoing_check_column_set(
    status: ReadStatus,
) -> dict[str, Any]:
    """发出消息底栏右侧：灰色 ✓ / ✓/ + 时间。"""
    check = CHECK_READ if status.is_read else CHECK_SENT
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": check,
                "text_size": "notation",
                "text_align": "center",
                "text_color": "grey",
            },
        }
    ]
    if status.is_read:
        time_element = _reader_time_element(status.read_ts)
        if time_element:
            elements.append(time_element)
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "top",
                "horizontal_align": "center",
                "elements": elements,
            }
        ],
    }


def build_read_receipt_inner_column_set(
    read_status: ReadStatus | None,
    *,
    is_outgoing: bool,
    is_group: bool,
) -> dict[str, Any] | None:
    """已读展示（无左侧 spacer），供底栏右侧与发送时间并排。"""
    if not is_outgoing:
        return None

    status = read_status or ReadStatus()

    if is_group:
        if status.readers:
            shown = sorted(
                status.readers,
                key=lambda item: (item.read_ts or 0.0, item.user_id),
            )[:MAX_READER_AVATARS]
            extra = len(status.readers) - len(shown)
            has_times = any(item.read_ts > 0 for item in status.readers)
            return _build_reader_receipt_row(
                shown,
                extra=extra,
                show_times=has_times,
                leading_spacer=False,
            )
        return _build_outgoing_check_column_set(status)

    return _build_outgoing_check_column_set(status)


def build_read_receipt_elements(
    read_status: ReadStatus | None,
    *,
    is_outgoing: bool,
    is_group: bool,
) -> list[dict[str, Any]]:
    """群聊发出消息：头像 + 已读时间（有则常显在头像下方）。"""
    if not is_outgoing or not is_group:
        return []

    status = read_status or ReadStatus()
    if not status.readers:
        return []

    shown = sorted(
        status.readers,
        key=lambda item: (item.read_ts or 0.0, item.user_id),
    )[:MAX_READER_AVATARS]
    extra = len(status.readers) - len(shown)
    has_times = any(item.read_ts > 0 for item in status.readers)
    return [
        _build_reader_receipt_row(
            shown,
            extra=extra,
            show_times=has_times,
        )
    ]
