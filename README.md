# OrgPilot

OrgPilot is a stateful organizational coordination agent kernel.

- **P0**: Deterministic, replayable kernel (events → projection → dependency impacts → coordination cases).
- **M1**: Mock closed-loop coordination agent (`CaseLedger` state machine, three-phase Action lifecycle, human approval gate, Mock adapter feedback, and bounded agent loop).
- **M2 prototype**: Grounded claim extraction with an offline deterministic client and an opt-in AIHubMix Anthropic-compatible client.
- **P1 prototype**: Async SQL persistence and a FastAPI event gateway. Local persistent SQLite is the default; PostgreSQL deployment still requires environment-specific integration validation.
- **F1 integration layer**: Feishu OpenAPI client, HTTP Webhook handler, adapter, and interactive cards. A real-account smoke test is still required before calling this production-ready.
- **W1 Web console**: Embedded Single-Page management console and real-time interactive DAG topology visualizer served directly at `http://localhost:8000/`.
- **P-1 team onboarding**: `team.yaml` roster import (`orgpilot team import`) with Feishu email → open_id resolution and a three-way field merge on re-import; the first-contact demo bootstrap now requires `ORGPILOT_DEMO_MODE=true`. Email resolution still needs a real-tenant smoke test.

---

## Quick Start

```powershell
uv sync --dev
uv run orgpilot replay --all
uv run orgpilot eval-extraction
uv run pytest
uv run ruff check .
```

To run the gateway and open the Web Dashboard locally:
```powershell
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```
Open `http://localhost:8000/` in your browser to explore the interactive DAG topology, Explainability Timeline, and PM Approval Console.

External integrations are opt-in. For AIHubMix:

```powershell
$env:ORGPILOT_LLM_PROVIDER = "aihubmix"
$env:AIHUBMIX_API_KEY = "<set securely>"
$env:AIHUBMIX_MODEL = "gpt-5.6-luna"
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```

For the DeepSeek official Anthropic-format endpoint (primary LLM candidate, ADR-0008 §7):

```powershell
$env:ORGPILOT_LLM_PROVIDER = "deepseek"
$env:DEEPSEEK_API_KEY = "<set securely>"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
$env:DEEPSEEK_MODEL = "deepseek-flash"
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```

Both online providers share one Anthropic-compatible client. The optional
`ORGPILOT_LLM_THINKING` switch (`omit` / `disabled` / `enabled`) controls the
Anthropic-format thinking field: it defaults to `omit` on AIHubMix (field never
sent, unchanged behavior) and `disabled` on DeepSeek (whose service-side default
is enabled).

The offline 20-sample extraction result is a deterministic regression benchmark; it is not a measured accuracy claim for the live model.

To onboard a real team, copy `team.example.yaml` to `team.yaml` (git-ignored), fill in members and tasks, then:

```powershell
uv run orgpilot team import team.yaml --dry-run
uv run orgpilot team import team.yaml
uv run orgpilot team show
```

- Feishu HTTP Webhook setup guide lives in `docs/feishu-setup-guide.md`.
- Ground-truth replay scenarios (4 P0 + 5 M1) live in `evals/scenarios/`.
- 20-sample natural language extraction gold dataset lives in `evals/extraction/gold_dataset.yaml`.
- Architecture and design specifications live in `docs/`.

---

## Documentation

- `docs/architecture.md`: Architecture specification, module boundaries, and state flow
- `docs/feishu-setup-guide.md`: 2-minute Feishu Custom App creation and permission guide
- `docs/event-semantics.md`: Event envelope, lifecycle, idempotency, and LLM boundary
- `docs/ground-truth-scenarios.md`: Ground truth specifications for P0 and M1 scenarios
- `docs/development.md`: Setup, Git workflow, checks, and Definition of Done
- `docs/development-log.md`: Verified implementation log and deferred scope
- `docs/adr/0001-replayable-coordination-kernel.md`: ADR-0001: Replayable Coordination Kernel
- `docs/adr/0002-coordination-case-lifecycle.md`: ADR-0002: Coordination Case Lifecycle & Closed Loop
- `docs/adr/0003-llm-claim-extraction-boundary.md`: ADR-0003: LLM Claim Extraction & Security Boundary
- `docs/adr/0004-postgresql-event-store-and-fastapi-gateway.md`: ADR-0004: SQL Persistence & FastAPI Gateway
- `docs/adr/0005-feishu-adapter-and-interactive-cards.md`: ADR-0005: Feishu Adapter & Interactive Cards
- `docs/adr/0006-web-dashboard-and-dag-visualization.md`: ADR-0006: Web Dashboard & DAG Visualization
- `docs/adr/0007-github-integration-and-pr-only-permission.md`: ADR-0007: GitHub Integration & PR-only Permission
- `docs/adr/0008-layered-intent-recognition.md`: ADR-0008: Layered Intent Recognition
- `docs/adr/0009-team-onboarding-and-roster-import.md`: ADR-0009: Team Onboarding & Roster Import

---

## Project Status

See `docs/development-log.md` for implemented, verified, and deferred work.
