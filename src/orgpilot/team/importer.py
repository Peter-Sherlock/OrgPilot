"""Resolves, plans, and applies a team.yaml import (ADR-0009 §3-§5)."""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from orgpilot.feishu.client import FeishuDirectory
from orgpilot.team.planner import ImportPlan, plan_import
from orgpilot.team.spec import TeamImportError, TeamSpec

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService

UNRESOLVED_EMAIL_HINTS = (
    "常见原因：应用未开通 contact:user.id:readonly 权限；",
    "成员不在应用的通讯录权限范围内；成员已离职；",
    "填写的是飞书企业邮箱，而接口只认用户资料里的「邮箱」字段。",
)


@dataclass(frozen=True)
class ImportResult:
    plan: ImportPlan
    open_ids: dict[str, str]
    applied: bool


async def resolve_open_ids(spec: TeamSpec, directory: FeishuDirectory) -> dict[str, str]:
    """Maps every member alias to its open_id, or raises listing every failure."""
    resolved = await directory.batch_get_open_ids([m.email for m in spec.members.values()])
    open_ids: dict[str, str] = {}
    errors: list[str] = []
    for alias, member in spec.members.items():
        open_id = resolved.get(member.email.lower())
        if open_id is None:
            errors.append(f"member {alias!r} <{member.email}>: 飞书没有返回 open_id")
        else:
            open_ids[alias] = open_id
    if errors:
        raise TeamImportError([*errors, "".join(UNRESOLVED_EMAIL_HINTS)])

    aliases_by_open_id: dict[str, list[str]] = {}
    for alias, open_id in open_ids.items():
        aliases_by_open_id.setdefault(open_id, []).append(alias)
    shared = [aliases for aliases in aliases_by_open_id.values() if len(aliases) > 1]
    if shared:
        raise TeamImportError(
            [
                f"members {', '.join(repr(a) for a in aliases)} are the same Feishu user"
                for aliases in shared
            ]
        )
    return open_ids


async def import_team(
    service: "GatewayService",
    spec: TeamSpec,
    directory: FeishuDirectory,
    *,
    digest: str,
    now: datetime,
    dry_run: bool = False,
) -> ImportResult:
    """Plans the import and, unless ``dry_run``, persists it all-or-nothing."""
    open_ids = await resolve_open_ids(spec, directory)
    agent = await service.get_or_replay_agent(spec.project)
    history = await service.event_store.get_events(spec.project)
    plan = plan_import(spec, agent.projector.state, open_ids, history, now=now, digest=digest)
    if dry_run or not plan.events:
        return ImportResult(plan, open_ids, applied=False)

    # The same validate-then-persist path as message extraction: the whole batch
    # is projected on a throwaway copy first, so a rejected import writes nothing.
    _, _, _, error = await service.commit_extracted_events(
        spec.project,
        list(plan.events),
        actor_id="team-import",
        occurred_at=now,
        auto_run_turn=False,
    )
    if error is not None:
        raise TeamImportError([f"导入在写入前的校验中被拒绝，未写入任何事件：{error}"])
    return ImportResult(plan, open_ids, applied=True)
