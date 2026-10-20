"""Resolves, plans, and applies a team.yaml import (ADR-0009 §3-§5)."""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from orgpilot.feishu.client import FeishuDirectory
from orgpilot.team.planner import ImportPlan, plan_import
from orgpilot.team.spec import TeamImportError, TeamSpec

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService

UNRESOLVED_HINTS = (
    "常见原因：成员不在应用的通讯录权限范围内；成员已离职；",
    "邮箱不是飞书账号绑定的邮箱——管理后台里填的「工作邮箱」（Work email）查不到；",
    "邮箱查不到时改填 mobile（手机号，中国大陆号码可不带 +86）。",
)


@dataclass(frozen=True)
class ImportResult:
    plan: ImportPlan
    open_ids: dict[str, str]
    applied: bool


async def resolve_open_ids(spec: TeamSpec, directory: FeishuDirectory) -> dict[str, str]:
    """Maps every member alias to its open_id, or raises listing every failure.

    Email is tried first and mobile is the fallback; when both resolve they must
    name the same Feishu user.
    """
    members = spec.members.values()
    resolved = await directory.batch_get_open_ids(
        emails=[m.email for m in members if m.email],
        mobiles=[m.mobile for m in members if m.mobile],
    )
    open_ids: dict[str, str] = {}
    errors: list[str] = []
    unresolved = False
    for alias, member in spec.members.items():
        by_email = resolved.get(member.email.lower()) if member.email else None
        by_mobile = resolved.get(member.mobile) if member.mobile else None
        if by_email and by_mobile and by_email != by_mobile:
            errors.append(
                f"member {alias!r} <{member.masked_identity}>: "
                "邮箱和手机号对应的是两个不同的飞书用户"
            )
        elif by_email or by_mobile:
            open_ids[alias] = by_email or by_mobile
        else:
            unresolved = True
            errors.append(f"member {alias!r} <{member.masked_identity}>: 飞书没有返回 open_id")
    if errors:
        raise TeamImportError([*errors, "".join(UNRESOLVED_HINTS)] if unresolved else errors)

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
