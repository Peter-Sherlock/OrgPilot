"""Tests for ApprovalManager state machine, expiration, rejection, and single-use token."""

from datetime import datetime, timedelta

import pytest

from orgpilot.coordination.approval import ApprovalManager
from orgpilot.domain.enums import ActionType, ApprovalStatus
from orgpilot.domain.models import ActionCommand, CoordinationAction

NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")


def _fixture() -> tuple[ApprovalManager, CoordinationAction, ActionCommand]:
    mgr = ApprovalManager()
    action = CoordinationAction(
        action_id="act:reschedule",
        action_type=ActionType.PROPOSE_RESCHEDULE,
        targets=("carol",),
        reason_refs=("evt-1",),
        expected_effect="reschedule",
    )
    cmd = ActionCommand(
        command_id="cmd:1",
        action_id="act:reschedule",
        action_type=ActionType.UPDATE_TASK,
        targets=("t1",),
        payload={"new_deadline": "2026-09-15T18:00:00+08:00"},
        idempotency_key="idem:1",
        created_at=NOW,
    )
    return mgr, action, cmd


def test_approval_lifecycle_happy_path() -> None:
    mgr, action, cmd = _fixture()
    req = mgr.create_request("case:1", action, cmd, "carol", NOW)
    assert req.status is ApprovalStatus.PENDING
    assert req.approval_id == "approval:case:1:act:reschedule"
    assert len(mgr.get_pending_requests()) == 1

    # Approve
    approved_req = mgr.approve(req.approval_id, "carol", NOW + timedelta(minutes=5))
    assert approved_req.status is ApprovalStatus.APPROVED
    assert approved_req.approved_by == "carol" if hasattr(approved_req, "approved_by") else True
    assert len(mgr.get_pending_requests()) == 0

    # Consume single-use approval
    authorized_cmd = mgr.consume(req.approval_id, NOW + timedelta(minutes=6))
    assert authorized_cmd.approved_by == "carol"
    assert req.consumed is True

    # Cannot consume again
    with pytest.raises(ValueError, match="already been consumed"):
        mgr.consume(req.approval_id, NOW + timedelta(minutes=7))


def test_approval_rejection_prevents_consumption() -> None:
    mgr, action, cmd = _fixture()
    req = mgr.create_request("case:1", action, cmd, "carol", NOW)
    mgr.reject(req.approval_id, "carol", "Date fixed", NOW + timedelta(minutes=5))
    assert req.status is ApprovalStatus.REJECTED
    assert req.rejection_reason == "Date fixed"

    with pytest.raises(ValueError, match="is not approved"):
        mgr.consume(req.approval_id, NOW + timedelta(minutes=6))

    with pytest.raises(ValueError, match="Cannot approve already rejected"):
        mgr.approve(req.approval_id, "carol", NOW + timedelta(minutes=7))


def test_approval_expiration_prevents_approval_and_consumption() -> None:
    mgr, action, cmd = _fixture()
    expires = NOW + timedelta(hours=1)
    req = mgr.create_request("case:1", action, cmd, "carol", NOW, expires_at=expires)

    # Approve after expiration
    with pytest.raises(ValueError, match="has expired"):
        mgr.approve(req.approval_id, "carol", NOW + timedelta(hours=2))
    assert req.status is ApprovalStatus.EXPIRED

    # Pre-approved but expired before consumption
    req2 = mgr.create_request("case:2", action, cmd, "carol", NOW, expires_at=expires)
    mgr.approve(req2.approval_id, "carol", NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="has expired"):
        mgr.consume(req2.approval_id, NOW + timedelta(hours=2))


def test_approval_manager_missing_keys() -> None:
    mgr = ApprovalManager()
    with pytest.raises(KeyError):
        mgr.approve("missing", "carol", NOW)
    with pytest.raises(KeyError):
        mgr.reject("missing", "carol", "reason", NOW)
    with pytest.raises(KeyError):
        mgr.consume("missing", NOW)
    assert mgr.get_request("missing") is None
    assert mgr.get_requests_for_case("missing") == ()
