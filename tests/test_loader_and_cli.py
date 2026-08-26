"""Public scenario loading and CLI behavior."""

import sys
from pathlib import Path

import pytest

from orgpilot import cli
from orgpilot.scenarios.loader import load_scenario


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


def test_cli_replays_all_scenarios(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["orgpilot", "replay", "--all"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert output.count("[PASS]") == 4


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
