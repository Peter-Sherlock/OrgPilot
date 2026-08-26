"""Safe YAML loader for versioned, typed benchmark scenarios."""

from pathlib import Path

import yaml

from orgpilot.events.models import parse_event
from orgpilot.scenarios.models import (
    GroundTruth,
    InteractiveGroundTruth,
    ScenarioDefinition,
    ScenarioRound,
)


def load_scenario(path: Path) -> ScenarioDefinition:
    source_path = path.resolve()
    with source_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError(f"scenario must be a mapping: {source_path}")

    raw_events = raw.pop("events", None)
    events = tuple(parse_event(item) for item in raw_events) if raw_events is not None else ()

    raw_rounds = raw.pop("rounds", None)
    rounds = ()
    if raw_rounds is not None:
        rounds_list = []
        for r in raw_rounds:
            r_events = tuple(parse_event(e) for e in r.pop("events", []))
            rounds_list.append(ScenarioRound.model_validate({**r, "events": r_events}))
        rounds = tuple(rounds_list)

    raw_gt = raw.pop("ground_truth", None)
    ground_truth = GroundTruth.model_validate(raw_gt) if raw_gt is not None else None

    raw_igt = raw.pop("interactive_ground_truth", None)
    interactive_gt = InteractiveGroundTruth.model_validate(raw_igt) if raw_igt is not None else None

    scenario = ScenarioDefinition.model_validate(
        {
            **raw,
            "source_path": source_path,
            "events": events,
            "rounds": rounds,
            "ground_truth": ground_truth,
            "interactive_ground_truth": interactive_gt,
        }
    )

    for event in scenario.events:
        if event.project_id != scenario.project_id:
            raise ValueError(f"event {event.event_id!r} project does not match scenario project")

    for round_item in scenario.rounds:
        for event in round_item.events:
            if event.project_id != scenario.project_id:
                raise ValueError(
                    f"round {round_item.round_number} event {event.event_id!r} "
                    f"project does not match scenario project"
                )

    return scenario


def discover_scenarios(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(directory.glob("*.yaml")))
