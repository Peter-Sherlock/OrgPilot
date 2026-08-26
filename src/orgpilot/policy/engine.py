"""Rule-first action policy; planner output cannot grant itself permission."""

from orgpilot.domain.enums import ActionType, PolicyDisposition, RiskLevel
from orgpilot.domain.models import CoordinationAction, PolicyDecision


class PolicyEngine:
    """Classifies action risk independently from candidate generation."""

    def evaluate(self, action: CoordinationAction) -> PolicyDecision:
        if action.action_type in {
            ActionType.ASK_RECOVERY_ESTIMATE,
            ActionType.ASK_CLARIFICATION,
        }:
            return PolicyDecision(
                action_id=action.action_id,
                disposition=PolicyDisposition.ALLOW,
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                reason="private information request does not alter official state",
            )

        if action.action_type is ActionType.PROPOSE_RESCHEDULE:
            return PolicyDecision(
                action_id=action.action_id,
                disposition=PolicyDisposition.ALLOW,
                risk_level=RiskLevel.MEDIUM,
                requires_approval=False,
                reason="proposal is advisory and does not apply a schedule change",
            )

        if action.action_type in {ActionType.NOTIFY_GROUP, ActionType.UPDATE_TASK}:
            return PolicyDecision(
                action_id=action.action_id,
                disposition=PolicyDisposition.REQUIRE_APPROVAL,
                risk_level=RiskLevel.HIGH,
                requires_approval=True,
                reason="action changes shared state or sends a public notification",
            )

        return PolicyDecision(
            action_id=action.action_id,
            disposition=PolicyDisposition.DENY,
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            reason="unknown actions are denied by default",
        )
