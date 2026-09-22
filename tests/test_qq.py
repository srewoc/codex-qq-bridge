from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import codex_qq_bridge.qq as qq_module
from codex_qq_bridge.config import Config
from codex_qq_bridge.inbound_image import InboundImageBatch
from codex_qq_bridge.qq import OfficialQQTransport


def make_config(tmp_path: Path) -> Config:
    return Config(
        app_id="app",
        app_secret="secret",
        owner_openid="owner",
        control_socket=tmp_path / "bridge.sock",
        state_dir=tmp_path,
        credential_file=tmp_path / "credentials.json",
    )


def make_transport(tmp_path: Path) -> OfficialQQTransport:
    async def state_handler() -> None:
        return

    async def fatal_handler(_reason: str) -> None:
        return

    async def message_handler(
        _user: str, _text: str, _message_id: str, _images: InboundImageBatch
    ) -> None:
        return

    return OfficialQQTransport(
        make_config(tmp_path),
        message_handler,
        state_handler,
        state_handler,
        fatal_handler,
    )


class FakeWorker:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


async def test_ws_worker_exit_is_reported_to_transport_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = make_transport(tmp_path)

    class FakeApi:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def setup(self, _client: object) -> None:
            return

        async def ensure_token(self) -> None:
            return

        async def get_gateway_url(self) -> str:
            return "wss://gateway.example"

        def ensure_token_sync(self) -> str:
            return "token"

        def get_gateway_url_sync(self) -> str:
            return "wss://gateway.example"

        def clear_token(self) -> None:
            return

    class FakeWebSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._ws_thread = FakeWorker(alive=False)

        def start(self, _url: str, _loop: asyncio.AbstractEventLoop) -> None:
            return

        async def async_stop(self) -> None:
            return

    monkeypatch.setattr(qq_module, "QQApiClient", FakeApi)
    monkeypatch.setattr(qq_module, "QQWebSocket", FakeWebSocket)

    with pytest.raises(RuntimeError, match="worker exited unexpectedly"):
        await transport._run_once()
    await transport._cleanup()


async def test_heartbeat_timeout_is_reported_to_transport_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = make_transport(tmp_path)

    class FakeApi:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def setup(self, _client: object) -> None:
            return

        async def ensure_token(self) -> None:
            return

        async def get_gateway_url(self) -> str:
            return "wss://gateway.example"

        def ensure_token_sync(self) -> str:
            return "token"

        def get_gateway_url_sync(self) -> str:
            return "wss://gateway.example"

        def clear_token(self) -> None:
            return

    class FakeWebSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._ws_thread = FakeWorker(alive=True)

        def start(self, _url: str, _loop: asyncio.AbstractEventLoop) -> None:
            return

        async def async_stop(self) -> None:
            self._ws_thread.alive = False

    async def heartbeat_timeout() -> tuple[float, float]:
        return 91.0, 90.0

    monkeypatch.setattr(qq_module, "QQApiClient", FakeApi)
    monkeypatch.setattr(qq_module, "QQWebSocket", FakeWebSocket)
    transport._wait_for_heartbeat_timeout = heartbeat_timeout  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="heartbeat ACK timed out"):
        await transport._run_once()
    await transport._cleanup()


async def test_heartbeat_watchdog_ignores_fresh_ack_and_reports_stale_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = make_transport(tmp_path)
    now = 100.0
    monkeypatch.setattr(qq_module, "monotonic", lambda: now)
    monkeypatch.setattr(qq_module, "MIN_HEARTBEAT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(qq_module, "HEARTBEAT_HEALTH_POLL_SECONDS", 0.001)
    transport.ready = True
    transport._heartbeat_interval_seconds = 10.0
    transport._last_heartbeat_ack_at = now

    waiter = asyncio.create_task(transport._wait_for_heartbeat_timeout())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    now = 131.0
    age, timeout = await asyncio.wait_for(waiter, timeout=1)
    assert age == 31.0
    assert timeout == 30.0


def test_heartbeat_ack_refreshes_health_and_persisted_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = make_transport(tmp_path)
    touched: list[str] = []
    monkeypatch.setattr(qq_module, "monotonic", lambda: 123.0)
    monkeypatch.setattr(transport._session, "touch", touched.append)

    transport._heartbeat_ack()

    assert transport._last_heartbeat_ack_at == 123.0
    assert touched == ["app"]


def test_health_status_exposes_stale_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = make_transport(tmp_path)
    monkeypatch.setattr(qq_module, "monotonic", lambda: 200.0)
    transport.ready = True
    transport._heartbeat_interval_seconds = 30.0
    transport._last_heartbeat_ack_at = 100.0

    status = transport.health_status()

    assert status["state"] == "stale"
    assert status["heartbeat_age_seconds"] == 100.0
    assert status["heartbeat_timeout_seconds"] == 90.0


async def test_run_retries_after_gateway_worker_failure(tmp_path: Path) -> None:
    transport = make_transport(tmp_path)
    attempts = 0
    cleanups = 0

    async def run_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("QQ Gateway WebSocket worker exited unexpectedly")
        transport._stop.set()

    async def cleanup() -> None:
        nonlocal cleanups
        cleanups += 1

    transport._run_once = run_once  # type: ignore[method-assign]
    transport._cleanup = cleanup  # type: ignore[method-assign]

    real_sleep = asyncio.sleep

    async def immediate_sleep(_delay: float) -> None:
        await real_sleep(0)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(asyncio, "sleep", immediate_sleep)
        await transport.run()

    assert attempts == 2
    assert cleanups == 2


async def test_ws_worker_waiter_stops_after_worker_exits(tmp_path: Path) -> None:
    transport = make_transport(tmp_path)
    worker = FakeWorker(alive=True)
    websocket = type("FakeWebSocket", (), {"_ws_thread": worker})()
    waiter = asyncio.create_task(
        transport._wait_for_ws_worker_exit(websocket)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    worker.alive = False
    await asyncio.wait_for(waiter, timeout=1)


async def test_send_image_uploads_and_posts_c2c_rich_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = make_transport(tmp_path)
    uploads: list[dict[str, object]] = []
    posts: list[tuple[str, dict[str, object]]] = []

    class FakeUploader:
        def __init__(self, *, api_client: object, http_client: object) -> None:
            assert api_client is transport._api
            assert http_client is transport._http

        async def upload(self, **kwargs: object) -> str:
            uploads.append(kwargs)
            return "file-info"

    class FakeApi:
        @staticmethod
        def next_msg_seq() -> int:
            return 7

        async def post_c2c_message(self, user_id: str, message: object) -> None:
            posts.append((user_id, message.to_dict()))  # type: ignore[attr-defined]

    monkeypatch.setattr(qq_module, "MediaUploader", FakeUploader)
    transport.ready = True
    transport._api = FakeApi()  # type: ignore[assignment]
    transport._http = object()  # type: ignore[assignment]
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")

    await transport.send_image(image, reply_to="message-id")

    assert uploads == [
        {
            "chat_type": "c2c",
            "chat_id": "owner",
            "source": str(image),
            "file_type": qq_module.MEDIA_TYPE_IMAGE,
            "file_name": "screen.png",
        }
    ]
    assert posts == [
        (
            "owner",
            {
                "msg_type": 7,
                "msg_id": "message-id",
                "msg_seq": 7,
                "media": {"file_info": "file-info"},
            },
        )
    ]
