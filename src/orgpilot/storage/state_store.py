"""Persistent state, case ledger, and approval store backed by SQL."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from orgpilot.domain.models import ApprovalRequest, CoordinationCase, OrgState
from orgpilot.domain.sync_models import SyncSession
from orgpilot.storage.database import Database
from orgpilot.storage.models import (
    ApprovalRecord,
    CaseRecord,
    StateSnapshotRecord,
    SyncSessionRecord,
)


class SqlStateStore:
    """Async manager for persisting and loading projected state, cases, and approvals."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def save_state(self, state: OrgState) -> None:
        """Persists a complete OrgState snapshot."""
        now = datetime.now(UTC)
        state_dict = state.model_dump(mode="json")
        async with self.db.session() as session:
            stmt = select(StateSnapshotRecord).where(
                StateSnapshotRecord.project_id == state.project_id
            )
            res = await session.execute(stmt)
            rec = res.scalar_one_or_none()
            if rec is None:
                rec = StateSnapshotRecord(
                    project_id=state.project_id,
                    state_json=json.dumps(state_dict, default=str),
                    updated_at=now,
                )
                session.add(rec)
            else:
                rec.state_json = json.dumps(state_dict, default=str)
                rec.updated_at = now

    async def load_state(self, project_id: str) -> OrgState | None:
        """Loads an OrgState snapshot if one exists."""
        async with self.db.session() as session:
            stmt = select(StateSnapshotRecord).where(StateSnapshotRecord.project_id == project_id)
            res = await session.execute(stmt)
            rec = res.scalar_one_or_none()
            if rec is None:
                return None
            return OrgState.model_validate_json(rec.state_json)

    async def save_cases(self, project_id: str, cases: list[CoordinationCase]) -> None:
        """Saves a collection of CoordinationCase objects."""
        now = datetime.now(UTC)
        async with self.db.session() as session:
            for case in cases:
                stmt = select(CaseRecord).where(
                    CaseRecord.project_id == project_id,
                    CaseRecord.case_id == case.case_id,
                )
                res = await session.execute(stmt)
                rec = res.scalar_one_or_none()
                case_dict = case.model_dump(mode="json")
                if rec is None:
                    rec = CaseRecord(
                        case_id=case.case_id,
                        project_id=project_id,
                        source_task_id=case.source_task_id,
                        status=case.status.value,
                        waiting_for=case.waiting_for,
                        waiting_until=case.waiting_until,
                        round_count=case.round_count,
                        terminal_reason=case.terminal_reason,
                        data_json=json.dumps(case_dict, default=str),
                        updated_at=now,
                    )
                    session.add(rec)
                else:
                    rec.status = case.status.value
                    rec.waiting_for = case.waiting_for
                    rec.waiting_until = case.waiting_until
                    rec.round_count = case.round_count
                    rec.terminal_reason = case.terminal_reason
                    rec.data_json = json.dumps(case_dict, default=str)
                    rec.updated_at = now

    async def load_cases(self, project_id: str) -> list[CoordinationCase]:
        """Loads all CoordinationCase objects for a project."""
        async with self.db.session() as session:
            stmt = select(CaseRecord).where(CaseRecord.project_id == project_id)
            res = await session.execute(stmt)
            records = res.scalars().all()
            return [CoordinationCase.model_validate_json(r.data_json) for r in records]

    async def save_approvals(self, project_id: str, requests: list[ApprovalRequest]) -> None:
        """Saves a collection of ApprovalRequest objects."""
        now = datetime.now(UTC)
        async with self.db.session() as session:
            for req in requests:
                stmt = select(ApprovalRecord).where(
                    ApprovalRecord.project_id == project_id,
                    ApprovalRecord.approval_id == req.approval_id,
                )
                res = await session.execute(stmt)
                rec = res.scalar_one_or_none()
                req_dict = req.model_dump(mode="json")
                if rec is None:
                    rec = ApprovalRecord(
                        approval_id=req.approval_id,
                        project_id=project_id,
                        case_id=req.case_id,
                        action_type=req.action_type.value,
                        approver_id=req.approver_id,
                        status=req.status.value,
                        data_json=json.dumps(req_dict, default=str),
                        consumed=req.consumed,
                        updated_at=now,
                    )
                    session.add(rec)
                else:
                    rec.status = req.status.value
                    rec.data_json = json.dumps(req_dict, default=str)
                    rec.consumed = req.consumed
                    rec.updated_at = now

    async def load_approvals(self, project_id: str) -> list[ApprovalRequest]:
        """Loads all ApprovalRequest objects for a project."""
        async with self.db.session() as session:
            stmt = select(ApprovalRecord).where(ApprovalRecord.project_id == project_id)
            res = await session.execute(stmt)
            records = res.scalars().all()
            return [ApprovalRequest.model_validate_json(r.data_json) for r in records]

    async def save_sync_sessions(self, project_id: str, sessions: list[SyncSession]) -> None:
        """Upserts progress sync sessions for a project."""
        now = datetime.now(UTC)
        async with self.db.session() as session:
            for sync in sessions:
                stmt = select(SyncSessionRecord).where(
                    SyncSessionRecord.session_id == sync.session_id
                )
                res = await session.execute(stmt)
                rec = res.scalar_one_or_none()
                data_json = json.dumps(sync.model_dump(mode="json"), default=str)
                if rec is None:
                    session.add(
                        SyncSessionRecord(
                            session_id=sync.session_id,
                            project_id=project_id,
                            status=sync.status.value,
                            initiated_by=sync.initiated_by,
                            data_json=data_json,
                            updated_at=now,
                        )
                    )
                else:
                    rec.status = sync.status.value
                    rec.data_json = data_json
                    rec.updated_at = now

    async def load_sync_sessions(self, project_id: str) -> list[SyncSession]:
        """Loads all progress sync sessions recorded for a project."""
        async with self.db.session() as session:
            stmt = select(SyncSessionRecord).where(SyncSessionRecord.project_id == project_id)
            res = await session.execute(stmt)
            records = res.scalars().all()
            return [SyncSession.model_validate_json(r.data_json) for r in records]
