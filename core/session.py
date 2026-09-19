"""Bounded conversation and task state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass
class Message:
    role: str
    content: Any
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid4().hex)
    messages: list[Message] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    iteration_count: int = 0
    status: str = "idle"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    history_limit: int = 50

    def add(self, role: str, content: Any) -> None:
        self.messages.append(Message(role, content))
        self.messages[:] = self.messages[-self.history_limit :]
        self.updated_at = datetime.now(UTC)
