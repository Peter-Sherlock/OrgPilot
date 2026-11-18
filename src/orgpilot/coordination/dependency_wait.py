"""Deterministic rules for dependency waits (ADR-0016). Pure functions, no I/O.

The model may say that someone is waiting; only this module decides which formal
upstream task that means, who is contacted, and what they are told.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from orgpilot.domain.enums import (
    ActionType,
    CoordinationCaseKind,
    CoordinationCaseStatus,
    DependencyWaitStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    CoordinationAction,
    CoordinationCase,
    DependencyWait,
    OrgState,
    TaskState,
)

WAIT_CASE_PREFIX = "case:dependency-wait:"

_EXACT_MENTION_SCORE = 1000
# Several formal upstreams need a clear name; one shared bigram ("接口") is not enough.
_CLEAR_MENTION_SCORE = 2

_WAIT_MARKER = re.compile(
    r"(?<![高中初平对同相均])等(?:着|待)?(?!等|一下|下|会|于|级|同|价|我|你|他|她)"
)
_NOT_A_WAIT = re.compile(
    r"[?？]|吗|什么时候|好了没|多久|如果|要是|假如|万一|假设|不等|不用等|别等|没在等|没有在等|无需等"
)
_CANCELS_WAIT = re.compile(r"不用等了|不等了|别等了|无需等了")
_CLAUSE_END = re.compile(r"[，。,.!！；;\n]")


class UpstreamOutcome(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNNAMED = "unnamed"
    NO_DEPENDENCY = "no_dependency"
    ALREADY_DONE = "already_done"
    SELF_OWNED = "self_owned"


@dataclass(frozen=True)
class UpstreamResolution:
    outcome: UpstreamOutcome
    task_id: str | None = None
    candidates: tuple[str, ...] = ()


def wait_case_id(wait_id: str) -> str:
    return f"{WAIT_CASE_PREFIX}{wait_id}"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _bigrams(text: str) -> set[str]:
    return {text[index : index + 2] for index in range(len(text) - 1)}


def mention_score(task: TaskState, text: str) -> int:
    """How clearly ``text`` names ``task``: exact title or id first, then shared bigrams."""
    compact = _compact(text)
    title = _compact(task.title)
    if (title and title in compact) or task.task_id.lower() in compact:
        return _EXACT_MENTION_SCORE + len(title)
    return len(_bigrams(title) & _bigrams(compact))


def names_task_exactly(task: TaskState, text: str) -> bool:
    return mention_score(task, text) >= _EXACT_MENTION_SCORE


def resolve_upstream(state: OrgState, waiting: TaskState, text: str) -> UpstreamResolution:
    """Finds the formal direct dependency a message refers to, without guessing."""
    dependencies = [state.tasks[dep] for dep in waiting.dependencies if dep in state.tasks]
    if not dependencies:
        return UpstreamResolution(UpstreamOutcome.NO_DEPENDENCY)

    # The waiting task's own name must not count as naming its upstream.
    subject = _compact(text).replace(_compact(waiting.title), "")
    open_tasks = [task for task in dependencies if task.workflow_status != WorkflowStatus.DONE]
    done_tasks = [task for task in dependencies if task.workflow_status == WorkflowStatus.DONE]
    scored = sorted(
        ((mention_score(task, subject), task.task_id) for task in open_tasks), reverse=True
    )
    best_open = scored[0][0] if scored else 0

    done_named = [
        task.task_id
        for task in done_tasks
        if mention_score(task, subject) >= max(_CLEAR_MENTION_SCORE, best_open + 1)
    ]
    if done_named or not open_tasks:
        return UpstreamResolution(
            UpstreamOutcome.ALREADY_DONE,
            candidates=tuple(done_named or (task.task_id for task in done_tasks)),
        )

    candidates = tuple(sorted(task.task_id for task in open_tasks))
    if len(open_tasks) == 1:
        if best_open < 1:
            return UpstreamResolution(UpstreamOutcome.UNNAMED, candidates=candidates)
        matched = scored[0][1]
    elif best_open >= _CLEAR_MENTION_SCORE and best_open > scored[1][0]:
        matched = scored[0][1]
    else:
        return UpstreamResolution(UpstreamOutcome.AMBIGUOUS, candidates=candidates)

    if state.tasks[matched].owner_id == waiting.owner_id:
        return UpstreamResolution(UpstreamOutcome.SELF_OWNED, task_id=matched)
    return UpstreamResolution(UpstreamOutcome.MATCHED, task_id=matched)


def wait_clauses(message: str) -> tuple[str, ...]:
    """The clause after each "等" in a plain statement; empty for anything else."""
    if _NOT_A_WAIT.search(message):
        return ()
    clauses: list[str] = []
    for marker in _WAIT_MARKER.finditer(message):
        rest = message[marker.end() :]
        end = _CLAUSE_END.search(rest)
        clause = rest[: end.start()] if end else rest
        if clause.strip():
            clauses.append(clause)
    return tuple(clauses)


def cancels_wait(message: str) -> bool:
    return _CANCELS_WAIT.search(message) is not None


def wait_statement_tasks(state: OrgState, sender_id: str, message: str) -> tuple[str, ...]:
    """Unfinished tasks of the sender whose formal upstream is named right after "等"."""
    clauses = wait_clauses(message)
    if not clauses:
        return ()
    hits: list[str] = []
    for task in sorted(state.tasks.values(), key=lambda item: item.task_id):
        if task.owner_id != sender_id or task.workflow_status == WorkflowStatus.DONE:
            continue
        upstreams = (state.tasks[dep] for dep in task.dependencies if dep in state.tasks)
        if any(
            mention_score(upstream, clause) >= _CLEAR_MENTION_SCORE
            for upstream in upstreams
            for clause in clauses
        ):
            hits.append(task.task_id)
    return tuple(hits)


def active_waits(state: OrgState) -> tuple[DependencyWait, ...]:
    return tuple(
        wait
        for _, wait in sorted(state.dependency_waits.items())
        if wait.status is not DependencyWaitStatus.RELEASED
    )


def active_wait_for_pair(
    state: OrgState, waiting_task_id: str, upstream_task_id: str
) -> DependencyWait | None:
    return next(
        (
            wait
            for wait in active_waits(state)
            if wait.waiting_task_id == waiting_task_id and wait.upstream_task_id == upstream_task_id
        ),
        None,
    )


def leads_upstream_question(wait: DependencyWait, state: OrgState) -> bool:
    """One open question per upstream task: the earliest unanswered wait asks it."""
    peers = [
        peer
        for peer in state.dependency_waits.values()
        if peer.upstream_task_id == wait.upstream_task_id
        and peer.status is DependencyWaitStatus.AWAITING_ESTIMATE
    ]
    return min(peers, key=lambda peer: (peer.opened_at, peer.wait_id)).wait_id == wait.wait_id


def awaited_waits(
    cases: Iterable[CoordinationCase], state: OrgState, member_id: str
) -> tuple[DependencyWait, ...]:
    """Waits currently expecting an estimate from this member.

    Read from the case ledger rather than dialogue state, so a pending probe or
    clarification is never overwritten and the link survives a restart.
    """
    waits: list[DependencyWait] = []
    for case in cases:
        if (
            case.kind is not CoordinationCaseKind.DEPENDENCY_WAIT
            or case.status is not CoordinationCaseStatus.WAITING_FOR_RESPONSE
        ):
            continue
        wait = state.dependency_waits.get(case.dependency_wait_id or "")
        if wait is None or wait.status is not DependencyWaitStatus.AWAITING_ESTIMATE:
            continue
        upstream = state.tasks.get(wait.upstream_task_id)
        if upstream is not None and upstream.owner_id == member_id:
            waits.append(wait)
    return tuple(sorted(waits, key=lambda wait: wait.wait_id))


def downstream_titles(state: OrgState, task_id: str) -> tuple[str, ...]:
    """Titles of every task that transitively depends on ``task_id``."""
    seen: set[str] = set()
    frontier = [task_id]
    while frontier:
        current = frontier.pop()
        for task in state.tasks.values():
            if current in task.dependencies and task.task_id not in seen:
                seen.add(task.task_id)
                frontier.append(task.task_id)
    return tuple(state.tasks[item].title for item in sorted(seen))


def format_moment(moment: datetime | None) -> str:
    return moment.strftime("%m/%d %H:%M") if moment is not None else "未设置"


def estimate_misses_deadline(wait: DependencyWait, waiting: TaskState) -> bool:
    return (
        wait.estimated_at is not None
        and waiting.deadline is not None
        and wait.estimated_at > waiting.deadline
    )


def _display_name(state: OrgState, member_id: str) -> str:
    member = state.members.get(member_id)
    return member.display_name if member is not None else member_id


def _private_action(
    wait: DependencyWait,
    suffix: str,
    action_type: ActionType,
    target: str,
    text: str,
    effect: str,
) -> CoordinationAction:
    return CoordinationAction(
        action_id=f"action:{wait.wait_id}:{suffix}",
        action_type=action_type,
        targets=(target,),
        reason_refs=wait.evidence_event_ids,
        expected_effect=effect,
        payload={
            "wait_id": wait.wait_id,
            "task_id": wait.upstream_task_id,
            "inquiry_text": text,
        },
    )


def plan_wait_actions(
    wait: DependencyWait, state: OrgState, pm_ids: tuple[str, ...]
) -> tuple[CoordinationAction, ...]:
    """The private messages this wait needs next; never a group message or a task write."""
    waiting = state.tasks.get(wait.waiting_task_id)
    upstream = state.tasks.get(wait.upstream_task_id)
    if waiting is None or upstream is None or wait.status is DependencyWaitStatus.RELEASED:
        return ()

    owner = upstream.owner_id
    reporter = _display_name(state, wait.reported_by)
    owner_name = _display_name(state, owner)
    pm_targets = tuple(pm for pm in pm_ids if pm not in {wait.reported_by, owner})

    if wait.status is DependencyWaitStatus.ESTIMATED:
        moment = format_moment(wait.estimated_at)
        text = (
            f"【{upstream.title}】负责人 {owner_name} 回复：预计 {moment} 可交付或联调"
            f"（原话：「{wait.estimate_quote}」）。这是预计时间，不代表已经交付。"
        )
        if estimate_misses_deadline(wait, waiting):
            text += (
                f"\n该时间晚于【{waiting.title}】的截止时间 {format_moment(waiting.deadline)}，"
                "需要 PM 决定是否调整排期，系统不会自动改期。"
            )
        return tuple(
            _private_action(
                wait,
                f"relay:{target}",
                ActionType.RELAY_DELIVERY_ESTIMATE,
                target,
                text,
                "share the upstream owner's delivery estimate",
            )
            for target in dict.fromkeys((wait.reported_by, *pm_targets))
        )

    actions: list[CoordinationAction] = []
    if leads_upstream_question(wait, state):
        actions.append(
            _private_action(
                wait,
                "ask_eta",
                ActionType.ASK_DEPENDENCY_ETA,
                owner,
                (
                    f"{reporter} 负责的【{waiting.title}】在等你负责的【{upstream.title}】"
                    f"（原话：「{wait.quote}」）。方便告诉我预计什么时候可以交付或联调吗？"
                    "例如：明天下午3点。"
                ),
                "obtain a delivery estimate from the upstream owner",
            )
        )
    downstream = downstream_titles(state, waiting.task_id)
    impact = f"，下游还影响：{'、'.join(downstream)}" if downstream else ""
    notice = (
        f"{reporter} 报告【{waiting.title}】在等【{upstream.title}】（原话：「{wait.quote}」）。"
        f"{owner_name} 的交付时间待确认。"
        f"【{waiting.title}】截止时间 {format_moment(waiting.deadline)}{impact}。"
    )
    actions.extend(
        _private_action(
            wait,
            f"notify_pm:{target}",
            ActionType.NOTIFY_WAIT_OPENED,
            target,
            notice,
            "tell the PM about a cross-member wait",
        )
        for target in pm_targets
    )
    return tuple(actions)
