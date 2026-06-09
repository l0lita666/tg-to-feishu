#!/usr/bin/env python3
"""列出当前 Telegram 账号加入的所有群聊及 ID。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session = str(BASE_DIR / os.getenv("SESSION_NAME", "telegram_session"))

    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("未登录，请先运行: ./start.sh login")
        return

    me = await client.get_me()
    print(f"账号: {me.first_name} (ID: {me.id})\n")
    print("群聊列表：")
    print("-" * 50)

    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, (Channel, Chat)):
            username = getattr(dialog.entity, "username", None) or ""
            uname = f" @{username}" if username else ""
            print(f"{dialog.title}{uname}")
            print(f"  ID: {dialog.id}")
            print()

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
