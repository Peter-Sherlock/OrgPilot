"""A projector fix must also take effect when upgrading an existing database."""

import json
from datetime import UTC, datetime

import pytest

from orgpilot.domain.enums import ClaimStatus, HealthStatus
from orgpilot.gateway.service import GatewayService
from orgpilot.state.projector import OrgProjector
from orgpilot.storage.database import Database
from orgpilot.storage.models import StateSnapshotRecord
from orgpilot.storage.state_store import SqlStateStore
from tests.test_completed_task_risk import _events


@pytest.mark.parametrize("keep_healthy", [False, True])
async def test_legacy_snapshot_rebuilds_completed_risk_from_log(
    tmp_path, monkeypatch, keep_healthy
):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'upgrade.db').as_posix()}")
    await db.init_db()
    try:
        service = GatewayService(db)
        events = _events("delayed", keep_healthy)
        await service.ingest_raw_events("test-project", [e.model_dump(mode="json") for e in events])
        # Historical snapshot format on main a8c577d, before the D6 fix.
        legacy = OrgProjector("test-project").replay(events)
        legacy.tasks["api"].health_status = HealthStatus.DELAYED
        legacy.tasks["api"].health_conflict = keep_healthy
        legacy.health_claims["claim:risk:task_health"].status = ClaimStatus.ACTIVE
        legacy.health_claims["claim:risk:task_health"].superseded_by = None
        async with db.session() as session:
            session.add(
                StateSnapshotRecord(
                    project_id="test-project",
                    state_json=legacy.model_dump_json(),
                    updated_at=datetime.now(UTC),
                )
            )

        restored = await service.get_or_replay_agent("test-project")
        expected = HealthStatus.ON_TRACK if keep_healthy else HealthStatus.UNKNOWN
        assert restored.state.tasks["api"].health_status is expected
        assert not restored.state.tasks["api"].health_conflict
        assert (
            restored.state.health_claims["claim:risk:task_health"].status is ClaimStatus.SUPERSEDED
        )
        assert len(await service.event_store.get_events("test-project")) == len(events)
        await service.save_agent_state(restored)

        def unexpected_replay(*args):
            raise AssertionError("A current compatible snapshot should remain usable")

        monkeypatch.setattr(OrgProjector, "apply", unexpected_replay)
        cached = await GatewayService(db).get_or_replay_agent("test-project")
        assert cached.state == restored.state
    finally:
        await db.close()


async def test_unknown_projection_revision_is_a_cache_miss():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    try:
        async with db.session() as session:
            session.add(
                StateSnapshotRecord(
                    project_id="test-project",
                    state_json=json.dumps({"projection_version": 999, "state": {}}),
                    updated_at=datetime.now(UTC),
                )
            )
        assert await SqlStateStore(db).load_state("test-project") is None
    finally:
        await db.close()
