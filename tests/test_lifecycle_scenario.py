"""Full project lifecycle scenario: correct-behaviour ground truth plus a defect ratchet.

`evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml` describes what a correct
coordination agent should do across a six-week project. The kernel does not meet
that bar yet, so every failing check is registered below under the root cause
that produces it. The two ratchet tests keep the registry exact:

- a check that fails without being registered is a regression;
- a registered check that starts passing means a defect was fixed, and its entry
  must be removed so it cannot silently regress again.

The gap between kernel and ground truth can therefore only shrink.

The registry was re-derived on main ed8bf27 (2026-10-29); file:line references
below point at that commit. Each root cause was also checked by patching only
the cited lines in memory: every patch turned exactly its own checks green, with
no new failure here or in evals/scenarios/. Details and the proposed fix order:
docs/full-lifecycle-scenario-design.md, section 9.
"""

from pathlib import Path

from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.scenarios.evaluator import evaluate_scenario
from orgpilot.scenarios.loader import load_scenario
from orgpilot.scenarios.models import ReplayResult
from orgpilot.scenarios.runner import ScenarioRunner

LIFECYCLE_PATH = Path("evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml")

C3 = "case:task-health:t3_order"

KNOWN_DEFECTS: dict[str, tuple[str, ...]] = {
    # Nothing is driven by time. The only time comparison in reconcile() is the
    # response timeout of a case that already waits (coordination/ledger.py:
    # 176-188); _is_risky() (:221-226) ignores task.deadline, and commitment.due_at
    # is only compared with the time of an event that settles the commitment
    # (state/projector.py, since D4), never with the passing clock. Round 9: T3 is
    # 16 hours past its deadline with an expired commitment, nobody speaks, and
    # the agent reports all_resolved.
    # The ground truth keeps T3's task-health case `resolved`, so an overdue
    # follow-up needs a case of its own (or must wait for D1b).
    "D3 missed deadlines trigger nothing": ("round_9:termination",),
}


def _invariant_checks(result: ReplayResult) -> dict[str, bool]:
    """Design-agnostic properties that hold for any correct closure state."""
    done = {
        task_id
        for task_id, task in result.state.tasks.items()
        if task.workflow_status is WorkflowStatus.DONE
    }
    return {
        "invariant:done_tasks_report_no_risk": all(
            result.state.tasks[task_id].health_status
            not in {HealthStatus.AT_RISK, HealthStatus.DELAYED}
            for task_id in done
        ),
        "invariant:no_impacts_from_done_tasks": all(
            impact.source_task_id not in done for impact in result.impacts
        ),
    }


def _all_checks() -> dict[str, bool]:
    scenario = load_scenario(LIFECYCLE_PATH)
    result = ScenarioRunner().run(scenario)
    report = evaluate_scenario(scenario, result)
    checks = {assertion.name: assertion.passed for assertion in report.assertions}
    assert len(checks) == len(report.assertions), "assertion names must be unique"
    checks.update(_invariant_checks(result))
    return checks


def _registered() -> set[str]:
    names = [name for names in KNOWN_DEFECTS.values() for name in names]
    assert len(names) == len(set(names)), "a check is registered under two defects"
    return set(names)


def test_lifecycle_has_no_unregistered_failures() -> None:
    failed = {name for name, passed in _all_checks().items() if not passed}

    assert failed - _registered() == set(), "unregistered failures are regressions"


def test_registered_defects_still_reproduce() -> None:
    """Fails once a defect is fixed: delete its entry so the fix stays locked in."""
    checks = _all_checks()
    unknown = _registered() - checks.keys()
    now_passing = {name for name in _registered() & checks.keys() if checks[name]}

    assert unknown == set(), "registered checks no longer exist in the scenario"
    fixed = {
        defect: sorted(set(names) & now_passing)
        for defect, names in KNOWN_DEFECTS.items()
        if set(names) & now_passing
    }
    assert now_passing == set(), f"now passing, remove from KNOWN_DEFECTS: {fixed}"


def test_lifecycle_exercises_both_working_and_broken_behaviour() -> None:
    """Guards against the scenario degrading into all-red or losing its controls."""
    checks = _all_checks()
    passing = {name for name, passed in checks.items() if passed}

    assert len(checks) >= 50
    # Positive controls: paths M1 already verified must keep working end to end.
    assert {
        "round_3:waiting_for:case:task-health:t1_gateway",
        "round_8:case_status:case:task-health:t3_order",
        "round_11:waiting_for:case:task-health:t5_cashier_app",
        "round_12:case_status:case:task-health:t5_cashier_app",
        "round_14:case_status:case:task-health:t4_cashier_h5",
        "task:t5_cashier_app",
        "commitment:c_frank_t7_done",
    } <= passing


def test_lifecycle_replay_is_deterministic() -> None:
    scenario = load_scenario(LIFECYCLE_PATH)

    first = ScenarioRunner().run(scenario)
    second = ScenarioRunner().run(scenario)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
