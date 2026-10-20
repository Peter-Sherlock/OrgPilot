"""Guards the web console against stored XSS.

Task titles, member names and message text reach the console from Feishu users and
team.yaml, and the console builds its DOM with innerHTML templates. Every value
interpolated into markup must go through ``escapeHtml(...)`` unless it is an in-page
constant on ``RAW_ALLOWED``. The static check parses the inline script of ``index.html``;
the behavioural check runs the real render functions under Node against a stub DOM.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

CONSOLE_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "orgpilot" / "gateway" / "static" / "index.html"
)

# In-page values the console computes itself. None of them carries server data, so they
# may be interpolated raw. Keep this list short: anything else goes through escapeHtml.
RAW_ALLOWED = {
    "badgeBg",  # DAG node colour: one of five hex literals returned by dagNodeColor
    "borderStroke",  # DAG node border colour: one of two hex literals
    "borderWidth",  # DAG node border width: one of two numeric literals
    "criticalDot",  # static <circle> markup or ''
    "catBadge",  # timeline badge classes: one of three string literals
    "recommendationRows",  # joined <div> rows; their own template is checked here too
}

TAG = re.compile(r"<\s*/?[A-Za-z]")
# An interpolation that lands inside an event-handler or URL attribute value. Escaping
# does not make those safe: the browser decodes entities before it runs the handler or
# follows the URL.
UNSAFE_ATTRIBUTE = re.compile(
    r"\b(?:on[a-z]+|href|src|action|formaction|xlink:href)\s*=\s*"
    r"(?:\"[^\"]*|'[^']*|[^\s\"'=<>`]*)$",
    re.IGNORECASE,
)
SINK_ASSIGN = re.compile(r"\.(?:innerHTML|outerHTML)\s*\+?=\s*")
SINK_CALL = re.compile(r"\.insertAdjacentHTML\(\s*(['\"])[^'\"]*\1\s*,\s*")
OTHER_SINKS = ("document.write", "createContextualFragment", "parseFromString", "srcdoc")


@dataclass
class Template:
    start: int  # index of the opening backtick
    end: int  # index just past the closing backtick
    chunks: list[str]  # static text; chunks[i] precedes exprs[i]
    exprs: list[tuple[int, str]]  # (index, source) of each ${...}


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    checked: int = 0  # interpolations inspected inside HTML templates


class _Scanner:
    """Finds template literals in a script and blanks string/comment bodies.

    Not a full JS lexer: it does not recognise regex literals. A quote inside one
    would start a bogus string, and a single-quoted string may not cross a line,
    so that misparse fails loudly instead of silently skipping code.
    """

    def __init__(self, src: str) -> None:
        self.src = src
        self.masked = list(src)
        self.templates: list[Template] = []

    def _blank(self, start: int, end: int) -> None:
        for k in range(start, end):
            if self.masked[k] != "\n":
                self.masked[k] = " "

    def code(self, i: int, until_brace: bool = False) -> int:
        src, depth = self.src, 0
        while i < len(src):
            ch = src[i]
            if ch in "'\"":
                i = self._string(i)
            elif src.startswith("//", i):
                end = src.find("\n", i)
                end = len(src) if end == -1 else end
                self._blank(i, end)
                i = end
            elif src.startswith("/*", i):
                end = src.index("*/", i) + 2
                self._blank(i, end)
                i = end
            elif ch == "`":
                i = self._template(i)
            elif ch == "{":
                depth += 1
                i += 1
            elif ch == "}":
                if until_brace and depth == 0:
                    return i
                depth -= 1
                i += 1
            else:
                i += 1
        if until_brace:
            raise AssertionError("unterminated ${...} in console script")
        return i

    def _string(self, i: int) -> int:
        quote, j = self.src[i], i + 1
        while self.src[j] != quote:
            if self.src[j] == "\\":
                j += 1
            elif self.src[j] == "\n":
                raise AssertionError(f"unterminated string literal at offset {i}")
            j += 1
        self._blank(i + 1, j)
        return j + 1

    def _template(self, i: int) -> int:
        chunks: list[str] = []
        exprs: list[tuple[int, str]] = []
        chunk_start = j = i + 1
        while self.src[j] != "`":
            if self.src[j] == "\\":
                j += 2
            elif self.src.startswith("${", j):
                chunks.append(self.src[chunk_start:j])
                self._blank(chunk_start, j)
                close = self.code(j + 2, until_brace=True)
                exprs.append((j, self.src[j + 2 : close].strip()))
                j = chunk_start = close + 1
            else:
                j += 1
        chunks.append(self.src[chunk_start:j])
        self._blank(chunk_start, j)
        self.templates.append(Template(i, j + 1, chunks, exprs))
        return j + 1


def _is_escape_call(expr: str) -> bool:
    """True when the whole expression is a single ``escapeHtml(...)`` call."""
    if not expr.startswith("escapeHtml("):
        return False
    depth = 0
    for k, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return k == len(expr) - 1
    return False


def check_script(js: str, first_line: int = 1) -> Report:
    scanner = _Scanner(js)
    scanner.code(0)
    masked = "".join(scanner.masked)
    by_start = {t.start: t for t in scanner.templates}
    report = Report()

    def line(pos: int) -> int:
        return first_line + js.count("\n", 0, pos)

    # Every sink in the raw text must also be a sink in code, or the scanner swallowed it.
    for pattern in (SINK_ASSIGN, SINK_CALL):
        raw = {m.start() for m in pattern.finditer(js)}
        seen = {m.start() for m in pattern.finditer(masked)}
        for pos in sorted(raw - seen):
            report.problems.append(f"line {line(pos)}: HTML sink hidden from the scanner")

    sink_templates: set[int] = set()
    sinks = [(m, ";") for m in SINK_ASSIGN.finditer(masked)]
    sinks += [(m, ")") for m in SINK_CALL.finditer(masked)]
    for match, closer in sinks:
        pos = match.end()
        if pos < len(js) and js[pos] == "`" and pos in by_start:
            end = by_start[pos].end
            sink_templates.add(pos)
        elif pos < len(js) and js[pos] in "'\"":
            end = masked.index(js[pos], pos + 1) + 1
        else:
            report.problems.append(
                f"line {line(pos)}: HTML sink takes a non-literal value; use a template "
                "whose interpolations go through escapeHtml"
            )
            continue
        if not masked[end:].lstrip(" \t").startswith(closer):
            report.problems.append(
                f"line {line(pos)}: HTML sink concatenates more text after its literal"
            )

    for template in scanner.templates:
        if template.start not in sink_templates and not TAG.search("".join(template.chunks)):
            continue  # plain text, e.g. a URL or a textContent label
        prefix = ""
        for chunk, (pos, expr) in zip(template.chunks, template.exprs, strict=False):
            prefix += chunk
            report.checked += 1
            if UNSAFE_ATTRIBUTE.search(prefix):
                report.problems.append(
                    f"line {line(pos)}: ${{{expr}}} lands in an event-handler or URL "
                    "attribute; bind the handler in JS instead"
                )
            elif not (_is_escape_call(expr) or expr in RAW_ALLOWED):
                report.problems.append(
                    f"line {line(pos)}: ${{{expr}}} is not wrapped in escapeHtml(...)"
                )
            prefix += "0"  # stands in for the interpolated value

    for name in OTHER_SINKS:
        if name in masked:
            report.problems.append(f"{name} builds HTML outside the checked sinks")
    return report


def console_scripts() -> list[tuple[int, str]]:
    """Inline scripts of index.html, each with the file line its code starts on."""
    html = CONSOLE_HTML.read_text(encoding="utf-8")
    pattern = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)
    return [(html.count("\n", 0, m.start(1)) + 1, m.group(1)) for m in pattern.finditer(html)]


def test_console_templates_escape_every_interpolation() -> None:
    reports = [check_script(js, first_line) for first_line, js in console_scripts()]
    problems = [p for r in reports for p in r.problems]
    assert problems == [], "\n".join(problems)
    # The console has about 25 today; a scanner bug that skips templates drops this to 0.
    assert sum(r.checked for r in reports) >= 20


@pytest.mark.parametrize(
    "snippet",
    [
        "el.innerHTML = `<p>${task.title}</p>`;",
        "el.innerHTML = `${task.title}`;",
        "el.innerHTML = '<p>' + task.title;",
        "el.innerHTML = html;",
        "el.innerHTML += `<p>${escapeHtml(a)}</p>` + b;",
        "el.innerHTML = `<p>${escapeHtml(a) + b}</p>`;",
        "el.insertAdjacentHTML('beforeend', `<li>${name}</li>`);",
        "const row = `<li>${name}</li>`;",
        "el.innerHTML = `<button onclick=\"go('${escapeHtml(id)}')\">x</button>`;",
        'el.innerHTML = `<a href="${escapeHtml(url)}">x</a>`;',
        "document.write(name);",
    ],
)
def test_checker_flags_unsafe_html(snippet: str) -> None:
    assert check_script(snippet).problems


@pytest.mark.parametrize(
    "snippet",
    [
        "el.innerHTML = `<p>${escapeHtml(task.title)}</p>`;",
        "el.innerHTML = '';",
        "el.textContent = `已完成: ${count}`;",
        "fetch(`/api/v1/projects/${pid}/dag`);",
        'el.innerHTML = `<rect fill="${badgeBg}" />`;',
        "el.insertAdjacentHTML('beforeend', `<li>${escapeHtml(name)}</li>`);",
    ],
)
def test_checker_accepts_safe_html(snippet: str) -> None:
    assert check_script(snippet).problems == []


# --- behavioural check: run the real render functions with hostile data -----------------

PAYLOAD = "<img src=x onerror=alert(1)><script>alert(2)</script>\"'&"

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const written = [];
let surface = null;

function element() {
  let markup = '';
  return {
    children: [], dataset: {}, value: '', textContent: '', className: '',
    scrollTop: 0, scrollHeight: 0,
    classList: { add() {}, remove() {} },
    setAttribute() {},
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll() { return []; },
    get innerHTML() { return markup; },
    set innerHTML(value) { markup = value; if (value) written.push({ surface, html: value }); },
  };
}

const elements = {};
const context = vm.createContext({
  console, setTimeout, setInterval() {}, fixture,
  mark(name) { surface = name; },
  window: { addEventListener() {} },
  document: {
    getElementById: id => (elements[id] ??= element()),
    createElement: () => element(),
    createElementNS: () => element(),
  },
  fetch: async url => {
    const hit = Object.keys(fixture.responses).find(key => url.includes(key));
    return { ok: Boolean(hit), json: async () => (hit ? fixture.responses[hit] : {}) };
  },
});
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), context);
vm.runInContext(`(async () => {
  const p = fixture.payload;
  mark('user-bubble'); appendChatBubble('chatStreamPM', true, p, p);
  mark('bot-bubble'); appendChatBubble('chatStreamPM', false, p, p);
  mark('briefing-card'); appendChatBubble('chatStreamPM', false, p, '', true, fixture.briefing);
  mark('dag-node'); renderDagSvg(fixture.dag);
  mark('timeline'); await loadTimeline();
  mark('approvals'); await loadApprovals();
})()`, context).then(() => process.stdout.write(JSON.stringify(written)));
"""


class _Fragment(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[str] = []
        self.text: list[str] = []
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs.extend(name for name, _ in attrs)

    handle_startendtag = handle_starttag

    def handle_data(self, data: str) -> None:
        self.text.append(data)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js on PATH")
def test_console_renders_hostile_data_as_text(tmp_path: Path) -> None:
    script = next(js for _, js in console_scripts() if "function appendChatBubble" in js)
    fixture = {
        "payload": PAYLOAD,
        "briefing": {
            "title": PAYLOAD,
            "on_track_count": 1,
            "topological_risks": [{"source_task_id": PAYLOAD, "cascading_impact_tasks": [PAYLOAD]}],
            "recommended_actions": [PAYLOAD],
        },
        "dag": {
            # Same shape as the real /dag response (DagNode in gateway/schemas.py).
            "nodes": [
                {
                    "task_id": "task-x",
                    "title": PAYLOAD,
                    "owner_id": PAYLOAD,
                    "workflow_status": PAYLOAD,
                    "health_status": PAYLOAD,
                    "layer": 0,
                    "is_critical_path": True,
                }
            ],
            "edges": [],
        },
        "responses": {
            "/timeline": {
                "entries": [
                    {
                        "category": PAYLOAD,
                        "title": PAYLOAD,
                        "description": PAYLOAD,
                        "timestamp": "2026-10-20T09:00:00Z",
                    }
                ]
            },
            "/approvals": [
                {
                    "approval_id": "appr-1",
                    "approver_id": PAYLOAD,
                    "proposed_command": {"payload": {"task_id": PAYLOAD, "new_deadline": PAYLOAD}},
                }
            ],
        },
    }
    (tmp_path / "console.js").write_text(script, encoding="utf-8")
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

    rendered: dict[str, list[_Fragment]] = {}
    for item in json.loads(result.stdout):
        rendered.setdefault(item["surface"], []).append(_Fragment(item["html"]))
    # The DAG title is cut to 8 characters before it is escaped.
    expected_text = {
        "user-bubble": PAYLOAD,
        "bot-bubble": PAYLOAD,
        "briefing-card": PAYLOAD,
        "dag-node": PAYLOAD[:8] + "…",
        "timeline": PAYLOAD,
        "approvals": PAYLOAD,
    }
    assert set(rendered) == set(expected_text)
    for surface, fragments in rendered.items():
        tags = {tag for f in fragments for tag in f.tags}
        handlers = [a for f in fragments for a in f.attrs if a.startswith("on")]
        text = "".join(t for f in fragments for t in f.text)
        assert not tags & {"img", "script"}, surface
        assert handlers == [], surface
        assert expected_text[surface] in text, surface
