"""Pins the contract between the web console and the gateway JSON it renders.

The console (``gateway/static/index.html``) reads API responses by field name, and no
type checker links it to ``gateway/schemas.py``. That gap let the DAG tab read
``health_distribution`` / ``nodes[].id`` / ``edges[].source`` from a response that has
``summary`` / ``nodes[].task_id`` / ``edges[].from_task``, and let approvals go out as a
hardcoded approver the server always rejected. The Python checks pin the fields and enum
values; the Node check runs the real console script against real API responses.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.gateway.app import create_app
from orgpilot.storage.database import Database

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")
PROJECT = "proj-console"
CONSOLE_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "orgpilot" / "gateway" / "static" / "index.html"
)

# Every /dag field the console reads. A field missing from the response renders as
# "undefined" or throws, so the response must carry all of them.
CONSOLE_DAG_READS = {
    "summary": {
        "total_tasks",
        "completed_count",
        "on_track_count",
        "at_risk_count",
        "delayed_count",
    },
    "node": {
        "task_id",
        "title",
        "owner_id",
        "workflow_status",
        "health_status",
        "layer",
        "is_at_risk",
        "is_critical_path",
        "deadline",
        "expected_completion",
        "blockers",
    },
    "edge": {"from_task", "to_task", "is_impacted", "is_critical"},
}


def _console_script() -> str:
    html = CONSOLE_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL)
    return next(js for js in scripts if "function loadDag" in js)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings, clock=FrozenClock(NOW))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await db.close()


def _event(event_id: str, event_type: str, source: str, payload: dict[str, Any]) -> dict:
    return {
        "schema_version": 1,
        "project_id": PROJECT,
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "source_ref": "setup",
        "occurred_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "payload": payload,
    }


def _member(member_id: str, name: str, role: str) -> dict:
    payload = {"member_id": member_id, "display_name": name, "role": role}
    return _event(f"m-{member_id}", "member.registered", "human", payload)


def _task(task_id: str, owner: str, status: str, hours: int, deps: tuple[str, ...] = ()) -> dict:
    payload = {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "owner_id": owner,
        "workflow_status": status,
        "deadline": (NOW + timedelta(hours=hours)).isoformat(),
        "dependencies": list(deps),
    }
    return _event(f"t-{task_id}", "task.created", "task", payload)


async def _seed(client: httpx.AsyncClient, *, with_pm: bool = True) -> None:
    """A delayed task with a downstream dependant, a done task, and a pending approval."""
    events = [
        _member("alice", "Alice", "backend"),
        _member("bob", "Bob", "frontend"),
        _task("backend_api", "alice", "doing", 3),
        _task("frontend_ui", "bob", "todo", 8, deps=("backend_api",)),
        _task("release_notes", "bob", "done", 1),
    ]
    if with_pm:
        events.insert(0, _member("carol", "Carol PM", "pm"))
    resp = await client.post(f"/api/v1/projects/{PROJECT}/events", json={"events": events})
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/projects/{PROJECT}/messages",
        json={
            "message": "支付 SDK 报错，排查需要到明天下午 5 点",
            "actor_id": "alice",
            "occurred_at": NOW.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text


async def _get(client: httpx.AsyncClient, path: str) -> Any:
    resp = await client.get(f"/api/v1/projects/{PROJECT}{path}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_dag_response_carries_every_field_the_console_reads(
    client: httpx.AsyncClient,
) -> None:
    await _seed(client)
    dag = await _get(client, "/dag")

    assert CONSOLE_DAG_READS["summary"] <= dag["summary"].keys()
    assert dag["nodes"] and dag["edges"]
    for node in dag["nodes"]:
        assert CONSOLE_DAG_READS["node"] <= node.keys(), node
    for edge in dag["edges"]:
        assert CONSOLE_DAG_READS["edge"] <= edge.keys(), edge

    # The list above must track what the console actually reads.
    script = _console_script()
    for fields in CONSOLE_DAG_READS.values():
        for name in fields:
            assert re.search(rf"\.{name}\b", script), f"console no longer reads .{name}"


def test_console_compares_against_real_enum_values() -> None:
    script = _console_script()
    health_values = {h.value for h in HealthStatus}
    workflow_values = {w.value for w in WorkflowStatus}

    labels = re.search(r"const HEALTH_LABELS = \{([^}]*)\}", script)
    assert labels is not None
    assert set(re.findall(r"(\w+):", labels.group(1))) == health_values

    compared_health = set(re.findall(r"health_status === '(\w+)'", script))
    compared_workflow = set(re.findall(r"workflow_status === '(\w+)'", script))
    assert compared_health and compared_health <= health_values
    assert compared_workflow and compared_workflow <= workflow_values


async def test_console_operator_is_the_pm_and_its_decision_is_accepted(
    client: httpx.AsyncClient,
) -> None:
    await _seed(client)
    context = await _get(client, "/console-context")
    assert context == {"project_id": PROJECT, "operator_id": "carol", "operator_name": "Carol PM"}

    (approval,) = await _get(client, "/approvals")
    assert approval["approver_id"] == context["operator_id"]
    resp = await client.post(
        f"/api/v1/projects/{PROJECT}/approvals/{approval['approval_id']}/decision",
        json={"decision": "approved", "approver_id": context["operator_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


async def test_console_has_no_operator_without_a_pm(client: httpx.AsyncClient) -> None:
    await _seed(client, with_pm=False)
    context = await _get(client, "/console-context")
    assert context == {"project_id": PROJECT, "operator_id": None, "operator_name": None}


# --- run the real console script against real API responses ---------------------------

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const calls = [];
const svg = { g: 0, path: 0 };

function element() {
  let markup = '';
  const el = {
    children: [], dataset: {}, value: '', textContent: '', className: '', title: '',
    disabled: false, onclick: null, scrollTop: 0, scrollHeight: 0, buttons: [],
    classList: { add() {}, remove() {} },
    setAttribute() {},
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll(selector) { return selector === 'button[data-decision]' ? this.buttons : []; },
    get innerHTML() { return markup; },
    set innerHTML(value) {
      markup = value;
      this.children = [];
      this.buttons = [...value.matchAll(/data-decision="(\w+)"/g)].map(m => {
        const button = element();
        button.dataset.decision = m[1];
        return button;
      });
    },
  };
  return el;
}

const elements = {};
const byId = id => (elements[id] ??= element());
byId('projectIdInput').value = fixture.project_id;
const context = vm.createContext({
  console, setTimeout, setInterval() {}, fixture,
  window: { addEventListener() {} },
  document: {
    getElementById: byId,
    createElement: () => element(),
    createElementNS: (ns, tag) => { svg[tag] = (svg[tag] || 0) + 1; return element(); },
  },
  fetch: async (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
    if (method !== 'GET') return { ok: true, status: 200, json: async () => fixture.decision };
    const key = Object.keys(fixture.responses).find(k => url.endsWith(k));
    return { ok: Boolean(key), status: key ? 200 : 404, json: async () => fixture.responses[key] };
  },
});
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), context);

const errors = [];
async function step(fn) {
  try { await fn(); } catch (e) { errors.push(String((e && e.stack) || e)); }
}
const text = id => byId(id).textContent;

(async () => {
  await step(() => context.loadDag());
  const statIds = ['statTotalTasks', 'statCompletedSub', 'statOnTrack', 'statAtRisk',
    'statDelayed', 'statHealthSub'];
  const stats = Object.fromEntries(statIds.map(id => [id, String(text(id))]));
  const drawn = { ...svg };
  await step(() => context.loadApprovals());
  const card = byId('approvalsListContainer').children[0];
  const buttons = card ? card.buttons : [];
  const enabled = buttons.map(b => !b.disabled);
  const operatorLabel = text('approvalOperator');
  const approve = buttons.find(b => b.dataset.decision === 'approved');
  await step(() => approve.onclick());
  const decisions = calls.filter(c => c.method === 'POST');
  const approvalStatus = text('approvalStatus');

  // Same approvals, but the server names no operator: nothing may be decidable.
  fixture.responses['/console-context'] = { project_id: fixture.project_id, operator_id: null };
  await step(() => context.loadApprovals());
  const noOperator = byId('approvalsListContainer').children[0].buttons.map(b => !b.disabled);

  process.stdout.write(JSON.stringify({
    errors, stats, drawn, enabled, operatorLabel, decisions, approvalStatus, noOperator,
  }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js on PATH")
async def test_console_script_renders_real_api_responses(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    await _seed(client)
    dag = await _get(client, "/dag")
    approvals = await _get(client, "/approvals")
    context = await _get(client, "/console-context")
    fixture = {
        "project_id": PROJECT,
        "responses": {
            "/dag": dag,
            "/state": await _get(client, "/state"),
            "/approvals": approvals,
            "/console-context": context,
        },
        "decision": {"status": "approved"},
    }
    (tmp_path / "console.js").write_text(_console_script(), encoding="utf-8")
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    (tmp_path / "harness.js").write_text(NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            str(tmp_path / "harness.js"),
            str(tmp_path / "console.js"),
            str(tmp_path / "fixture.json"),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert out["errors"] == []
    summary = dag["summary"]
    assert summary["delayed_count"] == 1 and summary["completed_count"] == 1
    assert out["stats"] == {
        "statTotalTasks": "3",
        "statCompletedSub": "已完成: 1",
        "statOnTrack": f"{summary['on_track_count']} 🟢",
        "statAtRisk": f"{summary['at_risk_count']} 🟡",
        "statDelayed": "1 🔴",
        "statHealthSub": "正常 / 风险 / 延期 · 未上报 1",
    }
    # One <g> per task and one <path> per dependency edge.
    assert out["drawn"] == {"g": len(dag["nodes"]), "path": len(dag["edges"])}

    assert out["operatorLabel"] == "当前操作人: Carol PM (carol)"
    assert out["enabled"] == [True, True]
    (decision,) = out["decisions"]
    approval_id = approvals[0]["approval_id"]
    assert decision["url"] == f"/api/v1/projects/{PROJECT}/approvals/{approval_id}/decision"
    assert decision["body"]["approver_id"] == "carol"
    assert out["approvalStatus"].startswith("✅")
    assert out["noOperator"] == [False, False]

    # Replay exactly what the console sent: the real endpoint accepts it.
    resp = await client.post(decision["url"], json=decision["body"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
