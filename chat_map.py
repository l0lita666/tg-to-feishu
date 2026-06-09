"""TG 群与飞书群的映射解析。"""

from __future__ import annotations


def parse_feishu_chat_map(raw: str) -> dict[int, str]:
    """解析 FEISHU_CHAT_MAP，格式: TG群ID:飞书chat_id,TG群ID:飞书chat_id"""
    mapping: dict[int, str] = {}
    for item in raw.split(","):
        piece = item.strip()
        if not piece or ":" not in piece:
            continue
        tg_raw, feishu_chat_id = piece.split(":", 1)
        tg_raw = tg_raw.strip()
        feishu_chat_id = feishu_chat_id.strip()
        if not tg_raw or not feishu_chat_id:
            continue
        try:
            mapping[int(tg_raw)] = feishu_chat_id
        except ValueError:
            continue
    return mapping


def invert_chat_map(tg_to_feishu: dict[int, str]) -> dict[str, int]:
    return {feishu_id: tg_id for tg_id, feishu_id in tg_to_feishu.items()}


def lookup_feishu_chat(tg_chat_id: int, tg_to_feishu: dict[int, str]) -> str | None:
    if tg_chat_id in tg_to_feishu:
        return tg_to_feishu[tg_chat_id]

    # Telethon 群 ID 与配置写法可能差 -100 前缀，尝试变体
    abs_id = abs(tg_chat_id)
    for tg_id, feishu_id in tg_to_feishu.items():
        if abs(tg_id) == abs_id:
            return feishu_id
        tg_abs = abs(tg_id)
        if str(tg_abs).endswith(str(abs_id)) or str(abs_id).endswith(str(tg_abs)):
            return feishu_id
    return None
