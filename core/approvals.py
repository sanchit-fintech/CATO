"""Task-bound, expiring, one-use durable action approvals."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from memory.persistent import contains_secret


def _fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    encoded = json.dumps([tool, arguments], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _contains_nested_secret(value: Any) -> bool:
    if isinstance(value, str):
        return contains_secret(value)
    if isinstance(value, dict):
        return any(_contains_nested_secret(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_nested_secret(item) for item in value)
    return False


@dataclass
class PendingApproval:
    id: str
    session_id: str
    tool: str
    arguments: dict[str, Any]
    request: str
    summary: str
    risk: str
    expires_at: datetime
    status: Literal["pending", "used", "denied", "expired"] = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    task_id: str | None = None
    action_fingerprint: str = ""
    durable: bool = False

    def public(self) -> dict[str, str]:
        return {
            "approval_id": self.id,
            "summary": self.summary,
            "risk": self.risk,
            "expires_at": self.expires_at.isoformat(),
        }

    def redact(self) -> None:
        self.arguments.clear()
        self.request = ""


@dataclass(frozen=True)
class ApprovalDecision:
    ok: bool
    code: str
    approval: PendingApproval | None = None


class ApprovalStore:
    SCHEMA_VERSION = 1

    def __init__(
        self, *, ttl_seconds: int = 300, path: str | Path | None = None
    ) -> None:
        self.ttl_seconds = max(1, ttl_seconds)
        self.path = Path(path).expanduser() if path is not None else None
        self._items: dict[str, PendingApproval] = {}
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.path, timeout=5.0, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._migrate()

    def _migrate(self) -> None:
        assert self._connection is not None
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError("Approval database was created by newer Cato.")
        if version == 0:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE approvals (
                      id TEXT PRIMARY KEY, task_id TEXT, session_id TEXT NOT NULL,
                      tool_name TEXT NOT NULL, arguments TEXT NOT NULL,
                      request TEXT NOT NULL, summary TEXT NOT NULL,
                      risk_level TEXT NOT NULL, action_fingerprint TEXT NOT NULL,
                      state TEXT NOT NULL, created_at TEXT NOT NULL,
                      expires_at TEXT NOT NULL, resolved_at TEXT);
                    CREATE INDEX approvals_task_state_idx
                      ON approvals(task_id,state,created_at);
                    PRAGMA user_version=1;
                    """
                )

    def create(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        request: str,
        summary: str,
        risk: str,
        task_id: str | None = None,
    ) -> PendingApproval:
        durable = (
            self._connection is not None
            and not _contains_nested_secret(arguments)
            and not contains_secret(request)
        )
        approval = PendingApproval(
            id=uuid4().hex,
            session_id=session_id,
            tool=tool,
            arguments=dict(arguments),
            request=request,
            summary=summary,
            risk=risk,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.ttl_seconds),
            task_id=task_id,
            action_fingerprint=_fingerprint(tool, arguments),
            durable=durable,
        )
        with self._lock:
            self._items[approval.id] = approval
            if durable:
                self._insert(approval)
        return approval

    def _insert(self, approval: PendingApproval) -> None:
        assert self._connection is not None
        with self._connection:
            self._connection.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    approval.id,
                    approval.task_id,
                    approval.session_id,
                    approval.tool,
                    json.dumps(approval.arguments, sort_keys=True),
                    approval.request,
                    approval.summary,
                    approval.risk,
                    approval.action_fingerprint,
                    approval.status,
                    approval.created_at.isoformat(),
                    approval.expires_at.isoformat(),
                    None,
                ),
            )

    def approve(self, approval_id: str, session_id: str) -> ApprovalDecision:
        with self._lock:
            decision = self._validate(approval_id, session_id)
            if not decision.ok or decision.approval is None:
                return decision
            stored = decision.approval
            consumed = replace(
                stored,
                arguments=dict(stored.arguments),
                request=stored.request,
                status="used",
            )
            stored.status = "used"
            self._resolve(stored, "used")
            stored.redact()
            return ApprovalDecision(True, "approved", consumed)

    def deny(self, approval_id: str, session_id: str) -> ApprovalDecision:
        with self._lock:
            decision = self._validate(approval_id, session_id)
            if not decision.ok or decision.approval is None:
                return decision
            decision.approval.status = "denied"
            self._resolve(decision.approval, "denied")
            decision.approval.redact()
            return ApprovalDecision(True, "denied", decision.approval)

    def _validate(self, approval_id: str, session_id: str) -> ApprovalDecision:
        approval = self._get(approval_id)
        if approval is None:
            return ApprovalDecision(False, "approval_not_found")
        if approval.session_id != session_id:
            return ApprovalDecision(False, "approval_session_mismatch")
        if approval.status != "pending":
            return ApprovalDecision(False, f"approval_{approval.status}")
        if datetime.now(UTC) >= approval.expires_at:
            approval.status = "expired"
            self._resolve(approval, "expired")
            approval.redact()
            return ApprovalDecision(False, "approval_expired")
        if approval.action_fingerprint != _fingerprint(
            approval.tool, approval.arguments
        ):
            return ApprovalDecision(False, "approval_action_mismatch")
        return ApprovalDecision(True, "pending", approval)

    def _get(self, approval_id: str) -> PendingApproval | None:
        if approval_id in self._items:
            return self._items[approval_id]
        if self._connection is None:
            return None
        row = self._connection.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        approval = PendingApproval(
            row["id"],
            row["session_id"],
            row["tool_name"],
            json.loads(row["arguments"]),
            row["request"],
            row["summary"],
            row["risk_level"],
            datetime.fromisoformat(row["expires_at"]),
            row["state"],
            datetime.fromisoformat(row["created_at"]),
            row["task_id"],
            row["action_fingerprint"],
            True,
        )
        self._items[approval.id] = approval
        return approval

    def _resolve(self, approval: PendingApproval, state: str) -> None:
        if not approval.durable or self._connection is None:
            return
        with self._connection:
            self._connection.execute(
                """UPDATE approvals SET state=?,resolved_at=?,arguments='{}',request=''
                WHERE id=? AND state='pending'""",
                (state, datetime.now(UTC).isoformat(), approval.id),
            )
