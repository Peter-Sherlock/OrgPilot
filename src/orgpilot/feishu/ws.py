"""Feishu WebSocket long connection listener for zero-public-network local deployments."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import lark_oapi as lark
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from orgpilot.feishu.webhook import FeishuWebhookHandler

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService

logger = logging.getLogger("orgpilot.feishu.ws")


class FeishuWebSocketListener:
    """Manages Feishu official WebSocket long-connection client and event dispatching."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        gateway_service: Any,
        project_id: str = "feishu-project",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.gateway_service: GatewayService = gateway_service
        self.project_id = project_id
        self.webhook_handler = FeishuWebhookHandler(
            service=gateway_service,
            project_id=project_id,
        )
        self._loop = loop
        self._ws_client: lark.ws.Client | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    def build_event_handler(self) -> lark.EventDispatcherHandler:
        """Constructs Feishu EventDispatcherHandler with message and card action triggers."""
        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.handle_message_event)
            .register_p2_card_action_trigger(self.handle_card_action)
            .build()
        )

    def _run_coroutine(self, coro: Any) -> Any:
        """Executes coroutine safely whether event loop is running in thread or not."""
        loop = self.loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            return loop.create_task(coro)
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=15.0)
        return loop.run_until_complete(coro)

    async def handle_message_event_async(self, data: P2ImMessageReceiveV1) -> None:
        """Processes incoming chat message from WebSocket stream asynchronously."""
        event = data.event
        if not event or not event.message:
            return

        msg = event.message
        sender = event.sender.sender_id if event.sender else None
        event_dict = {
            "message": {
                "message_id": msg.message_id,
                "message_type": msg.message_type,
                "content": msg.content,
                "create_time": msg.create_time,
            },
            "sender": {
                "sender_id": {
                    "open_id": getattr(sender, "open_id", None),
                    "user_id": getattr(sender, "user_id", None),
                }
            },
        }

        await self.webhook_handler._handle_message_received(event_dict)

    def handle_message_event(self, data: P2ImMessageReceiveV1) -> None:
        """Synchronous entry point called by lark-oapi worker thread."""
        try:
            self._run_coroutine(self.handle_message_event_async(data))
        except Exception as e:
            logger.error("Error ingesting message via WS: %s", e)

    async def handle_card_action_async(self, data: P2CardActionTrigger) -> Any:
        """Processes interactive card button callbacks asynchronously."""
        event = data.event
        if not event or not event.action:
            return None

        event_dict = {
            "action": {
                "value": event.action.value,
            },
            "operator": {
                "open_id": event.operator.open_id if event.operator else None,
            },
        }

        result = await self.webhook_handler._handle_card_action(event_dict)
        if not result or result.get("code", 0) != 0:
            logger.warning("WS card action returned status: %s", result)
            return None

        card_dict = result.get("card")
        resp = P2CardActionTriggerResponse()
        resp.card = CallBackCard({"type": "raw", "data": card_dict})
        if "toast" in result and isinstance(result["toast"], dict):
            resp.toast = CallBackToast(result["toast"])
        return resp

    def handle_card_action(self, data: P2CardActionTrigger) -> Any:
        """Synchronous entry point called by lark-oapi worker thread."""
        try:
            return self._run_coroutine(self.handle_card_action_async(data))
        except Exception as e:
            logger.error("Error handling card action via WS: %s", e)
            return None

    def start(self) -> lark.ws.Client:
        """Initializes and starts the background WebSocket connection."""
        event_handler = self.build_event_handler()
        self._ws_client = lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        self._ws_client.start()
        logger.info("Feishu WebSocket long connection client started successfully!")
        return self._ws_client
