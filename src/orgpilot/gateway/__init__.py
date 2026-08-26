"""FastAPI-based event gateway and REST API for OrgPilot."""

from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService

__all__ = ["GatewayService", "create_app"]
