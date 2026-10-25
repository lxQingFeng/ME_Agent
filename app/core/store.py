"""SQLite 存储层(console.db):消息/会话/kv/去重。"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL, chat_id TEXT, chat_type TEXT, sender_id TEXT, sender_name TEXT,
              role TEXT, content TEXT, segments TEXT, origin TEXT,
              importance INTEGER DEFAULT 0, retained INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS dedup(hash TEXT PRIMARY KEY, ts REAL);
            """
        )
        c.commit()

    def exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, args)
        self._conn.commit()
        return cur

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, args).fetchall())

    # ---- kv ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        return json.loads(row[0]["value"])

    def kv_set(self, key: str, value: Any) -> None:
        self.exec(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---- messages ----
    def append_message(self, *, ts: float, chat_id: str, chat_type: str, sender_id: str,
                       sender_name: str, role: str, content: str, segments: list,
                       origin: str, importance: int = 0) -> int:
        cur = self.exec(
            "INSERT INTO messages(ts,chat_id,chat_type,sender_id,sender_name,role,content,segments,origin,importance)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, chat_id, chat_type, sender_id, sender_name, role, content,
             json.dumps(segments, ensure_ascii=False), origin, importance),
        )
        return cur.lastrowid

    def recent_messages(self, chat_id: str, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.query(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )
        return list(reversed(rows))

    def chats(self) -> list[dict]:
        rows = self.query(
            "SELECT chat_id, chat_type, COUNT(*) n, MAX(ts) last_ts FROM messages GROUP BY chat_id"
        )
        return [dict(r) for r in rows]

    def mark_retained(self, chat_id: str, upto_id: int) -> None:
        self.exec("UPDATE messages SET retained=1 WHERE chat_id=? AND id<=?", (chat_id, upto_id))

    # ---- dedup ----
    def dedup_check(self, h: str, window_s: float) -> bool:
        row = self.query("SELECT ts FROM dedup WHERE hash=?", (h,))
        now = time.time()
        if row and now - row[0]["ts"] < window_s:
            return True
        self.exec("INSERT OR REPLACE INTO dedup(hash,ts) VALUES(?,?)", (h, now))
        return False

    def close(self) -> None:
        self._conn.close()
