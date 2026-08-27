"""Multi-turn slot completeness evaluator and autonomous clarification question generator."""

from orgpilot.domain.enums import HealthStatus
from orgpilot.extraction.models import ExtractionResult


class SlotCompletenessEvaluator:
    """Evaluates whether extracted claims contain all necessary decision slots."""

    @staticmethod
    def evaluate_completeness(
        result: ExtractionResult,
        raw_reply: str,
    ) -> tuple[bool, list[str]]:
        """Determines if the response is sufficient or requires follow-up clarification."""
        if not result.is_actionable or not result.claims:
            # Check if it was an explicit on-track / normal progress indication
            if any(k in raw_reply for k in ["正常", "顺利", "没问题", "按计划", "搞定", "已完成"]):
                return True, []
            return False, ["当前具体进展与预计完成时间"]

        missing_slots: list[str] = []
        for claim in result.claims:
            if claim.health_status in (HealthStatus.DELAYED, HealthStatus.AT_RISK):
                if claim.expected_completion is None:
                    missing_slots.append("预计恢复或完成时间点")
                if not claim.blocker and claim.health_status == HealthStatus.DELAYED:
                    missing_slots.append("具体阻塞根因与卡点")

        return len(missing_slots) == 0, missing_slots

    @staticmethod
    def generate_clarification_question(
        task_title: str,
        missing_slots: list[str],
        raw_reply: str,
    ) -> str:
        if "预计恢复或完成时间点" in missing_slots and "具体阻塞根因与卡点" in missing_slots:
            return (
                f"收到！关于【{task_title}】，请问目前遇到了什么困难？"
                "大概预计需要排查到几点能恢复？"
            )
        if "预计恢复或完成时间点" in missing_slots:
            return (
                f"收到！关于【{task_title}】，"
                "请问预计大概需要排查到什么时候（例如：明天下午5点）？"
            )
        if "具体阻塞根因与卡点" in missing_slots:
            return (
                f"收到！关于【{task_title}】，"
                "请问导致延期的主要卡点是什么？是否需要协调其他同学支持？"
            )
        return (
            f"收到！关于【{task_title}】，"
            "为了评估对下游影响，请补充预计完成时间或当前具体进展。"
        )
