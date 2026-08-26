"""Safe YAML loader for versioned, typed benchmark scenarios."""

from pathlib import Path

import yaml

from orgpilot.events.models import parse_event
from orgpilot.scenarios.models import GroundTruth, ScenarioDefinition


def load_scenario(path: Path) -> ScenarioDefinition:
    source_path = path.resolve()
    with source_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError(f"scenario must be a mapping: {source_path}")

    events = tuple(parse_event(item) for item in raw.pop("events", []))
    ground_truth = GroundTruth.model_validate(raw.pop("ground_truth", {}))
    scenario = ScenarioDefinition.model_validate(
        {
            **raw,
            "source_path": source_path,
            "events": events,
            "ground_truth": ground_truth,
        }
    )
    for event in scenario.events:
        if event.project_id != scenario.project_id:
            raise ValueError(f"event {event.event_id!r} project does not match scenario project")
    return scenario


def discover_scenarios(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(directory.glob("*.yaml")))
