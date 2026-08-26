# OrgPilot

OrgPilot is a stateful organizational coordination agent. The current P0 is a
deterministic, replayable coordination kernel that turns immutable organization
events into source-backed task health, dependency impacts, and safe next-action
candidates.

## P0 scope

P0 intentionally contains no Feishu integration, database, web UI, or external
message execution. It proves the following chain first:

```text
OrgEvent -> projection -> task health -> dependency impact -> coordination case
```

Every derived result remains traceable to immutable source events. An LLM will
later be allowed to propose structured claims, but it will not write official
state or decide permissions.

## Quick start

```powershell
uv sync --dev
uv run orgpilot replay --all
uv run pytest
uv run ruff check .
```

The four replayable scenarios live in `evals/scenarios`. Development and design
documents live in `docs`.

## Documentation

- `docs/architecture.md`: implemented P0 module boundaries and state flow
- `docs/event-semantics.md`: event envelope, lifecycle, idempotency, and LLM boundary
- `docs/ground-truth-scenarios.md`: executable scenario contract
- `docs/development.md`: setup, Git workflow, checks, and Definition of Done
- `docs/development-log.md`: verified implementation record and explicit deferrals
- `docs/adr/0001-replayable-coordination-kernel.md`: first architecture decision

## Project status

See `OrgPilot-Initial-Design.md` for the original project proposal and
`docs/development-log.md` for implemented, verified, and deferred work.
