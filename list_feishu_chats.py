#!/usr/bin/env python3
"""列出应用机器人所在的飞书/Lark 群聊及 chat_id。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


async def main() -> None:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    api_base = os.getenv("FEISHU_API_BASE", "").strip()
    if not api_base:
        webhook = os.getenv("FEISHU_WEBHOOK_URL", "")
        api_base = (
            "https://open.larksuite.com/open-apis"
            if "larksuite.com" in webhook
            else "https://open.feishu.cn/open-apis"
        )

    if not app_id or not app_secret:
        print("请先配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return

    async with httpx.AsyncClient(timeout=30) as client:
        token_resp = await client.post(
            f"{api_base}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        token_data = token_resp.json()
        token = token_data.get("tenant_access_token")
        if not token:
            print("获取 token 失败:", token_data)
            return

        page_token = ""
        print("机器人所在群聊：")
        print("-" * 50)
        while True:
            params = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            resp = await client.get(
                f"{api_base}/im/v1/chats",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            data = resp.json()
            if data.get("code", 0) != 0:
                print("获取群列表失败:", json.dumps(data, ensure_ascii=False, indent=2))
                return

            items = data.get("data", {}).get("items", [])
            for chat in items:
                print(f"{chat.get('name', '(无名称)')}")
                print(f"  chat_id: {chat.get('chat_id')}")
                print()

            page_token = data.get("data", {}).get("page_token", "")
            if not page_token:
                break

        print("将目标群的 chat_id 填入 .env 的 FEISHU_CHAT_MAP（格式: TG群ID:oc_xxx）")


if __name__ == "__main__":
    asyncio.run(main())
