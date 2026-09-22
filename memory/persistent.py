"""Versioned SQLite memory with conservative secret refusal."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|access[_-]?token|secret)\s*[:=]"),
    re.compile(r"\b(?:sk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"),
)


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: str
    content: str
    metadata: dict[str, Any]
    importance: int
    created_at: str
    updated_at: str


class MemoryRefused(ValueError):
    pass


class SQLiteMemory:
    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = RLock()
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError("Memory database was created by a newer Cato version.")
        if version == 0:
            with self._connection:
                self._connection.execute(
                    """CREATE TABLE memories (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
                    metadata TEXT NOT NULL, importance INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
                )
                self._connection.execute(
                    "CREATE INDEX memories_kind_idx ON memories(kind)"
                )
                self._connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            version = 1
        if version < 2:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE VIRTUAL TABLE memories_fts USING fts5(
                      content, kind, content='memories', content_rowid='rowid');
                    CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
                      INSERT INTO memories_fts(rowid,content,kind)
                      VALUES(new.rowid,new.content,new.kind);
                    END;
                    CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                      INSERT INTO memories_fts(memories_fts,rowid,content,kind)
                      VALUES('delete',old.rowid,old.content,old.kind);
                    END;
                    CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                      INSERT INTO memories_fts(memories_fts,rowid,content,kind)
                      VALUES('delete',old.rowid,old.content,old.kind);
                      INSERT INTO memories_fts(rowid,content,kind)
                      VALUES(new.rowid,new.content,new.kind);
                    END;
                    INSERT INTO memories_fts(memories_fts) VALUES('rebuild');
                    PRAGMA user_version=2;
                    """
                )

    def store(
        self,
        kind: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        importance: int = 5,
    ) -> MemoryRecord:
        normalized = content.strip()
        if not normalized:
            raise MemoryRefused("Empty memories are not stored.")
        if contains_secret(normalized):
            raise MemoryRefused("Potential secrets cannot be stored in memory.")
        now = datetime.now(UTC).isoformat()
        record = MemoryRecord(
            uuid4().hex,
            kind.strip().lower() or "fact",
            normalized[:10_000],
            metadata or {},
            max(1, min(10, importance)),
            now,
            now,
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.kind,
                    record.content,
                    json.dumps(record.metadata, sort_keys=True),
                    record.importance,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
        return self._record(row) if row else None

    def search(
        self, query: str, *, kind: str | None = None, limit: int = 10
    ) -> list[MemoryRecord]:
        terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query)}
        if terms:
            match = " OR ".join(f'"{term}"' for term in sorted(terms))
            sql = """SELECT memories.* FROM memories_fts
            JOIN memories ON memories.rowid=memories_fts.rowid
            WHERE memories_fts MATCH ?"""
            parameters: list[Any] = [match]
            if kind:
                sql += " AND memories.kind=?"
                parameters.append(kind.lower())
            sql += " ORDER BY bm25(memories_fts), memories.importance DESC LIMIT ?"
            parameters.append(max(1, min(limit, 100)))
            with self._lock:
                rows = self._connection.execute(sql, parameters).fetchall()
            return [self._record(row) for row in rows]
        sql = "SELECT * FROM memories"
        parameters: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            parameters.append(kind.lower())
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        scored = []
        for row in rows:
            record = self._record(row)
            words = set(re.findall(r"[A-Za-z0-9_]+", record.content.lower()))
            score = len(terms & words) * 10 + record.importance
            if not terms or score > record.importance:
                scored.append((score, record.updated_at, record))
        scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[: max(1, min(limit, 100))]]

    def list(self, *, kind: str | None = None, limit: int = 50) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories"
        parameters: tuple[Any, ...] = ()
        if kind:
            sql += " WHERE kind=?"
            parameters = (kind.lower(),)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(
                sql, (*parameters, max(1, min(limit, 500)))
            ).fetchall()
        return [self._record(row) for row in rows]

    def delete(self, memory_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE id=?", (memory_id,)
            )
        return cursor.rowcount > 0

    def export(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self.list(limit=500)]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            row["id"],
            row["kind"],
            row["content"],
            json.loads(row["metadata"]),
            row["importance"],
            row["created_at"],
            row["updated_at"],
        )


def contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)
