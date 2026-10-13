"""LLM-backed intent client (ADR-0008 §7, step 3b): DeepSeek V4.1 Flash first.

The client implements the ``IntentClient`` protocol with forced tool calling
(``record_intent`` with the ``IntentResult`` JSON schema, ``tool_choice=tool``,
``temperature=0``, ``thinking=disabled`` — the configuration the compatibility
probe settled on; thinking mode is never used). The prompt puts a hashed fixed
prefix FIRST (role, intent taxonomy, orthogonal attributes, R1–R11 judging
rules, output requirements) and the per-message dynamic content AFTER it, so
DeepSeek's automatic context cache hits on every call of a run.

Validation failures append a Pydantic error summary to the conversation and
retry exactly once; a second failure raises and the router falls back. When a
forced call returns no ``tool_use`` block, the documented degradation path
(schema embedded in the prompt, parse the text block) runs once. Every call is
recorded for offline replay under ``evals/intent/recordings/<prompt version>/``
keyed by request hash; replay mode only READS recordings and fails on a miss.
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Mirrors the model definitions in orgpilot.intent.models; used to silently
# DROP hallucinated keys at the LLM boundary (counted), everything else is
# left for Pydantic to reject and drive the single retry.
TOP_LEVEL_FIELDS = {"reasoning", "items", "answers_pending"}
ITEM_FIELDS = {
    "intent",
    "task_id",
    "speech_act",
    "polarity",
    "certainty",
    "health_status",
    "time_expression",
    "expected_completion",
    "blocker",
    "source_quote",
    "confidence",
}


def normalize_intent_payload(
    payload: Any, business_tz: ZoneInfo
) -> tuple[dict[str, Any], dict[str, int]]:
    """Repairs ONLY the two benign LLM output quirks, with per-kind counts.

    - unknown (hallucinated) fields at either level are dropped;
    - a naive ``expected_completion`` string gets the business timezone
      attached.

    Every other malformation is left untouched so Pydantic rejects it and the
    caller can retry with the validation error summary.
    """
    counters = {"unknown_fields_dropped": 0, "naive_datetimes_fixed": 0}

    def _fix_datetime(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value  # unparseable: Pydantic reports the real error
        if parsed.tzinfo is None:
            counters["naive_datetimes_fixed"] += 1
            return parsed.replace(tzinfo=business_tz).isoformat()
        return value

    def _clean_dict(node: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key not in allowed:
                counters["unknown_fields_dropped"] += 1
                continue
            if key == "expected_completion":
                cleaned[key] = _fix_datetime(value)
            elif key == "items" and isinstance(value, list):
                cleaned[key] = [
                    _clean_dict(entry, ITEM_FIELDS) if isinstance(entry, dict) else entry
                    for entry in value
                ]
            else:
                cleaned[key] = value
        return cleaned

    if not isinstance(payload, dict):
        return payload, counters
    return _clean_dict(payload, TOP_LEVEL_FIELDS), counters


def request_hash(payload: dict[str, Any]) -> str:
    """Stable hash over the full request body, used as the recording key."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
