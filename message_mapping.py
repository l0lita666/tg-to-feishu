"""飞书消息 ID 与 Telegram 消息的映射存储。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


def tg_chat_id_variants(chat_id: int) -> list[int]:
    """TG 群聊 ID 常见两种格式：-5289292369 与 -1005289292369。"""
    variants: list[int] = [chat_id]
    text = str(chat_id)
    if text.startswith("-100") and len(text) > 4:
        short = int(f"-{text[4:]}")
        if short not in variants:
            variants.append(short)
    elif chat_id < 0:
        long_id = int(f"-100{abs(chat_id)}")
        if long_id not in variants:
            variants.append(long_id)
    return variants


@dataclass(frozen=True)
class TgMessageRef:
    tg_chat_id: int
    tg_msg_id: int
    chat_title: str = ""
    tg_sender_id: int = 0
    tg_sender_username: str = ""
    tg_sender_name: str = ""


@dataclass(frozen=True)
class CardSnapshot:
    feishu_message_id: str
    tg_chat_id: int
    tg_msg_id: int
    time_str: str
    info: str
    body: str
    image_keys: list[str]
    avatar_key: str
    is_outgoing: bool
    is_group: bool
    read_status_json: str = ""
    tg_read_synced: bool = False


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_card_snapshots (
                    feishu_message_id TEXT PRIMARY KEY,
                    tg_chat_id INTEGER NOT NULL,
                    tg_msg_id INTEGER NOT NULL,
                    time_str TEXT NOT NULL DEFAULT '',
                    info TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    image_keys TEXT NOT NULL DEFAULT '[]',
                    avatar_key TEXT NOT NULL DEFAULT '',
                    is_outgoing INTEGER NOT NULL DEFAULT 0,
                    is_group INTEGER NOT NULL DEFAULT 0,
                    read_status_json TEXT NOT NULL DEFAULT '',
                    tg_read_synced INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_card_snapshots_tg
                ON feishu_card_snapshots(tg_chat_id, tg_msg_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_card_snapshots_created_at
                ON feishu_card_snapshots(created_at)
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

    def save_card_snapshot(
        self,
        feishu_message_id: str,
        *,
        tg_chat_id: int,
        tg_msg_id: int,
        time_str: str,
        info: str,
        body: str,
        image_keys: list[str],
        avatar_key: str = "",
        is_outgoing: bool = False,
        is_group: bool = False,
        read_status_json: str = "",
        tg_read_synced: bool = False,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO feishu_card_snapshots
                (
                    feishu_message_id,
                    tg_chat_id,
                    tg_msg_id,
                    time_str,
                    info,
                    body,
                    image_keys,
                    avatar_key,
                    is_outgoing,
                    is_group,
                    read_status_json,
                    tg_read_synced,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feishu_message_id,
                    tg_chat_id,
                    tg_msg_id,
                    time_str,
                    info,
                    body,
                    json.dumps(image_keys, ensure_ascii=False),
                    avatar_key,
                    1 if is_outgoing else 0,
                    1 if is_group else 0,
                    read_status_json,
                    1 if tg_read_synced else 0,
                    time.time(),
                ),
            )

    def _row_to_card_snapshot(self, row: sqlite3.Row) -> CardSnapshot:
        try:
            image_keys = json.loads(row["image_keys"] or "[]")
        except json.JSONDecodeError:
            image_keys = []
        if not isinstance(image_keys, list):
            image_keys = []
        return CardSnapshot(
            feishu_message_id=row["feishu_message_id"],
            tg_chat_id=row["tg_chat_id"],
            tg_msg_id=row["tg_msg_id"],
            time_str=row["time_str"] or "",
            info=row["info"] or "",
            body=row["body"] or "",
            image_keys=[str(key) for key in image_keys if str(key).strip()],
            avatar_key=row["avatar_key"] or "",
            is_outgoing=bool(row["is_outgoing"]),
            is_group=bool(row["is_group"]),
            read_status_json=row["read_status_json"] or "",
            tg_read_synced=bool(row["tg_read_synced"]),
        )

    def get_card_snapshot(self, feishu_message_id: str) -> CardSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM feishu_card_snapshots
                WHERE feishu_message_id = ?
                """,
                (feishu_message_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_card_snapshot(row)

    def get_card_by_tg(self, tg_chat_id: int, tg_msg_id: int) -> CardSnapshot | None:
        with self._connect() as conn:
            for cid in tg_chat_id_variants(tg_chat_id):
                row = conn.execute(
                    """
                    SELECT *
                    FROM feishu_card_snapshots
                    WHERE tg_chat_id = ? AND tg_msg_id = ?
                    """,
                    (cid, tg_msg_id),
                ).fetchone()
                if row is not None:
                    return self._row_to_card_snapshot(row)
        return None

    def list_outgoing_cards_up_to(self, tg_chat_id: int, max_msg_id: int) -> list[CardSnapshot]:
        seen: set[str] = set()
        snapshots: list[CardSnapshot] = []
        with self._connect() as conn:
            for cid in tg_chat_id_variants(tg_chat_id):
                rows = conn.execute(
                    """
                    SELECT *
                    FROM feishu_card_snapshots
                    WHERE tg_chat_id = ?
                      AND tg_msg_id <= ?
                      AND is_outgoing = 1
                    ORDER BY tg_msg_id ASC
                    """,
                    (cid, max_msg_id),
                ).fetchall()
                for row in rows:
                    feishu_message_id = row["feishu_message_id"]
                    if feishu_message_id in seen:
                        continue
                    seen.add(feishu_message_id)
                    snapshots.append(self._row_to_card_snapshot(row))
        snapshots.sort(key=lambda item: item.tg_msg_id)
        return snapshots

    def list_incoming_unsynced_tg_read(self, limit: int = 80) -> list[CardSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM feishu_card_snapshots
                WHERE is_outgoing = 0
                  AND tg_read_synced = 0
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_card_snapshot(row) for row in rows]

    def list_recent_outgoing_group_cards(self, limit: int = 40) -> list[CardSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM feishu_card_snapshots
                WHERE is_outgoing = 1
                  AND is_group = 1
                  AND created_at > ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (time.time() - 7 * 86400, limit),
            ).fetchall()
        return [self._row_to_card_snapshot(row) for row in rows]

    def list_recent_outgoing_private_cards(self, limit: int = 40) -> list[CardSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM feishu_card_snapshots
                WHERE is_outgoing = 1
                  AND is_group = 0
                  AND created_at > ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (time.time() - 7 * 86400, limit),
            ).fetchall()
        return [self._row_to_card_snapshot(row) for row in rows]

    def update_read_status(self, feishu_message_id: str, read_status_json: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE feishu_card_snapshots
                SET read_status_json = ?
                WHERE feishu_message_id = ?
                """,
                (read_status_json, feishu_message_id),
            )

    def mark_tg_read_synced(self, feishu_message_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE feishu_card_snapshots
                SET tg_read_synced = 1
                WHERE feishu_message_id = ?
                """,
                (feishu_message_id,),
            )

    def cleanup_old(self) -> int:
        cutoff = time.time() - self.max_age_days * 86400
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM feishu_message_mappings WHERE created_at < ?",
                (cutoff,),
            )
            removed = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM feishu_card_snapshots WHERE created_at < ?",
                (cutoff,),
            )
            return removed + cursor.rowcount
