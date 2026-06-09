"""飞书消息 ID 与 Telegram 消息的映射存储。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TgMessageRef:
    tg_chat_id: int
    tg_msg_id: int
    chat_title: str = ""
    tg_sender_id: int = 0
    tg_sender_username: str = ""
    tg_sender_name: str = ""


class MessageMappingStore:
    def __init__(self, db_path: Path, max_age_days: int = 30) -> None:
        self.db_path = db_path
        self.max_age_days = max_age_days
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_message_mappings (
                    feishu_message_id TEXT PRIMARY KEY,
                    tg_chat_id INTEGER NOT NULL,
                    tg_msg_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feishu_mappings_created_at
                ON feishu_message_mappings(created_at)
                """
            )
            self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(feishu_message_mappings)")
        }
        migrations = {
            "tg_sender_id": "INTEGER NOT NULL DEFAULT 0",
            "tg_sender_username": "TEXT NOT NULL DEFAULT ''",
            "tg_sender_name": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in migrations.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE feishu_message_mappings ADD COLUMN {name} {ddl}"
                )

    def save(
        self,
        feishu_message_id: str,
        tg_chat_id: int,
        tg_msg_id: int,
        chat_title: str = "",
        *,
        tg_sender_id: int = 0,
        tg_sender_username: str = "",
        tg_sender_name: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO feishu_message_mappings
                (
                    feishu_message_id,
                    tg_chat_id,
                    tg_msg_id,
                    chat_title,
                    tg_sender_id,
                    tg_sender_username,
                    tg_sender_name,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feishu_message_id,
                    tg_chat_id,
                    tg_msg_id,
                    chat_title,
                    tg_sender_id,
                    tg_sender_username,
                    tg_sender_name,
                    time.time(),
                ),
            )

    def get(self, feishu_message_id: str) -> TgMessageRef | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    tg_chat_id,
                    tg_msg_id,
                    chat_title,
                    tg_sender_id,
                    tg_sender_username,
                    tg_sender_name
                FROM feishu_message_mappings
                WHERE feishu_message_id = ?
                """,
                (feishu_message_id,),
            ).fetchone()
        if row is None:
            return None
        return TgMessageRef(
            tg_chat_id=row["tg_chat_id"],
            tg_msg_id=row["tg_msg_id"],
            chat_title=row["chat_title"] or "",
            tg_sender_id=row["tg_sender_id"] or 0,
            tg_sender_username=row["tg_sender_username"] or "",
            tg_sender_name=row["tg_sender_name"] or "",
        )

    def cleanup_old(self) -> int:
        cutoff = time.time() - self.max_age_days * 86400
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM feishu_message_mappings WHERE created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount
