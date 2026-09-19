"""Minimal, secret-conscious in-memory session store."""

from __future__ import annotations

from typing import Protocol

from core.session import Session


class MemoryStore(Protocol):
    def create(self) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...
    def clear(self, session_id: str) -> bool: ...


class InMemoryStore:
    def __init__(self, *, history_limit: int = 50) -> None:
        self._sessions: dict[str, Session] = {}
        self.history_limit = history_limit

    def create(self) -> Session:
        session = Session(history_limit=self.history_limit)
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def save(self, session: Session) -> None:
        self._sessions[session.id] = session

    def clear(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None
