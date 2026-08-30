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
