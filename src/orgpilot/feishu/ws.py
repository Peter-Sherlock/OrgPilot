"""Feishu WebSocket long connection listener for zero-public-network local deployments."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import lark_oapi as lark
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.core.log import logger as sdk_logger
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from orgpilot.feishu.webhook import FeishuWebhookHandler

if TYPE_CHECKING:
    from orgpilot.feishu.acceptance import AcceptanceWindow
    from orgpilot.gateway.service import GatewayService

logger = logging.getLogger("orgpilot.feishu.ws")


class _SafeSdkLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Even ERROR exceptions can contain authenticated WebSocket URLs.
        record.msg = "Feishu SDK log (details suppressed)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class FeishuWebSocketListener:
    """Manages Feishu official WebSocket long-connection client and event dispatching."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        gateway_service: Any = None,
        project_id: str = "feishu-project",
        loop: asyncio.AbstractEventLoop | None = None,
        demo_bootstrap: bool = False,
        acceptance: AcceptanceWindow | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.gateway_service: GatewayService = gateway_service
        self.project_id = project_id
        self.acceptance = acceptance
        if acceptance is None and gateway_service is None:
            raise ValueError("GatewayService is required outside L2 acceptance")
        self.webhook_handler = (
            FeishuWebhookHandler(
                service=gateway_service, project_id=project_id, demo_bootstrap=demo_bootstrap
            )
            if acceptance is None
            else None
        )
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = loop
        self._ws_client: lark.ws.Client | None = None
        self._ws_thread: threading.Thread | None = None
        self._loop_thread: threading.Thread | None = None
        self._owns_loop_thread = False
        self._owns_loop = False
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = threading.Event()
        self._sdk_finishing = threading.Event()
        self._sdk_cleanup_ok = True
        self._started = False
        self._sdk_log_filter = _SafeSdkLogFilter()
        self._ws_error_type: str | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                self._owns_loop = True
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

    def _ensure_service_loop_running(self) -> None:
        """Runs a caller-provided dormant loop on its own thread for SDK callbacks."""
        loop = self.loop
        if loop.is_running():
            return

        ready = threading.Event()

        def _loop_worker() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_loop_worker,
            name="FeishuServiceEventLoopThread",
            daemon=True,
        )
        self._owns_loop_thread = True
        self._loop_thread.start()
        if not ready.wait(5.0):
            raise RuntimeError("Feishu service event loop did not start")

    async def handle_message_event_async(self, data: P2ImMessageReceiveV1) -> None:
        """Processes incoming chat message from WebSocket stream asynchronously."""
        event = data.event
        if self._stopping.is_set() or not event or not event.message:
            return

        msg = event.message
        sender = event.sender.sender_id if event.sender else None
        event_dict = {
            "message": {
                "message_id": msg.message_id,
                "message_type": msg.message_type,
                "content": msg.content,
                "create_time": msg.create_time,
                "chat_type": getattr(msg, "chat_type", None),
            },
            "sender": {
                "sender_id": {
                    "open_id": getattr(sender, "open_id", None),
                    "user_id": getattr(sender, "user_id", None),
                }
            },
        }

        if self.acceptance is not None:
            await self.acceptance.handle_message(
                event_dict["message"],
                {
                    "open_id": getattr(sender, "open_id", None),
                    "sender_type": getattr(event.sender, "sender_type", None),
                },
            )
            return
        await self.webhook_handler._handle_message_received(event_dict)

    def handle_message_event(self, data: P2ImMessageReceiveV1) -> None:
        """Synchronous entry point called by lark-oapi worker thread."""
        if self._stopping.is_set():
            return
        try:
            self._run_coroutine(self.handle_message_event_async(data))
        except Exception as e:
            logger.error("Error ingesting message via WS: %s", type(e).__name__)

    async def handle_card_action_async(self, data: P2CardActionTrigger) -> Any:
        """Processes interactive card button callbacks asynchronously."""
        if self._stopping.is_set():
            return None
        if self.acceptance is not None:
            event = data.event
            if event is None:
                return None
            result = await self.acceptance.handle_card(
                {
                    "operator_id": getattr(event.operator, "open_id", None),
                    "message_id": getattr(event.context, "open_message_id", None),
                    "value": getattr(event.action, "value", None),
                }
            )
            return P2CardActionTriggerResponse(result) if result else None
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
        if self._stopping.is_set():
            return None
        try:
            return self._run_coroutine(self.handle_card_action_async(data))
        except Exception as e:
            logger.error("Error handling card action via WS: %s", type(e).__name__)
            return None

    def start(self) -> lark.ws.Client:
        """Initializes and starts the background WebSocket connection in a daemon thread."""
        if self._started or self._stopping.is_set():
            raise RuntimeError("Feishu listener instances are single-use")
        self._started = True
        try:
            self._ensure_service_loop_running()
            event_handler = self.build_event_handler()
        except Exception:
            self.stop()
            raise
        sdk_logger.addFilter(self._sdk_log_filter)
        client_ready = threading.Event()

        def _ws_thread_worker() -> None:
            thread_loop = asyncio.new_event_loop()
            self._ws_loop = thread_loop
            asyncio.set_event_loop(thread_loop)
            thread_loop.set_exception_handler(
                lambda _loop, context: logger.error(
                    "Feishu WS asynchronous failure: %s",
                    type(context.get("exception")).__name__,
                )
            )
            import lark_oapi.ws.client as ws_module

            previous_sdk_loop = ws_module.loop
            ws_module.loop = thread_loop
            try:
                # ExpiringCache creates a task in Client.__init__. Construct it on
                # the SDK loop/thread, never on the caller or service loop.
                self._ws_client = lark.ws.Client(
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                    event_handler=event_handler,
                    # INFO exposes credential-bearing connection URLs.
                    log_level=lark.LogLevel.ERROR,
                    auto_reconnect=self.acceptance is None,
                )
                client_ready.set()
                if not self._stopping.is_set():
                    print(
                        "[*] [Feishu WS] Connecting to Feishu WebSocket stream "
                        "(credentials configured; values hidden)...",
                        flush=True,
                    )
                    self._ws_client.start()
            except (Exception, asyncio.CancelledError) as e:
                if not self._stopping.is_set():
                    self._ws_error_type = type(e).__name__
                    logger.error("Feishu WS client error: %s", type(e).__name__)
            finally:
                self._sdk_finishing.set()
                client_ready.set()  # Constructor failures must also release start().
                try:
                    thread_loop.run_until_complete(self._shutdown_sdk())
                except Exception as exc:
                    self._sdk_cleanup_ok = False
                    logger.warning("Feishu SDK cleanup error: %s", type(exc).__name__)
                finally:
                    thread_loop.close()
                    if ws_module.loop is thread_loop:
                        ws_module.loop = previous_sdk_loop

        try:
            self._ws_thread = threading.Thread(
                target=_ws_thread_worker,
                name="FeishuWebSocketListenerThread",
                daemon=True,
            )
            self._ws_thread.start()
            if not client_ready.wait(5.0):
                raise RuntimeError("Feishu SDK client initialization timed out")
            if self._ws_client is None:
                raise RuntimeError("Feishu SDK client initialization failed")
        except Exception:
            self.stop()
            raise
        logger.info("Feishu WebSocket long connection client thread launched successfully!")
        return self._ws_client

    def wait_until_connected(self, timeout_seconds: float) -> bool:
        """Waits for the SDK connection object without logging its credential-bearing URL."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            client = self._ws_client
            if client is not None and getattr(client, "_conn", None) is not None:
                return True
            thread = self._ws_thread
            if thread is not None and not thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    @property
    def connection_error_type(self) -> str | None:
        """Returns only the connection exception class, never its secret-bearing details."""
        return self._ws_error_type

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def _cancel_pending(cls) -> None:
        await cls._cancel_tasks(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        )

    async def _shutdown_sdk(self) -> None:
        """Runs only on the SDK loop, after Client.start() has returned/unwound."""
        try:
            client = self._ws_client
            if client is not None:
                client._auto_reconnect = False
                # Cancel the SDK receiver before closing its socket, so an intentional
                # close isn't logged/re-raised as ConnectionClosedOK. Leave transport
                # internals alive until _disconnect completes the close handshake.
                receive_code = getattr(
                    getattr(client, "_receive_message_loop", None), "__code__", None
                )
                if receive_code is not None:
                    await self._cancel_tasks(
                        [
                            task
                            for task in asyncio.all_tasks()
                            if getattr(task.get_coro(), "cr_code", None) is receive_code
                        ]
                    )
                disconnect = getattr(client, "_disconnect", None)
                if not callable(disconnect):
                    raise RuntimeError("SDK disconnect interface unavailable")
                async with asyncio.timeout(3.0):
                    await disconnect()
        finally:
            # Includes the real SDK's ExpiringCache cron, ping, and root task.
            await self._cancel_pending()

    def _request_sdk_stop(self) -> None:
        # Executed on the SDK thread. A queued stop must not interrupt final cleanup.
        if not self._sdk_finishing.is_set():
            self._ws_loop.stop()
            # Client.start() uses successive run_until_complete calls. A stop in
            # the final connect tick can be consumed before _select starts. Keep
            # the stop request latched until the worker actually enters cleanup.
            self._ws_loop.call_later(0.01, self._request_sdk_stop)

    def stop(self) -> bool:
        """Revoke acceptance, disconnect on the SDK loop, then join owned threads.

        A blocked SDK handshake may outlive the join deadline. Return False in
        that case instead of claiming shutdown succeeded; the write window is closed.
        """
        already_stopping = self._stopping.is_set()
        self._stopping.set()
        cleanup_ok = True
        if self.acceptance is not None:
            self.acceptance.close()
        client = self._ws_client
        ws_loop = self._ws_loop
        if client is not None:
            client._auto_reconnect = False
        if not already_stopping and ws_loop is not None and not ws_loop.is_closed():
            try:
                ws_loop.call_soon_threadsafe(self._request_sdk_stop)
            except RuntimeError:
                # The worker may have finished between the check and scheduling.
                cleanup_ok = ws_loop.is_closed()
        thread = self._ws_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=4.0)
        stopped = thread is None or not thread.is_alive()
        loop_thread = self._loop_thread
        if self._owns_loop_thread and self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._cancel_pending(), self._loop)
            try:
                future.result(timeout=3.0)
            except Exception as exc:
                future.cancel()
                cleanup_ok = False
                logger.warning("Feishu service cleanup error: %s", type(exc).__name__)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if loop_thread is not None and loop_thread.is_alive():
            loop_thread.join(timeout=3.0)
        service_stopped = loop_thread is None or not loop_thread.is_alive()
        if self._owns_loop and service_stopped and not self._loop.is_closed():
            self._loop.close()
        if stopped:
            sdk_logger.removeFilter(self._sdk_log_filter)
        confirmed = stopped and service_stopped and cleanup_ok and self._sdk_cleanup_ok
        logger.info("Feishu listener shutdown confirmed=%s", confirmed)
        return confirmed
