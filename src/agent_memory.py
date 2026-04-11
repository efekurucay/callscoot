from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import callscoot


@dataclass
class CallerProfile:
    caller_id: str
    name: str | None = None
    tier: str | None = None
    notes: str | None = None


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        callscoot.ensure_dirs()
        self.path = path or (callscoot.STATE_DIR / "agent_memory.sqlite3")
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS caller_profiles (
                    caller_id TEXT PRIMARY KEY,
                    name TEXT,
                    tier TEXT,
                    notes TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS long_term_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    caller_id TEXT,
                    memory TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS call_summaries (
                    session_id TEXT PRIMARY KEY,
                    caller_id TEXT,
                    summary TEXT,
                    structured_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()

    def get_profile(self, caller_id: str | None) -> CallerProfile | None:
        if not caller_id:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT caller_id, name, tier, notes FROM caller_profiles WHERE caller_id = ?",
                (caller_id,),
            ).fetchone()
        if not row:
            return None
        return CallerProfile(caller_id=row["caller_id"], name=row["name"], tier=row["tier"], notes=row["notes"])

    def upsert_profile(self, caller_id: str, name: str | None = None, tier: str | None = None, notes: str | None = None) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO caller_profiles(caller_id, name, tier, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(caller_id) DO UPDATE SET
                    name = COALESCE(excluded.name, caller_profiles.name),
                    tier = COALESCE(excluded.tier, caller_profiles.tier),
                    notes = COALESCE(excluded.notes, caller_profiles.notes),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (caller_id, name, tier, notes),
            )
            conn.commit()

    def add_memory(self, caller_id: str, memory: str, metadata: dict[str, Any] | None = None) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO long_term_memories(caller_id, memory, metadata_json) VALUES (?, ?, ?)",
                (caller_id, memory, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.commit()

    def retrieve_memories(self, caller_id: str | None, limit: int = 5) -> list[dict[str, Any]]:
        if not caller_id:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT memory, metadata_json, created_at FROM long_term_memories WHERE caller_id = ? ORDER BY id DESC LIMIT ?",
                (caller_id, limit),
            ).fetchall()
        return [
            {
                "memory": row["memory"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_summary(self, session_id: str, caller_id: str | None, summary: str, structured: dict[str, Any] | None = None) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO call_summaries(session_id, caller_id, summary, structured_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    caller_id = excluded.caller_id,
                    summary = excluded.summary,
                    structured_json = excluded.structured_json
                """,
                (session_id, caller_id, summary, json.dumps(structured or {}, ensure_ascii=False)),
            )
            conn.commit()
