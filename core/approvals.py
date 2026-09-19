"""Session-bound, expiring, one-time action approvals."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4


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
    def __init__(self, *, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = max(1, ttl_seconds)
        self._items: dict[str, PendingApproval] = {}

    def create(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        request: str,
        summary: str,
        risk: str,
    ) -> PendingApproval:
        approval = PendingApproval(
            id=uuid4().hex,
            session_id=session_id,
            tool=tool,
            arguments=dict(arguments),
            request=request,
            summary=summary,
            risk=risk,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.ttl_seconds),
        )
        self._items[approval.id] = approval
        return approval

    def approve(self, approval_id: str, session_id: str) -> ApprovalDecision:
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
        stored.redact()
        return ApprovalDecision(True, "approved", consumed)

    def deny(self, approval_id: str, session_id: str) -> ApprovalDecision:
        decision = self._validate(approval_id, session_id)
        if not decision.ok or decision.approval is None:
            return decision
        decision.approval.status = "denied"
        decision.approval.redact()
        return ApprovalDecision(True, "denied", decision.approval)

    def _validate(self, approval_id: str, session_id: str) -> ApprovalDecision:
        approval = self._items.get(approval_id)
        if approval is None:
            return ApprovalDecision(False, "approval_not_found")
        if approval.session_id != session_id:
            return ApprovalDecision(False, "approval_session_mismatch")
        if approval.status != "pending":
            return ApprovalDecision(False, f"approval_{approval.status}")
        if datetime.now(UTC) >= approval.expires_at:
            approval.status = "expired"
            approval.redact()
            return ApprovalDecision(False, "approval_expired")
        return ApprovalDecision(True, "pending", approval)
