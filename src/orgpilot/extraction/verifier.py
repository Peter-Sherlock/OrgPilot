"""Grounding and temporal verification to prevent evidence hallucination and timestamp drift."""

import re
from datetime import datetime, time, timedelta

from orgpilot.extraction.models import (
    ExtractedCommitment,
    ExtractedHealthClaim,
    ExtractionResult,
    MessageContext,
)


class GroundingVerifier:
    """Validates that extracted quotes are verbatim substrings and entities exist in context."""

    def verify_quote(self, message: str, quote: str) -> bool:
        if not quote:
            return False
        clean_msg = re.sub(r"\s+", " ", message).strip().lower()
        clean_quote = re.sub(r"\s+", " ", quote).strip().lower()
        return clean_quote in clean_msg

    def verify_claim(
        self, claim: ExtractedHealthClaim, message: str, context: MessageContext
    ) -> bool:
        if not self.verify_quote(message, claim.source_quote):
            return False
        return claim.task_id in context.known_tasks

    def verify_commitment(
        self, commitment: ExtractedCommitment, message: str, context: MessageContext
    ) -> bool:
        if not self.verify_quote(message, commitment.source_quote):
            return False
        return commitment.target_id in context.known_tasks

    def filter_and_verify(
        self, result: ExtractionResult, message: str, context: MessageContext
    ) -> ExtractionResult:
        """Filters out ungrounded or hallucinated claims from extraction result."""
        valid_claims = [c for c in result.claims if self.verify_claim(c, message, context)]
        valid_commitments = [
            c for c in result.commitments if self.verify_commitment(c, message, context)
        ]
        is_act = bool(valid_claims or valid_commitments)
        return ExtractionResult(
            is_actionable=is_act,
            claims=valid_claims,
            commitments=valid_commitments,
            intent=result.intent,
            reasoning=result.reasoning,
        )


class TemporalResolver:
    """Parses relative Chinese/English temporal expressions anchored to message occurred_at."""

    @staticmethod
    def resolve_relative_time(
        expr: str, anchor: datetime, default_hour: int = 18
    ) -> datetime | None:
        """Resolves relative text into an absolute datetime maintaining anchor timezone."""
        text = expr.strip()
        tz = anchor.tzinfo

        # Direct ISO format check
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=tz)
        except (ValueError, TypeError):
            pass

        # Match '明天' (tomorrow)
        if "明天" in text or "tomorrow" in text.lower():
            target_date = anchor.date() + timedelta(days=1)
            hour, minute = TemporalResolver._parse_hour_minute(text, default_hour)
            return datetime.combine(target_date, time(hour, minute), tzinfo=tz)

        # Match '后天' (day after tomorrow)
        if "后天" in text:
            target_date = anchor.date() + timedelta(days=2)
            hour, minute = TemporalResolver._parse_hour_minute(text, default_hour)
            return datetime.combine(target_date, time(hour, minute), tzinfo=tz)

        # Match '今天' / '今晚' (today / tonight)
        if "今天" in text or "今晚" in text or "today" in text.lower():
            target_date = anchor.date()
            hour, minute = TemporalResolver._parse_hour_minute(text, default_hour)
            return datetime.combine(target_date, time(hour, minute), tzinfo=tz)

        # Match 'N天后' (N days later)
        days_match = re.search(r"(\d+)\s*(天|日)后", text)
        if days_match:
            days = int(days_match.group(1))
            target_date = anchor.date() + timedelta(days=days)
            hour, minute = TemporalResolver._parse_hour_minute(text, default_hour)
            return datetime.combine(target_date, time(hour, minute), tzinfo=tz)

        # Match '周N' / '星期N' (weekday)
        weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        weekday_match = re.search(r"(周|星期|礼拜)([一二三四五六日天])", text)
        if weekday_match:
            target_wd = weekday_map[weekday_match.group(2)]
            current_wd = anchor.weekday()
            days_ahead = (target_wd - current_wd) % 7
            if "下周" in text:
                days_ahead = (7 - current_wd) + target_wd
            target_date = anchor.date() + timedelta(days=days_ahead)
            hour, minute = TemporalResolver._parse_hour_minute(text, default_hour)
            return datetime.combine(target_date, time(hour, minute), tzinfo=tz)

        return None

    @staticmethod
    def _parse_hour_minute(text: str, default_hour: int) -> tuple[int, int]:
        # Check "下午 N 点" / "晚上 N 点"
        pm_match = re.search(r"(下午|晚上)\s*(\d{1,2})\s*(点|时|:)?(\d{1,2})?", text)
        if pm_match:
            h = int(pm_match.group(2))
            if h < 12:
                h += 12
            m = int(pm_match.group(4)) if pm_match.group(4) else 0
            return h, m

        # Check "上午 N 点" / "早上 N 点"
        am_match = re.search(r"(上午|早上)\s*(\d{1,2})\s*(点|时|:)?(\d{1,2})?", text)
        if am_match:
            h = int(am_match.group(2))
            m = int(am_match.group(4)) if am_match.group(4) else 0
            return h, m

        # Check direct "N点"
        hour_match = re.search(r"(\d{1,2})\s*(点|时)", text)
        if hour_match:
            h = int(hour_match.group(1))
            return h, 0

        return default_hour, 0
