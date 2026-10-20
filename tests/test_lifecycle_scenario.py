"""Full project lifecycle scenario: correct-behaviour ground truth plus a defect ratchet.

`evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml` describes what a correct
coordination agent should do across a six-week project. The kernel does not meet
that bar yet, so every failing check is registered below under the root cause
that produces it. The two ratchet tests keep the registry exact:

- a check that fails without being registered is a regression;
- a registered check that starts passing means a defect was fixed, and its entry
  must be removed so it cannot silently regress again.

The gap between kernel and ground truth can therefore only shrink.
"""

from pathlib import Path

from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.scenarios.evaluator import evaluate_scenario
from orgpilot.scenarios.loader import load_scenario
from orgpilot.scenarios.models import ReplayResult
from orgpilot.scenarios.runner import ScenarioRunner

LIFECYCLE_PATH = Path("evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml")

C1 = "case:task-health:t1_gateway"
C3 = "case:task-health:t3_order"

KNOWN_DEFECTS: dict[str, tuple[str, ...]] = {
    # case_id is derived from the task id and reconcile() only opens a case when
    # none exists, so a task whose case reached a terminal state can never be
    # opened again. Two symptoms: an estimate obtained by a follow-up question
    # resolves the case before the deadline comparison runs (round 4, no
    # reschedule is ever proposed), and a task reopened by a regression is risky
    # but stays silent (round 17). A fix also has to give the second approval a
    # distinct approval_id, which is derived from case_id and action_id today.
    "D1 terminal case occupies the task's case slot": (
        "round_4:termination",
        f"round_4:case_status:{C1}",
        f"round_4:waiting_for:{C1}",
        "round_5:approval_applied:pm_carol:approved",
        "round_17:termination",
        "round_18:approval_applied:pm_carol:approved",
        f"case_terminal_reason:{C1}",
        "task:t1_gateway",
    ),
    # The recovery question goes to whoever reported the risk
    # (primary_claim.stated_by), not to the task owner: QA reports a risk on a
    # backend task and the agent asks QA when the backend work will recover.
    "D2 follow-up targets the reporter instead of the owner": (f"round_7:waiting_for:{C3}",),
    # Nothing is triggered by time passing. T3 is 16 hours past its deadline
    # with an expired commitment and nobody speaking, and the agent reports
    # all_resolved.
    "D3 missed deadlines trigger nothing": ("round_9:termination",),
    # Commitments are fulfilled whenever the predicate becomes true, without
    # comparing against due_at; CommitmentStatus.BROKEN is never written.
    "D4 late fulfilment is recorded as fulfilled": ("commitment:c_bob_t3_done",),
    # With no active case, any escalation in the project's history makes every
    # later turn report `escalated`, and terminal cases never react to their
    # task completing. One escalation in week 3 colours the rest of the project.
    "D5 escalation sticks for the rest of the project": (
        "round_15:termination",
        "round_16:termination",
        "round_18:termination",
        "round_19:termination",
        "round_20:termination",
        "round_21:termination",
        "round_22:termination",
        "final_termination_reason",
    ),
    # Risk claims stay active after the task completes, so finished tasks keep
    # reporting at_risk/delayed and keep propagating impacts downstream. The
    # dashboard paints them as delayed (gateway/routes/dag.py derives is_delayed
    # from health alone).
    "D6 completed tasks keep their risk": (
        "invariant:done_tasks_report_no_risk",
        "invariant:no_impacts_from_done_tasks",
    ),
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

    assert failed - _registered() == set()


def test_registered_defects_still_reproduce() -> None:
    """Fails once a defect is fixed: delete its entry so the fix stays locked in."""
    checks = _all_checks()
    unknown = _registered() - checks.keys()
    now_passing = {name for name in _registered() & checks.keys() if checks[name]}

    assert unknown == set(), "registered checks no longer exist in the scenario"
    assert now_passing == set()


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
