"""Public scenario loading and CLI behavior."""

import sys
from pathlib import Path

import pytest

from orgpilot import cli
from orgpilot.domain.errors import GroundTruthMismatch
from orgpilot.scenarios.evaluator import (
    assert_ground_truth,
    assert_scenario,
    evaluate_scenario,
)
from orgpilot.scenarios.loader import load_scenario
from orgpilot.scenarios.models import GroundTruth, ScenarioDefinition
from orgpilot.scenarios.runner import ScenarioRunner


def test_loader_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_scenario(path)


def test_loader_rejects_project_mismatch(tmp_path: Path) -> None:
    source = Path("evals/scenarios/03_workflow_health_separation.yaml").read_text(encoding="utf-8")
    path = tmp_path / "mismatch.yaml"
    path.write_text(
        source.replace("project_id: payment-sdk", "project_id: other-project", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project does not match"):
        load_scenario(path)


def test_loader_rejects_round_project_mismatch(tmp_path: Path) -> None:
    source = Path("evals/scenarios/m1_01_delay_inquiry_and_recovery.yaml").read_text(
        encoding="utf-8"
    )
    path = tmp_path / "m1_mismatch.yaml"
    path.write_text(
        source.replace("project_id: m1-delay-recovery", "project_id: other-project", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project does not match"):
        load_scenario(path)


def test_cli_replays_all_scenarios(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["orgpilot", "replay", "--all"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert output.count("[PASS]") == 9


def test_cli_replays_one_scenario(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = "evals/scenarios/01_delay_propagation.yaml"
    monkeypatch.setattr(sys, "argv", ["orgpilot", "replay", path])
    assert cli.main() == 0
    assert "[PASS] delay_propagation" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["orgpilot", "replay"],
        ["orgpilot", "replay", "evals/scenarios/01_delay_propagation.yaml", "--all"],
    ],
)
def test_cli_requires_one_scenario_selection(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit):
        cli.main()


def test_evaluator_raises_on_missing_ground_truth() -> None:
    scenario = ScenarioDefinition(
        schema_version=1,
        scenario_id="s",
        title="S",
        description="D",
        project_id="p",
        source_path=Path("s.yaml"),
        events=(),
        ground_truth=None,
        interactive_ground_truth=None,
    )
    runner = ScenarioRunner()
    result = runner.run(scenario)
    with pytest.raises(ValueError, match="has no declared ground truth"):
        evaluate_scenario(scenario, result)


def test_assert_ground_truth_raises_on_mismatch() -> None:
    path = Path("evals/scenarios/01_delay_propagation.yaml")
    scenario = load_scenario(path)
    result = ScenarioRunner().run(scenario)
    bad_gt = GroundTruth(event_count=999, member_count=0, task_count=0)
    with pytest.raises(GroundTruthMismatch, match="failed"):
        assert_ground_truth(result, bad_gt)


def test_assert_scenario_raises_on_mismatch() -> None:
    path = Path("evals/scenarios/01_delay_propagation.yaml")
    scenario = load_scenario(path)
    result = ScenarioRunner().run(scenario)
    bad_scenario = scenario.model_copy(
        update={"ground_truth": GroundTruth(event_count=999, member_count=0, task_count=0)}
    )
    with pytest.raises(GroundTruthMismatch, match="failed"):
        assert_scenario(bad_scenario, result)


def test_cli_eval_extraction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["orgpilot", "eval-extraction"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "[PASS] Natural Language Extraction Benchmark (mock, 34 samples):" in output
    assert "Intent Accuracy: 100.00%" in output


def test_cli_eval_extraction_missing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["orgpilot", "eval-extraction", "--dataset", "non_existent.yaml"]
    )
    with pytest.raises(SystemExit, match="dataset file not found"):
        cli.main()


def _set_safe_feishu_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGPILOT_COLLABORATION_ADAPTER", "feishu")
    monkeypatch.setenv("ORGPILOT_FEISHU_USE_WS", "true")
    monkeypatch.setenv("ORGPILOT_FEISHU_ALLOW_WRITES", "false")
    monkeypatch.setenv("ORGPILOT_DEMO_BOOTSTRAP", "false")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_must_not_appear")


def test_cli_feishu_preflight_is_offline_and_hides_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_safe_feishu_env(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["orgpilot", "feishu-preflight"])

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert "[PASS] Feishu preflight completed" in output
    assert "[SKIP] online auth" in output
    assert "cli_test" not in output
    assert "secret_must_not_appear" not in output


def test_cli_feishu_preflight_online_auth_hides_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from orgpilot.feishu.client import AsyncFeishuClient

    async def issue_token(_client: AsyncFeishuClient) -> str:
        return "tenant_token_must_not_appear"

    _set_safe_feishu_env(monkeypatch)
    monkeypatch.setattr(AsyncFeishuClient, "get_tenant_access_token", issue_token)
    monkeypatch.setattr(sys, "argv", ["orgpilot", "feishu-preflight", "--online-auth"])

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert "tenant token issued (value hidden; no write performed)" in output
    assert "tenant_token_must_not_appear" not in output


def test_cli_feishu_listener_requires_explicit_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_safe_feishu_env(monkeypatch)
    with pytest.raises(SystemExit, match="write gate is closed"):
        cli._start_feishu_ws()


@pytest.mark.parametrize(
    ("challenge", "project_id", "timeout_seconds", "confirmed", "error"),
    [
        ("long-enough-challenge", "feishu-l2-test", 30, False, "confirm-single-reply"),
        ("too-short", "feishu-l2-test", 30, True, "at least 12"),
        ("long-enough-challenge", "feishu-l2-test", 29, True, "between 30 and 300"),
        ("long-enough-challenge", "shared-project", 30, True, "must start"),
        ("long-enough-challenge", "feishu-l2-../../escape", 30, True, "safe unique suffix"),
        ("long-enough\nchallenge", "feishu-l2-test", 30, True, "single line"),
    ],
)
def test_cli_feishu_l2_rejects_unbounded_or_ambiguous_invocation(
    challenge: str,
    project_id: str,
    timeout_seconds: int,
    confirmed: bool,
    error: str,
) -> None:
    with pytest.raises(SystemExit, match=error):
        cli._feishu_l2_smoke(challenge, project_id, timeout_seconds, confirmed)


def test_cli_feishu_l2_requires_ambient_write_gate_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_safe_feishu_env(monkeypatch)
    monkeypatch.setenv("ORGPILOT_FEISHU_ALLOW_WRITES", "true")

    with pytest.raises(SystemExit, match="Ambient Feishu write gate must remain closed"):
        cli._feishu_l2_smoke(
            "long-enough-challenge",
            "feishu-l2-test",
            30,
            True,
        )


@pytest.mark.parametrize(
    ("mode", "expected_code", "expected_status"),
    [
        ("accepted", 0, "api_accepted"),
        ("timeout", 1, "not_attempted"),
        ("request_error", 1, "unknown"),
        ("connection_failure", 1, "not_attempted"),
        ("shutdown_failure", 1, "api_accepted"),
        ("shutdown_exception", 1, "api_accepted"),
    ],
)
def test_cli_l2_offline_lifecycle_and_safe_report(
    mode: str,
    expected_code: int,
    expected_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio
    import json

    from orgpilot.feishu.acceptance import L2Acceptance
    from orgpilot.feishu.client import MockFeishuClient

    _set_safe_feishu_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    class TestClient(MockFeishuClient):
        async def send_message(self, **kwargs) -> dict:
            if mode == "request_error":
                raise TimeoutError("secret_must_not_appear")
            return await super().send_message(**kwargs)

    def client_factory(**kwargs) -> TestClient:
        assert kwargs["allow_writes"] is True
        return TestClient()

    class TestListener:
        connection_error_type = "TestConnectionError"

        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

        def start(self) -> None:
            seen["started"] = True

        def wait_until_connected(self, _timeout: float) -> bool:
            return mode != "connection_failure"

        def stop(self) -> bool:
            seen["acceptance"].close()
            seen["stopped"] = True
            if mode == "shutdown_exception":
                raise RuntimeError("secret_must_not_appear")
            return mode != "shutdown_failure"

    original_wait = L2Acceptance.wait

    def deliver_or_timeout(acceptance: L2Acceptance, _timeout: float) -> dict:
        if mode != "timeout":
            asyncio.run(
                acceptance.handle_message(
                    {
                        "message_type": "text",
                        "chat_type": "p2p",
                        "message_id": "om_private",
                        "content": json.dumps({"text": acceptance.challenge}),
                    },
                    {"open_id": "ou_private", "sender_type": "user"},
                )
            )
        return original_wait(acceptance, 0)

    monkeypatch.setattr("orgpilot.feishu.client.AsyncFeishuClient", client_factory)
    monkeypatch.setattr("orgpilot.feishu.ws.FeishuWebSocketListener", TestListener)
    monkeypatch.setattr(L2Acceptance, "wait", deliver_or_timeout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orgpilot",
            "feishu-l2-smoke",
            "--project-id",
            "feishu-l2-offline",
            "--confirm-single-reply",
        ],
    )
    assert cli.main() == expected_code
    assert seen["stopped"] is True
    assert "gateway_service" not in seen
    report_path = tmp_path / "var/feishu-acceptance/feishu-l2-offline.json"
    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["reply_status"] == expected_status
    assert report["user_visible_confirmed"] is False
    assert report["window_closed"] is True
    assert report["listener_stopped"] is (mode not in {"shutdown_failure", "shutdown_exception"})
    assert not (tmp_path / "orgpilot.db").exists()
    output = capsys.readouterr().out
    for private_value in ("secret_must_not_appear", "ou_private", "om_private"):
        assert private_value not in output + report_text
    assert seen["acceptance"].challenge not in report_text
    if mode == "accepted":
        assert "user-side visibility still needs confirmation" in output
    with pytest.raises(SystemExit, match="report already exists"):
        cli.main()
