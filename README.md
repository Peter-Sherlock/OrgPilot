# OrgPilot

OrgPilot is a stateful organizational coordination agent kernel.

- **P0**: Deterministic, replayable kernel (events → projection → dependency impacts → coordination cases).
- **M1**: Mock closed-loop coordination agent (`CaseLedger` state machine, three-phase Action lifecycle, human approval gate, Mock adapter feedback, and bounded agent loop).
- **M2 prototype**: Grounded claim extraction with an offline deterministic client and an opt-in AIHubMix Anthropic-compatible client.
- **P1 prototype**: Async SQL persistence and a FastAPI event gateway. Local persistent SQLite is the default; PostgreSQL deployment still requires environment-specific integration validation.
- **F1 integration layer**: Feishu OpenAPI client, HTTP Webhook handler, adapter, and interactive cards. A real-account smoke test is still required before calling this production-ready.
- **W1 Web console**: Embedded Single-Page management console and real-time interactive DAG topology visualizer served directly at `http://localhost:8000/`.
- **M3 complete**: Role-aware intent routing, event-sourced directive execution (issue → relay → ack → complete → remind → escalate), closed multi-turn clarifications, gated NL task creation/reassignment/deadline changes (grounded proposal → dependency analysis → approval card → task.created/task.updated events → DAG + notifications), and M3-R real-integration reliability (typed adapter contract, transactional outbox with retry/dead-letter/delivery ledger, LLM circuit breaker, per-project ingest serialization, server-bound approval identity, XSS escaping).

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
Open `http://localhost:8000/` in your browser: the multi-role split-screen sandbox lets one person role-play PM + engineers (4 windows) through probe fan-out, autonomous clarification, and DAG briefing synthesis; the interactive DAG topology, the Explainability Timeline, and the PM approval strip (inside the PM window) sit on the adjacent tabs. If a probed member never replies, `POST /api/v1/projects/{id}/sync/complete` (the PM window's「⏭️ 未响应成员直接出简报」button) closes the session with `no_response` markers and delivers the briefing anyway.

External integrations are opt-in. The provider selector and `AIHUBMIX_*` names are retained for
backward compatibility, but the client accepts any Anthropic Messages-compatible endpoint. For
official DeepSeek:

```powershell
$env:ORGPILOT_LLM_PROVIDER = "aihubmix"
$env:AIHUBMIX_API_KEY = "<DeepSeek key; set securely>"
$env:AIHUBMIX_BASE_URL = "https://api.deepseek.com/anthropic"
$env:AIHUBMIX_MODEL = "deepseek-v4-flash"
$env:ORGPILOT_LLM_REASONING_EFFORT = "none"
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```

- The offline 34-sample extraction result (`--provider mock`) is a deterministic regression benchmark, not an accuracy claim.
- Live-model benchmark via `uv run orgpilot eval-extraction --provider aihubmix` (requires `AIHUBMIX_API_KEY`): official `deepseek-v4-flash` measured **F1 100%**, task-id accuracy 100%, slot-datetime accuracy 100%, false alarms 0%, grounding 100%, and **intent accuracy 92.86%** over all 34 synthetic gold samples (2026-08-30, Anthropic format, thinking disabled). Earlier `gpt-5.6-luna` runs measured intent accuracy 92.9–100%. Relative times resolve against the team reference timezone (`ORGPILOT_TIMEZONE`, default `Asia/Shanghai`). The role-aware intent router (directives, task create/reassign, deadline change, question, chit-chat) runs on deterministic rules before the LLM call and short-circuits non-report messages at zero LLM cost.

- Feishu HTTP Webhook setup guide lives in `docs/feishu-setup-guide.md`.
- Real Feishu mutations are additionally gated by `ORGPILOT_FEISHU_ALLOW_WRITES=true`
  (false by default). Run `uv run orgpilot feishu-preflight` first; add
  `--online-auth` only when a read-only credential check is intended.
- The solo-tester demo task chain injected on a first Feishu message is opt-in: set `ORGPILOT_DEMO_BOOTSTRAP=true` (off by default).
- Outbound delivery is durable: commands persist in an outbox before sending, failed transports retry with backoff (`ORGPILOT_OUTBOX_*` settings) and dead-letter after the attempt cap; a startup/periodic sweep re-sends anything a crash stranded between "event persisted" and "sent". `GET /api/v1/projects/{id}/outbox` exposes the delivery ledger, and the console shows a backlog badge.
- Ground-truth replay scenarios (4 P0 + 5 M1) live in `evals/scenarios/`.
- 34-sample natural language extraction gold dataset lives in `evals/extraction/gold_dataset.yaml`.
- Architecture and design specifications live in `docs/`.

---

## Documentation

- `docs/architecture.md`: Architecture specification, module boundaries, and state flow
- `docs/feishu-setup-guide.md`: 2-minute Feishu Custom App creation and permission guide
- `docs/feishu-live-acceptance.md`: gated live-tenant preflight, acceptance evidence, and rollback
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
- `docs/adr/0007-intent-routing-layer.md`: ADR-0007: Role-Aware Intent Routing Layer
- `docs/adr/0008-directive-execution-chain.md`: ADR-0008: Event-Sourced Directive Execution Chain
- `docs/adr/0009-real-integration-reliability.md`: ADR-0009: Real-Integration Reliability (M3-R)
- `docs/adr/0010-nl-task-operations.md`: ADR-0010: NL Task Creation & Reassignment Behind Approval Gates

---

## Project Status

See `docs/development-log.md` for implemented, verified, and deferred work.
