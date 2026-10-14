"""`orgpilot team import / show` end to end (ADR-0009 §6)."""

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orgpilot import cli
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database

EXAMPLE = Path(__file__).resolve().parents[1] / "team.example.yaml"
REAL_TEAM_DIRECTORY = cli._team_directory
DIRECTORY = {
    "wang@example.com": "ou_wang",
    "alice@example.com": "ou_alice",
    "bob@example.com": "ou_bob",
}


@pytest.fixture
def team_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "orgpilot.db"
    monkeypatch.setenv("ORGPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ORGPILOT_FEISHU_PROJECT_ID", "feishu-project")
    monkeypatch.delenv("ORGPILOT_DEMO_MODE", raising=False)
    monkeypatch.setattr(
        cli, "_team_directory", lambda settings: MockFeishuClient(email_directory=DIRECTORY)
    )
    roster = tmp_path / "team.yaml"
    shutil.copy(EXAMPLE, roster)
    return roster


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["orgpilot", "team", *args])
    return cli.main()


async def _service() -> tuple[Database, GatewayService]:
    from orgpilot.config import OrgPilotSettings

    db = Database(OrgPilotSettings.from_env().database_url)
    await db.init_db()
    return db, GatewayService(db)


def test_dry_run_prints_plan_and_writes_nothing(
    team_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "import", str(team_env), "--dry-run") == 0

    out = capsys.readouterr().out
    assert "+ member  wang  王鹏飞 (pm)" in out
    assert "+ task    task-checkout  收银台前端结账  owner=bob" in out
    assert "将写入 5 个事件，跳过 0 项（dry-run，未写入）" in out

    assert _run(monkeypatch, "show") == 0
    assert "成员（0）" in capsys.readouterr().out


def test_import_then_reimport_then_show(
    team_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "import", str(team_env)) == 0
    assert "已写入 5 个事件" in capsys.readouterr().out

    assert _run(monkeypatch, "import", str(team_env)) == 0
    assert "没有需要写入的变更" in capsys.readouterr().out

    assert _run(monkeypatch, "show") == 0
    out = capsys.readouterr().out
    assert "成员（3）" in out
    assert "王鹏飞  pm" in out
    assert "task-checkout  收银台前端结账  todo  截止 10-27 18:00  负责人 前端工程师 Bob" in out
    assert "依赖 task-payment" in out


def test_import_rejects_wrong_project_and_invalid_files(
    team_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "import", str(team_env), "--project", "other") == 1
    assert "team.yaml is for project 'feishu-project'" in capsys.readouterr().err

    team_env.write_text(
        team_env.read_text(encoding="utf-8").replace("2026-10-24 18:00", "2026-10-24"),
        encoding="utf-8",
    )
    assert _run(monkeypatch, "import", str(team_env)) == 1
    err = capsys.readouterr().err
    assert "导入失败，未写入任何事件" in err
    assert "needs a time of day" in err


def test_import_reports_unresolved_emails(
    team_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "_team_directory", lambda settings: MockFeishuClient(email_directory={})
    )

    assert _run(monkeypatch, "import", str(team_env)) == 1
    assert "'alice' <alice@example.com>: 飞书没有返回 open_id" in capsys.readouterr().err


def test_import_requires_feishu_credentials(
    team_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Restore only the real directory factory. monkeypatch.undo() would also undo
    # the conftest guard and let a developer's .env supply real credentials.
    monkeypatch.setattr(cli, "_team_directory", REAL_TEAM_DIRECTORY)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    with pytest.raises(SystemExit, match="FEISHU_APP_ID and FEISHU_APP_SECRET"):
        _run(monkeypatch, "import", str(team_env))


async def test_imported_members_chat_and_pm_syncs_their_owners(
    team_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P-1 goal: after an import, a second person is no longer turned away."""
    monkeypatch.setattr(sys, "argv", ["orgpilot", "team", "import", str(team_env)])
    # cli.main drives its own event loop, so run it outside this test's loop.
    import asyncio

    assert await asyncio.to_thread(cli.main) == 0

    db, service = await _service()
    try:
        handler = FeishuWebhookHandler(service=service, project_id="feishu-project")
        now = datetime.now(UTC)

        def message(open_id: str, text: str, message_id: str) -> dict:
            return {
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": open_id}},
                    "message": {
                        "message_id": message_id,
                        "message_type": "text",
                        "chat_type": "p2p",
                        "content": json.dumps({"text": text}),
                        "create_time": str(int(now.timestamp() * 1000)),
                    },
                },
            }

        bob = await handler.handle_event(message("ou_bob", "/帮助", "om_bob"))
        assert bob["msg"] != "sender_not_registered"

        stranger = await handler.handle_event(message("ou_stranger", "你好", "om_x"))
        assert stranger["msg"] == "sender_not_registered"

        sync = await handler.handle_event(message("ou_wang", "/同步", "om_sync"))
        assert sync["msg"] == "sync_session_started"
        coordinator = await service.get_sync_coordinator("feishu-project")
        session = coordinator.get_active_session("feishu-project", now=now)
        assert session is not None
        assert set(session.member_probes) == {"ou_alice", "ou_bob"}
    finally:
        await db.close()


def test_import_reports_feishu_api_errors_without_traceback(
    team_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class DeniedDirectory:
        async def batch_get_open_ids(self, emails=(), mobiles=()) -> dict[str, str]:
            raise RuntimeError("Feishu batch get user id failed: Access denied")

    monkeypatch.setattr(cli, "_team_directory", lambda settings: DeniedDirectory())

    assert _run(monkeypatch, "import", str(team_env)) == 1
    err = capsys.readouterr().err
    assert "调用飞书接口失败：Feishu batch get user id failed: Access denied" in err
    assert "contact:user.id:readonly" in err
