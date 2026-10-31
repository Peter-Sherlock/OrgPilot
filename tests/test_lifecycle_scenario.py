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

C1 = "case:task-health:t1_gateway"
C3 = "case:task-health:t3_order"

KNOWN_DEFECTS: dict[str, tuple[str, ...]] = {
    # coordination/ledger.py:163-173. When an estimate arrives while the case
    # waits for a response, reconcile() resolves the case as "recovery estimate
    # received" before anything compares the estimate with the deadline. Round 4:
    # Alice's ETA is two days past T1's deadline, no reschedule is proposed, and
    # the PM's approval in round 5 finds nothing to approve.
    # The 2026-10-08 registry filed this under D1; it is a separate code site and
    # can be fixed on its own.
    "D1a recovery estimate closes the case without checking the deadline": (
        "round_4:termination",
        f"round_4:case_status:{C1}",
        f"round_4:waiting_for:{C1}",
        "round_5:approval_applied:pm_carol:approved",
        # Either D1a or D1b alone turns this green (with D1b, the reopened case
        # later resolves as "reschedule approved"). Whichever fix lands first
        # removes it.
        f"case_terminal_reason:{C1}",
    ),
    # coordination/ledger.py:195-197. case_id is derived from the task id and
    # reconcile() opens a case only when that id is unused, so a task whose case
    # is terminal can never get a new one. Round 17: T1 regresses (done -> doing,
    # ETA past its deadline), _is_risky() is true, and nothing happens.
    # A fix must reopen on new evidence only (e.g. a risk claim newer than the
    # closed case). Reopening whenever the task is still risky makes cases flap
    # (T3 stays at_risk after its in-deadline ETA; done tasks keep their risk
    # until D6) and breaks m1_01 and m1_05.
    # coordination/approval.py:24 derives approval_id from case_id + action_id,
    # so a second reschedule on the same case overwrites the consumed first
    # request. No check here observes that, but the fix should not rely on it.
    "D1b terminal case occupies the task's case slot": (
        "round_17:termination",
        "round_18:approval_applied:pm_carol:approved",
        "task:t1_gateway",
    ),
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
    # state/projector.py:103-119: _change_workflow() leaves health claims alone,
    # so a task that reaches done keeps the at_risk/delayed health resolved from
    # its claims (:167-169), and dependencies/analyzer.py:47-52 keeps propagating
    # impacts from it. The dashboard paints it delayed (gateway/routes/dag.py:146
    # derives is_delayed from health alone). A fix must retire risk claims only:
    # the ground truth expects t2_channel to stay on_track after it is done.
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
