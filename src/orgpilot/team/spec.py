"""team.yaml schema and loader (ADR-0009 §2)."""

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orgpilot.domain.enums import WorkflowStatus

PM_ROLE = "pm"
# Roles that used to receive approvals before ADR-0009 §7 narrowed them to pm.
FORMER_PRIVILEGED_ROLES = frozenset({"manager", "lead"})

_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TeamImportError(ValueError):
    """team.yaml cannot be loaded, planned, or applied; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


class _SpecModel(BaseModel):
    """Unknown keys are typos, not extensions: reject them."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MemberSpec(_SpecModel):
    email: str
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip()
        if not _EMAIL_PATTERN.match(value):
            raise ValueError(f"not an email address: {value!r}")
        return value

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()

    @field_validator("role")
    @classmethod
    def _normalize_role(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("role must not be blank")
        return value.strip().lower()


class TaskSpec(_SpecModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    owner: str
    status: WorkflowStatus = WorkflowStatus.TODO
    deadline: datetime | None = None
    depends_on: tuple[str, ...] = ()

    @field_validator("deadline", mode="before")
    @classmethod
    def _require_time_of_day(cls, value: Any) -> Any:
        # YAML parses a bare 2026-10-24 into a date; "当天几点截止" is ambiguous.
        if isinstance(value, date) and not isinstance(value, datetime):
            raise ValueError(
                f"deadline {value.isoformat()} needs a time of day, e.g. '{value} 18:00'"
            )
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            raise ValueError(f"deadline {value} needs a time of day, e.g. '{value} 18:00'")
        return value

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "TaskSpec":
        if self.id in self.depends_on:
            raise ValueError(f"task {self.id!r} cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"task {self.id!r} lists a dependency twice")
        return self


class TeamSpec(_SpecModel):
    project: str = Field(min_length=1)
    members: dict[str, MemberSpec]
    tasks: tuple[TaskSpec, ...] = ()

    @model_validator(mode="after")
    def _check_references(self) -> "TeamSpec":
        errors: list[str] = []
        for alias in self.members:
            if not _ALIAS_PATTERN.match(alias):
                errors.append(f"member alias {alias!r} may only use letters, digits, - and _")
        seen_emails: dict[str, str] = {}
        for alias, member in self.members.items():
            key = member.email.lower()
            if key in seen_emails:
                errors.append(
                    f"members {seen_emails[key]!r} and {alias!r} share email {member.email!r}"
                )
            seen_emails.setdefault(key, alias)
        seen_tasks: set[str] = set()
        for task in self.tasks:
            if task.id in seen_tasks:
                errors.append(f"task id {task.id!r} appears more than once")
            seen_tasks.add(task.id)
            if task.owner not in self.members:
                errors.append(f"task {task.id!r} owner {task.owner!r} is not a member alias")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def localized(self, business_timezone: ZoneInfo) -> "TeamSpec":
        """Interprets naive deadlines in the team's business timezone."""
        tasks = tuple(
            task.model_copy(update={"deadline": task.deadline.replace(tzinfo=business_timezone)})
            if task.deadline is not None and task.deadline.tzinfo is None
            else task
            for task in self.tasks
        )
        return self.model_copy(update={"tasks": tasks})


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_team_spec(text: str, business_timezone: ZoneInfo) -> TeamSpec:
    """Parses and validates team.yaml text; raises TeamImportError listing all problems."""
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise TeamImportError([f"invalid YAML: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise TeamImportError(["team.yaml must be a mapping with project, members, and tasks"])
    try:
        spec = TeamSpec.model_validate(raw)
    except ValidationError as exc:
        raise TeamImportError(
            [
                f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()
            ]
        ) from exc
    return spec.localized(business_timezone)


def load_team_spec(path: Path, business_timezone: ZoneInfo) -> tuple[TeamSpec, str]:
    """Loads team.yaml and returns it with a short content digest for source_ref."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:12]
    return parse_team_spec(data.decode("utf-8"), business_timezone), digest
