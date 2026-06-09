#!/usr/bin/env python3
"""列出所有群聊及静音/监听状态。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient

from telegram_listener import _is_peer_muted, parse_proxy

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session = str(BASE_DIR / os.getenv("SESSION_NAME", "telegram_session"))
    proxy = parse_proxy(os.getenv("TELEGRAM_PROXY", ""))

    client = TelegramClient(session, api_id, api_hash, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        print("未登录")
        return

    me = await client.get_me()
    print(f"账号: {me.first_name} (ID: {me.id})\n")

    groups: list[tuple] = []
    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        ns = dialog.dialog.notify_settings
        muted = _is_peer_muted(ns)
        silent = getattr(ns, "silent", None)
        mute_until = getattr(ns, "mute_until", None)
        groups.append((dialog.title, dialog.id, muted, silent, mute_until))

    groups.sort(key=lambda item: item[0].lower())
    monitored = [g for g in groups if not g[2]]
    skipped = [g for g in groups if g[2]]

    print(f"全部群聊: {len(groups)} 个")
    print("=" * 60)
    print(f"【正在监听 - 未静音】{len(monitored)} 个:")
    for i, (title, chat_id, _, silent, _) in enumerate(monitored, 1):
        note = " [仅关闭通知音]" if silent else ""
        print(f"{i:2d}. {title} (ID: {chat_id}){note}")

    print()
    print(f"【已跳过 - 静音中】{len(skipped)} 个:")
    for i, (title, chat_id, _, _, mute_until) in enumerate(skipped, 1):
        print(f"{i:2d}. {title} (ID: {chat_id}) mute_until={mute_until}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
