"""Tests for the multi-turn conversation evaluation (3a.1)."""

from pathlib import Path

from orgpilot.intent.client import HeuristicIntentClient
from orgpilot.intent.conversation_eval import (
    ConversationSystemUnderTest,
    load_conversations,
)
from orgpilot.intent.evaluator import load_intent_project
from orgpilot.intent.oracle import OracleIntentClient

EVAL_DIR = Path(__file__).resolve().parents[1] / "evals" / "intent"

# Short conversations for the in-suite gate (keeps pytest fast); the full set
# runs via `orgpilot eval-intent --conversations --system layered --intent oracle`.
SHORT_CONVERSATIONS = {
    "conv_04_probe_help_command",
    "conv_08_duplicate_delivery",
}


def test_conversations_dataset_shape() -> None:
    scripts = load_conversations(EVAL_DIR / "conversations.yaml")
    assert len(scripts) >= 8
    ids = [script.id for script in scripts]
    assert len(set(ids)) == len(ids)
    for script in scripts:
        assert script.turns, f"{script.id} has no turns"
        assert script.final is not None


async def test_oracle_gate_passes_short_conversations() -> None:
    project = load_intent_project(EVAL_DIR / "project.yaml")
    scripts = [
        script
        for script in load_conversations(EVAL_DIR / "conversations.yaml")
        if script.id in SHORT_CONVERSATIONS
    ]
    assert len(scripts) == len(SHORT_CONVERSATIONS)
    system = ConversationSystemUnderTest(
        project, lambda script: OracleIntentClient(script.turns, allow_duplicate_keys=True)
    )
    for script in scripts:
        result = await system.run(script)
        assert result.passed, f"{script.id}: {result.issues}"


async def test_duplicate_delivery_turn_reports_duplicate() -> None:
    project = load_intent_project(EVAL_DIR / "project.yaml")
    scripts = {
        script.id: script
        for script in load_conversations(EVAL_DIR / "conversations.yaml")
    }
    system = ConversationSystemUnderTest(
        project, lambda script: OracleIntentClient(script.turns, allow_duplicate_keys=True)
    )
    result = await system.run(scripts["conv_08_duplicate_delivery"])
    assert result.turn_results[1].duplicate is True
    assert result.turn_results[0].duplicate is False
    # the redelivered message produced no second claim
    assert len(result.final_claims) == 1


async def test_full_oracle_gate_on_committed_conversations(tmp_path: Path) -> None:
    """The committed oracle gate: ALL 10 conversations must pass."""

    project = load_intent_project(EVAL_DIR / "project.yaml")
    scripts = load_conversations(EVAL_DIR / "conversations.yaml")
    system = ConversationSystemUnderTest(
        project, lambda script: OracleIntentClient(script.turns, allow_duplicate_keys=True)
    )
    results = [await system.run(script) for script in scripts]
    failed = [r.script_id for r in results if not r.passed]
    assert not failed, f"conversations failed: {failed}"
    from orgpilot.intent.conversation_eval import write_conversation_reports

    json_path, markdown_path = write_conversation_reports(results, tmp_path)
    assert json_path.exists() and markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "多轮对话评测报告" in markdown
    assert "失败 0" in markdown


async def test_heuristic_mode_converges_conv_01() -> None:
    """The 3a.2 heuristic gate: conv_01 converges with the right completion time."""
    project = load_intent_project(EVAL_DIR / "project.yaml")
    scripts = {
        script.id: script
        for script in load_conversations(EVAL_DIR / "conversations.yaml")
    }
    system = ConversationSystemUnderTest(project, HeuristicIntentClient())
    result = await system.run(scripts["conv_01_probe_clarify_time"])
    assert result.passed, result.issues
    claim = [c for c in result.final_claims if c["task_id"] == "pay_e2e_test"][-1]
    assert claim["health_status"] == "at_risk"
    assert claim["expected_completion"] is not None


async def test_failing_expectations_surface_issues(tmp_path: Path) -> None:
    project = load_intent_project(EVAL_DIR / "project.yaml")
    scripts = load_conversations(EVAL_DIR / "conversations.yaml")
    by_id = {script.id: script for script in scripts}
    target = by_id["conv_01_probe_clarify_time"]
    # corrupt the first turn's expectation: claim a reply category we know is wrong
    target.turns[0].expected.behavior.reply_category = "no_permission"
    system = ConversationSystemUnderTest(
        project, lambda script: OracleIntentClient(script.turns, allow_duplicate_keys=True)
    )
    result = await system.run(target)
    assert not result.passed
    assert any("回复类别不符" in issue for issue in result.issues)
    from orgpilot.intent.conversation_eval import render_conversation_report

    markdown = render_conversation_report([result])
    assert "失败" in markdown
