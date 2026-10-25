"""事件溯源日志(state/world.db):append-only;快照存 state/world.json。"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

class EventLog:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              type TEXT NOT NULL,
              from_act TEXT, to_act TEXT, location TEXT, reason TEXT,
              meta TEXT, version INTEGER);
            CREATE INDEX IF NOT EXISTS idx_evt_ver ON events(version);
            """
        )
        self._conn.commit()

    def append(self, *, ts, type: str, from_act: str | None = None,
               to_act: str | None = None, location: str | None = None,
               reason: str | None = None, meta: dict | None = None,
               version: int | None = None) -> int:
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        cur = self._conn.execute(
            "INSERT INTO events(ts,type,from_act,to_act,location,reason,meta,version) VALUES(?,?,?,?,?,?,?,?)",
            (ts.isoformat(), type, from_act, to_act, location, reason,
             json.dumps(meta or {}, ensure_ascii=False), version),
        )
        self._conn.commit()
        return cur.lastrowid

    def since(self, after_id: int, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def tail(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def all(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def last_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) m FROM events").fetchone()
        return row["m"] or 0

    def close(self) -> None:
        self._conn.close()
