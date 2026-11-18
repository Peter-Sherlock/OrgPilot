"""The R1 dependency wait evaluation: frozen data, honest scoring, gated model calls."""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from evals.dependency_wait import runner
from evals.dependency_wait.runner import Contact, Observation, ObservedWait, score_case
from orgpilot.intent.client import HeuristicIntentClient
from orgpilot.intent.llm_client import LLMIntentClient, SpendLimitExceeded

REQUIRED_CATEGORIES = {
    "explicit_wait",
    "vague_upstream",
    "multiple_candidates",
    "question",
    "hypothetical",
    "negation",
    "cancel",
    "reply_with_time",
    "reply_without_time",
    "multiple_items",
}
ASK = {"action": "ask_dependency_eta", "target": "bob"}
NOTIFY = {"action": "notify_wait_opened", "target": "pm"}


def _case(**expected: object) -> dict:
    return {
        "id": "sample",
        "category": "explicit_wait",
        "turns": [{"sender": "alice", "text": "在等接口"}],
        "expected": {"waits": [], "contacts": [], **expected},
    }


WAIT = {"waiting": "page", "upstream": "api", "status": "awaiting_estimate"}
OPEN_WAIT = ObservedWait("page", "api", "awaiting_estimate")


# -------------------------------------------------------------------- dataset


def test_the_dataset_matches_its_frozen_fingerprint() -> None:
    assert runner.frozen_mismatches() == []


def test_the_dataset_covers_every_required_situation() -> None:
    cases = runner.load_cases()
    distinct = sum(
        len({turn.get("message_id") or turn["text"] for turn in case["turns"]}) for case in cases
    )
    assert 20 <= distinct <= 30
    assert {case["category"] for case in cases} >= REQUIRED_CATEGORIES
    assert len({case["id"] for case in cases}) == len(cases)


def test_expected_results_only_name_known_tasks_and_members() -> None:
    project = runner.load_project()
    tasks = {task["task_id"] for task in project["tasks"]}
    members = {member["member_id"] for member in project["members"]}
    for case in runner.load_cases():
        assert {turn["sender"] for turn in case["turns"]} <= members
        assert {contact["target"] for contact in case["expected"]["contacts"]} <= members
        for wait in case["expected"]["waits"]:
            assert {wait["waiting"], wait["upstream"]} <= tasks


# -------------------------------------------------------------------- scoring


def test_an_exact_match_passes() -> None:
    score = score_case(
        _case(waits=[WAIT], contacts=[ASK, NOTIFY]),
        Observation(
            waits=[OPEN_WAIT],
            contacts=[
                Contact("ask_dependency_eta", "bob", "page->api"),
                Contact("notify_wait_opened", "pm", "page->api"),
            ],
        ),
    )
    assert score["passed"] and score["localized"] and score["reasons"] == []


def test_contacting_someone_unexpected_is_a_wrong_contact() -> None:
    score = score_case(
        _case(waits=[WAIT], contacts=[ASK]),
        Observation(
            waits=[OPEN_WAIT],
            contacts=[
                Contact("ask_dependency_eta", "bob", "page->api"),
                Contact("ask_dependency_eta", "carol", "page->other"),
            ],
        ),
    )
    assert score["wrong_contacts"] == [["ask_dependency_eta", "carol"]]
    assert score["wrong_contacts_to_others"] == [["ask_dependency_eta", "carol"]]
    assert not score["passed"]


def test_asking_the_sender_back_is_wrong_but_reported_apart() -> None:
    score = score_case(_case(), Observation(contacts=[Contact("ask_recovery_estimate", "alice")]))
    assert score["wrong_contacts"] == [["ask_recovery_estimate", "alice"]]
    assert score["wrong_contacts_to_others"] == []


def test_the_same_action_twice_is_a_duplicate_and_not_a_wrong_contact() -> None:
    repeated = Contact("ask_dependency_eta", "bob", "page->api")
    score = score_case(
        _case(waits=[WAIT], contacts=[ASK]),
        Observation(waits=[OPEN_WAIT], contacts=[repeated, repeated]),
    )
    assert len(score["duplicate_actions"]) == 1
    assert score["wrong_contacts"] == []
    assert not score["passed"]


def test_the_same_action_about_two_waits_is_not_a_duplicate() -> None:
    score = score_case(
        _case(contacts=[NOTIFY, NOTIFY]),
        Observation(
            contacts=[
                Contact("notify_wait_opened", "pm", "page->api"),
                Contact("notify_wait_opened", "pm", "form->api"),
            ]
        ),
    )
    assert score["duplicate_actions"] == []
    assert score["missing_contacts"] == []


def test_a_missed_contact_is_not_counted_as_a_wrong_one() -> None:
    score = score_case(_case(waits=[WAIT], contacts=[ASK]), Observation())
    assert score["missing_contacts"] == [["ask_dependency_eta", "bob"]]
    assert score["wrong_contacts"] == []
    assert not score["localized"]


def test_a_wrong_upstream_is_not_localized() -> None:
    score = score_case(
        _case(waits=[WAIT]), Observation(waits=[ObservedWait("page", "other", "awaiting_estimate")])
    )
    assert not score["localized"]


def test_a_wrong_delivery_time_fails_the_case_but_not_localization() -> None:
    expected = {**WAIT, "status": "estimated", "estimated_at": "2026-11-19T15:00:00+08:00"}
    score = score_case(
        _case(waits=[expected]),
        Observation(waits=[ObservedWait("page", "api", "estimated", "2026-11-19T18:00:00+08:00")]),
    )
    assert score["localized"]
    assert score["estimate_correct"] is False
    assert not score["passed"]


def test_the_same_moment_in_another_offset_is_the_right_time() -> None:
    expected = {**WAIT, "status": "estimated", "estimated_at": "2026-11-19T15:00:00+08:00"}
    score = score_case(
        _case(waits=[expected]),
        Observation(waits=[ObservedWait("page", "api", "estimated", "2026-11-19T07:00:00+00:00")]),
    )
    assert score["estimate_correct"] is True


def test_a_required_clarification_must_happen() -> None:
    silent = score_case(_case(clarify=True), Observation(reply_categories=["noted"]))
    asked = score_case(_case(clarify=True), Observation(reply_categories=["clarify"]))
    assert silent["clarified"] is False and not silent["passed"]
    assert asked["clarified"] is True and asked["passed"]


def test_a_failed_model_call_never_passes() -> None:
    score = score_case(_case(), Observation(reply_categories=["fallback"]))
    assert score["fallback"] and not score["passed"]


def test_summary_reports_each_group_on_its_own() -> None:
    opened = {**_case(waits=[WAIT]), "id": "opened"}
    quiet = {**_case(), "id": "quiet"}
    summary = runner.summarize(
        [score_case(opened, Observation()), score_case(quiet, Observation())]
    )
    assert summary["upstream_localization"] == {"correct": 1, "total": 2}
    assert summary["localization_by_group"]["should_open"] == {"correct": 0, "total": 1}
    assert summary["localization_by_group"]["should_not_open"] == {"correct": 1, "total": 1}


# ------------------------------------------------------------------- end to end


async def test_an_offline_run_is_reproducible() -> None:
    first = await runner.evaluate(HeuristicIntentClient())
    second = await runner.evaluate(HeuristicIntentClient())
    assert first == second
    assert first["summary"]["scenarios"] == len(runner.load_cases())


def _model_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_intent",
                    "input": {"reasoning": "无", "items": [], "answers_pending": False},
                }
            ],
        },
    )


def _llm_client(tmp_path: Path, **overrides: object) -> LLMIntentClient:
    settings: dict = {
        "api_key": "test-only",
        "base_url": "https://model.invalid",
        "model": "deepseek-flash",
        "business_tz": ZoneInfo("Asia/Shanghai"),
        "recordings_dir": tmp_path,
    }
    return LLMIntentClient(**{**settings, **overrides})  # type: ignore[arg-type]


async def test_a_recorded_run_replays_to_the_same_report_without_any_request(
    tmp_path: Path,
) -> None:
    cases = [case for case in runner.load_cases() if case["id"] in {"explicit_01", "cancel_01"}]
    calls: list[httpx.Request] = []

    def live_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _model_reply(request)

    live = _llm_client(
        tmp_path, mode="live", record=True, transport=httpx.MockTransport(live_handler)
    )
    recorded = await runner.evaluate(live, cases=cases)
    await live.aclose()
    assert len(calls) == 2

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("replay must not send a request")

    replay = _llm_client(tmp_path, mode="replay", transport=httpx.MockTransport(forbidden))
    assert await runner.evaluate(replay, cases=cases) == recorded
    await replay.aclose()


async def test_the_spend_cap_stops_the_run(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _model_reply(request)

    client = _llm_client(
        tmp_path, mode="live", max_spend_usd=1e-9, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SpendLimitExceeded):
        await runner.evaluate(client)
    await client.aclose()
    assert len(calls) == 1


async def test_the_committed_live_run_replays_to_the_same_behaviour() -> None:
    committed = json.loads(runner.results_path("deepseek-flash").read_text(encoding="utf-8"))
    client = _llm_client(runner.recordings_dir("deepseek-flash"), mode="replay")
    replayed = await runner.evaluate(client)
    await client.aclose()
    assert runner.behaviour_view(replayed) == runner.behaviour_view(committed)


def test_the_published_numbers_match_the_committed_live_run() -> None:
    summary = json.loads(runner.results_path("deepseek-flash").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["upstream_localization"] == {"correct": 24, "total": 29}
    assert summary["localization_by_group"]["should_open"] == {"correct": 5, "total": 10}
    assert summary["wrong_contacts"] == 9
    assert summary["wrong_contacts_to_others"] == 0
    assert summary["duplicate_actions"] == 0
    assert summary["cases_passed"] == {"correct": 20, "total": 29}


def test_recordings_stay_out_of_the_frozen_intent_directory() -> None:
    target = runner.recordings_dir("deepseek-flash").resolve()
    assert target.is_relative_to(runner.EVAL_DIR.resolve())
    assert "intent" not in target.relative_to(runner.EVAL_DIR.resolve()).parts


@pytest.mark.parametrize(
    "argv",
    [
        ["--client", "llm"],
        ["--client", "llm", "--live", "--replay"],
        ["--client", "llm", "--live"],
        ["--client", "llm", "--live", "--confirm-spend"],
        ["--client", "llm", "--live", "--max-spend-usd", "0.1"],
        ["--live"],
    ],
)
def test_a_live_run_needs_explicit_confirmation_and_a_cap(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        runner.main(argv)


def test_a_run_where_every_call_failed_writes_no_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "denied"}))
    client = _llm_client(tmp_path, mode="live", transport=rejected)
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(runner, "_build_llm_client", lambda _args: client)
    code = runner.main(
        [
            "--client",
            "llm",
            "--live",
            "--confirm-spend",
            "--max-spend-usd",
            "0.1",
            "--write-results",
        ]
    )
    assert code == 1
    assert not (tmp_path / "results").exists()


def test_a_live_run_refuses_to_replace_existing_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    runner.results_path("deepseek-flash").write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(SystemExit, match="已存在"):
        runner.main(["--client", "llm", "--live", "--confirm-spend", "--max-spend-usd", "0.1"])
