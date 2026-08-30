"""Shared Feishu runtime wiring used by the web app and standalone listener."""

from collections.abc import Callable

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import AsyncFeishuClient


def build_feishu_adapter_factory(
    settings: OrgPilotSettings,
) -> Callable[[str], CollaborationAdapter]:
    """Builds the real adapter factory with the configured write gate."""
    client = AsyncFeishuClient(
        app_id=settings.feishu_app_id or "",
        app_secret=settings.feishu_app_secret or "",
        allow_writes=settings.feishu_allow_writes,
    )

    def adapter_factory(project_id: str) -> CollaborationAdapter:
        return FeishuCollaborationAdapter(client=client, project_id=project_id)

    return adapter_factory
