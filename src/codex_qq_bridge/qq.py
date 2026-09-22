from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from qqbot_agent_sdk import (
    MEDIA_TYPE_IMAGE,
    EventParser,
    MediaInfo,
    MediaUploader,
    MessageToCreate,
    QQApiClient,
    QQMessageType,
    QQWebSocket,
    WSCallbacks,
    WSSessionStore,
)

from .config import Config
from .inbound_image import InboundImageBatch, download_inbound_images

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, str, str, InboundImageBatch], Awaitable[None]]
StateHandler = Callable[[], Awaitable[None]]
FatalHandler = Callable[[str], Awaitable[None]]

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_TIMEOUT_MULTIPLIER = 3.0
MIN_HEARTBEAT_TIMEOUT_SECONDS = 30.0
HEARTBEAT_HEALTH_POLL_SECONDS = 1.0


class OfficialQQTransport:
    """Tencent official QQ Bot Gateway and C2C OpenAPI adapter."""

    def __init__(
        self,
        config: Config,
        on_message: MessageHandler,
        on_connected: StateHandler,
        on_disconnected: StateHandler,
        on_fatal: FatalHandler,
    ) -> None:
        self.config = config
        self.on_message = on_message
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_fatal = on_fatal
        self.ready = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._api: QQApiClient | None = None
        self._http: httpx.AsyncClient | None = None
        self._ws: QQWebSocket | None = None
        self._stop = asyncio.Event()
        self._fatal = asyncio.Event()
        self._heartbeat_interval_seconds = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        self._last_heartbeat_ack_at: float | None = None
        self._last_disconnect_reason: str | None = None
        self._session = WSSessionStore(
            base_dir=str(config.state_dir), filename="qq-gateway-session.json"
        )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            self._fatal.clear()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor must keep retrying
                log.warning("Official QQ Bot unavailable: %s", exc)
                self._last_disconnect_reason = str(exc)
                await self._mark_disconnected()
            finally:
                await self._cleanup()
            if not self._stop.is_set():
                await asyncio.sleep(5)

    async def stop(self) -> None:
        self._stop.set()
        await self._cleanup()

    async def send(self, text: str, *, reply_to: str | None = None) -> None:
        if not self.ready or not self._api:
            raise RuntimeError("官方 QQ 机器人 Gateway 未连接")
        chunks = [
            text[index : index + self.config.message_chunk_size]
            for index in range(0, len(text), self.config.message_chunk_size)
        ] or [""]
        for index, chunk in enumerate(chunks):
            await self._api.send_text(
                "c2c",
                self.config.owner_openid,
                chunk,
                reply_to=reply_to if index == 0 else None,
                markdown=False,
                max_length=self.config.message_chunk_size,
                retries=1,
            )

    async def send_image(self, path: Path, *, reply_to: str | None = None) -> None:
        if not self.ready or not self._api or not self._http:
            raise RuntimeError("官方 QQ 机器人 Gateway 未连接")
        uploader = MediaUploader(api_client=self._api, http_client=self._http)
        file_info = await uploader.upload(
            chat_type="c2c",
            chat_id=self.config.owner_openid,
            source=str(path),
            file_type=MEDIA_TYPE_IMAGE,
            file_name=path.name,
        )
        message = MessageToCreate(
            msg_type=QQMessageType.RICH_MEDIA,
            msg_id=reply_to or "",
            msg_seq=self._api.next_msg_seq(),
            media=MediaInfo(file_info=file_info),
        )
        await self._api.post_c2c_message(self.config.owner_openid, message)

    async def _run_once(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0, trust_env=True)
        self._api = QQApiClient(
            self.config.app_id, self.config.app_secret, log_tag="CodexQQ"
        )
        self._api.setup(self._http)
        await self._api.ensure_token()
        gateway_url = await self._api.get_gateway_url()

        def get_session() -> tuple[str | None, int | None]:
            session = self._session.get(self.config.app_id)
            if session.is_resumable and session.is_fresh():
                return session.session_id, session.seq
            return None, None

        def set_session(session_id: str | None, seq: int | None) -> None:
            if session_id:
                self._session.save(self.config.app_id, session_id, seq)
            else:
                self._session.clear(self.config.app_id)

        callbacks = WSCallbacks(
            on_message_event=self._receive,
            on_connected=self._gateway_connected,
            on_disconnected=self._gateway_disconnected,
            on_fatal_error=self._fatal_error,
            get_token=self._api.ensure_token_sync,
            get_session=get_session,
            set_session=set_session,
            set_heartbeat_interval=self._set_heartbeat_interval,
            clear_token=self._api.clear_token,
            fail_pending=lambda reason: log.warning("QQ pending calls failed: %s", reason),
            get_gateway_url=self._api.get_gateway_url_sync,
            on_heartbeat_ack=self._heartbeat_ack,
        )
        self._ws = QQWebSocket(callbacks=callbacks, log_tag="CodexQQ")
        self._ws.start(gateway_url, asyncio.get_running_loop())
        stop_waiter = asyncio.create_task(self._stop.wait())
        fatal_waiter = asyncio.create_task(self._fatal.wait())
        worker_waiter = asyncio.create_task(self._wait_for_ws_worker_exit(self._ws))
        heartbeat_waiter = asyncio.create_task(self._wait_for_heartbeat_timeout())
        done, pending = await asyncio.wait(
            {stop_waiter, fatal_waiter, worker_waiter, heartbeat_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if (
            worker_waiter in done
            and not self._stop.is_set()
            and not self._fatal.is_set()
        ):
            raise RuntimeError("QQ Gateway WebSocket worker exited unexpectedly")
        if (
            heartbeat_waiter in done
            and not self._stop.is_set()
            and not self._fatal.is_set()
        ):
            age, timeout = heartbeat_waiter.result()
            raise RuntimeError(
                "QQ Gateway heartbeat ACK timed out "
                f"(age={age:.1f}s, timeout={timeout:.1f}s)"
            )

    async def _wait_for_ws_worker_exit(self, websocket: QQWebSocket) -> None:
        """Wait for the SDK's daemon worker to exit.

        qqbot-agent-sdk 1.2.2 has no public completion primitive. In particular,
        an exception during the initial WebSocket connection only terminates its
        daemon thread; it does not invoke ``on_fatal_error``. Keep the private
        compatibility check isolated here so the transport supervisor can retry.
        """
        worker = getattr(websocket, "_ws_thread", None)
        if worker is None or not hasattr(worker, "is_alive"):
            raise RuntimeError("QQ Gateway SDK worker status is unavailable")
        while worker.is_alive():
            await asyncio.sleep(0.25)

    async def _wait_for_heartbeat_timeout(self) -> tuple[float, float]:
        while True:
            last_ack = self._last_heartbeat_ack_at
            timeout = self._heartbeat_timeout_seconds()
            if self.ready and last_ack is not None:
                age = monotonic() - last_ack
                if age > timeout:
                    return age, timeout
            await asyncio.sleep(HEARTBEAT_HEALTH_POLL_SECONDS)

    def _heartbeat_timeout_seconds(self) -> float:
        return max(
            MIN_HEARTBEAT_TIMEOUT_SECONDS,
            self._heartbeat_interval_seconds * HEARTBEAT_TIMEOUT_MULTIPLIER,
        )

    def _set_heartbeat_interval(self, interval: float) -> None:
        if interval > 0:
            self._heartbeat_interval_seconds = interval

    def _gateway_connected(self) -> None:
        self._last_heartbeat_ack_at = monotonic()
        self._last_disconnect_reason = None
        self._schedule(self._mark_connected())

    def _gateway_disconnected(self) -> None:
        self._last_disconnect_reason = "QQ Gateway connection interrupted"
        self._schedule(self._mark_disconnected())

    def _heartbeat_ack(self) -> None:
        self._last_heartbeat_ack_at = monotonic()
        self._session.touch(self.config.app_id)

    def health_status(self) -> dict[str, Any]:
        last_ack = self._last_heartbeat_ack_at
        age = monotonic() - last_ack if last_ack is not None else None
        timeout = self._heartbeat_timeout_seconds()
        worker = getattr(self._ws, "_ws_thread", None)
        worker_alive = bool(
            worker is not None
            and hasattr(worker, "is_alive")
            and worker.is_alive()
        )
        if self.ready and age is not None and age > timeout:
            state = "stale"
        elif self.ready:
            state = "connected"
        else:
            state = "disconnected"
        return {
            "state": state,
            "worker_alive": worker_alive,
            "heartbeat_interval_seconds": round(
                self._heartbeat_interval_seconds, 1
            ),
            "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "heartbeat_timeout_seconds": round(timeout, 1),
            "last_error": self._last_disconnect_reason,
        }

    async def _receive(self, event_type: str, raw: dict[str, Any]) -> None:
        event = EventParser().parse(event_type, raw)
        if (
            event is None
            or event.chat_scope != "c2c"
            or event.user_id != self.config.owner_openid
        ):
            return
        attachments = list(event.attachments)
        for element in event.msg_elements:
            attachments.extend(element.attachments)
        images = InboundImageBatch()
        if attachments and self._http:
            images = await download_inbound_images(
                self._http,
                attachments,
                self.config.state_dir / "incoming-images",
                event.message_id,
            )
        await self.on_message(
            event.user_id,
            event.content.strip(),
            event.message_id,
            images,
        )

    def _fatal_error(self, code: str, message: str) -> None:
        self._schedule(self.on_fatal(f"{code}: {message}"))
        loop = self._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._fatal.set)

    def _schedule(self, awaitable: Awaitable[None]) -> None:
        loop = self._loop
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(awaitable, loop)

    async def _mark_connected(self) -> None:
        self.ready = True
        await self.on_connected()

    async def _mark_disconnected(self) -> None:
        was_ready = self.ready
        self.ready = False
        if was_ready:
            await self.on_disconnected()

    async def _cleanup(self) -> None:
        self.ready = False
        ws, self._ws = self._ws, None
        if ws:
            with contextlib.suppress(Exception):
                await ws.async_stop()
        http, self._http = self._http, None
        if http:
            with contextlib.suppress(Exception):
                await http.aclose()
        self._api = None
