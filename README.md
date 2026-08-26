# OrgPilot

OrgPilot is a stateful organizational coordination agent kernel.

- **P0**: Deterministic, replayable kernel (events → projection → dependency impacts → coordination cases).
- **M1**: Mock closed-loop coordination agent (`CaseLedger` state machine, three-phase Action lifecycle, human approval gate, Mock adapter feedback, and bounded agent loop).

---

## Quick Start

```powershell
uv sync --dev
uv run orgpilot replay --all
uv run pytest
uv run ruff check .
```

The 9 ground-truth scenarios (4 P0 + 5 M1) live in `evals/scenarios/`. Development and architecture documents live in `docs/`.

---

## Documentation

- `docs/architecture.md`: Architecture specification, module boundaries, and state flow
- `docs/event-semantics.md`: Event envelope, lifecycle, idempotency, and LLM boundary
- `docs/ground-truth-scenarios.md`: Ground truth specifications for P0 and M1 scenarios
- `docs/development.md`: Setup, Git workflow, checks, and Definition of Done
- `docs/development-log.md`: Verified implementation log and deferred scope
- `docs/adr/0001-replayable-coordination-kernel.md`: ADR-0001: Replayable Coordination Kernel
- `docs/adr/0002-coordination-case-lifecycle.md`: ADR-0002: Coordination Case Lifecycle & Closed Loop

---

## Project Status

See `docs/development-log.md` for implemented, verified, and deferred work.
