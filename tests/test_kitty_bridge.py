from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codex_qq_bridge.cli import (
    inspect_hook_definitions,
    install_hooks,
)
from codex_qq_bridge.config import Config
from codex_qq_bridge.inbound_image import ImageRejection, InboundImageBatch
from codex_qq_bridge.kitty import (
    ALLOWED_KEYS,
    KittyAdapter,
    KittyError,
    KittyImagePasteError,
    sanitize_screen,
)
from codex_qq_bridge.kitty_bridge import HELP_TEXT, KittyBridge


class FakeQQ:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.sent: list[tuple[str, str | None]] = []
        self.images: list[tuple[bytes, str | None]] = []
        self.events: list[tuple[str, object]] = []
        self.error: Exception | None = None
        self.image_error: Exception | None = None

    async def run(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def send(self, text: str, *, reply_to: str | None = None) -> None:
        if self.error:
            raise self.error
        self.sent.append((text, reply_to))
        self.events.append(("text", text))

    async def send_image(self, path: Path, *, reply_to: str | None = None) -> None:
        if self.image_error:
            raise self.image_error
        content = path.read_bytes()
        self.images.append((content, reply_to))
        self.events.append(("image", content))

    def health_status(self) -> dict[str, object]:
        return {"state": "connected" if self.ready else "disconnected"}


class FakeKitty:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.screen = "Codex screen"
        self.invalid = False
        self.send_error: Exception | None = None
        self.screen_error: Exception | None = None
        self.launch_window_id = 9
        self.sockets = ["unix:/run/user/1000/codex-qq-kitty-1"]

    async def validate(
        self, socket: str, window_id: int, kind: str = "codex"
    ) -> None:
        self.calls.append(("validate", socket, window_id, kind))
        if self.invalid:
            raise RuntimeError("offline")

    async def send_text(self, socket: str, window_id: int, text: str) -> None:
        if self.send_error:
            raise self.send_error
        self.calls.append(("text", socket, window_id, text))

    async def send_images(
        self, socket: str, window_id: int, paths: list[Path], prompt: str
    ) -> None:
        if self.send_error:
            raise self.send_error
        self.calls.append(("images", socket, window_id, paths, prompt))

    async def send_keys(
        self,
        socket: str,
        window_id: int,
        keys: list[str],
        blocked: frozenset[str] = frozenset(),
    ) -> None:
        if self.send_error:
            raise self.send_error
        self.calls.append(("keys", socket, window_id, keys, blocked))

    async def get_text(self, socket: str, window_id: int, limit: int) -> str:
        if self.screen_error:
            raise self.screen_error
        self.calls.append(("screen", socket, window_id, limit))
        return self.screen

    async def launch_shell(self, socket: str, cwd: Path, title: str) -> int:
        self.calls.append(("launch", socket, cwd, title))
        return self.launch_window_id

    async def managed_sockets(self) -> list[str]:
        self.calls.append(("managed_sockets",))
        return self.sockets


def make_config(tmp_path: Path) -> Config:
    return Config(
        app_id="app",
        app_secret="secret",
        owner_openid="owner",
        control_socket=tmp_path / "bridge.sock",
        state_dir=tmp_path,
        credential_file=tmp_path / "credentials.json",
    )


async def register(
    bridge: KittyBridge,
    session: str = "session-1",
    *,
    window_id: str = "7",
    transcript_path: str = "/session.jsonl",
) -> None:
    await bridge.register_kitty(
        {
            "session_id": session,
            "cwd": "/project",
            "transcript_path": transcript_path,
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": window_id,
        }
    )
    # The connect notice is delivered off the critical path; settle it here so
    # callers observe a fully registered binding.
    await asyncio.gather(*list(bridge._notify_tasks), return_exceptions=True)


def with_hook_identity(
    event: dict[str, object],
    *,
    transcript_path: str = "/session.jsonl",
    window_id: str = "7",
) -> dict[str, object]:
    result = dict(event)
    payload = dict(result.get("payload") or {})
    payload["transcript_path"] = transcript_path
    result["payload"] = payload
    result["kitty_socket"] = "unix:/run/user/1000/codex-qq-kitty-1"
    result["kitty_window_id"] = window_id
    return result


def write_plan_transcript(path: Path, turn_id: str, text: str) -> None:
    records = [
        {"type": "not-json-shape"},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session-1",
                "turn_id": "different-turn",
                "item": {"type": "Plan", "text": "wrong plan"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session-1",
                "turn_id": turn_id,
                "item": {"type": "Plan", "text": text},
            },
        },
    ]
    path.write_text(
        "{broken json\n" + "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


async def complete_qq_new_registration(
    bridge: KittyBridge,
    kitty: FakeKitty,
    *,
    session: str = "session-new",
    window_id: int = 9,
) -> None:
    # Real sleeps: the launch path waits for the TUI screen to settle.
    for _ in range(300):
        await asyncio.sleep(0.01)
        if any(call[0] == "text" and call[-1] == "qq-connect" for call in kitty.calls):
            break
    assert any(call[0] == "text" and call[-1] == "qq-connect" for call in kitty.calls)
    await bridge.register_kitty(
        {
            "session_id": session,
            "cwd": "/ignored-for-pending-launch",
            "transcript_path": f"/{session}.jsonl",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": str(window_id),
        }
    )


async def test_mobile_image_is_pasted_into_active_codex(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)
    await register(bridge)
    image = tmp_path / "incoming.png"
    image.write_bytes(b"image")

    await bridge._on_qq_message(
        "owner", "解释这张图", "image-message", [image]
    )

    assert (
        "images",
        "unix:/run/user/1000/codex-qq-kitty-1",
        7,
        [image],
        "解释这张图",
    ) in kitty.calls
    assert qq.sent[-1] == (
        "已向 codex 提交 1 张图片和文字说明",
        "image-message",
    )


async def test_mobile_image_without_binding_reports_failure(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())

    await bridge._on_qq_message(
        "owner", "", "image-message", [tmp_path / "incoming.png"]
    )

    assert qq.sent == [("当前会话离线，图片未发送", "image-message")]


async def test_rejected_mobile_images_report_reason_without_submission(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()
    batch = InboundImageBatch(
        candidate_count=2,
        rejected=[ImageRejection(index=1, reason="too_large")],
        truncated_count=1,
    )

    await bridge._on_qq_message("owner", "", "bad-images", batch)

    assert qq.sent == [
        ("图片未发送：文件超过 20 MB 1 张；超过每条消息 4 张限制 1 张", "bad-images")
    ]
    assert not any(call[0] == "images" for call in kitty.calls)


async def test_partial_download_report_is_included_in_success_reply(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    image = tmp_path / "incoming.png"
    image.write_bytes(b"image")
    batch = InboundImageBatch(
        paths=[image],
        candidate_count=2,
        rejected=[ImageRejection(index=2, reason="invalid_image")],
    )

    await bridge._on_qq_message("owner", "分析图片", "partial-images", batch)

    assert qq.sent[-1] == (
        "已向 codex 提交 1 张图片和文字说明；另有图片未处理：图片已损坏或内容无效 1 张",
        "partial-images",
    )


async def test_image_paste_failure_does_not_mark_binding_offline(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.send_error = KittyImagePasteError("第 2 张图片未能粘贴到 Codex", attached_count=1)
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    images = [tmp_path / "one.png", tmp_path / "two.png"]

    await bridge._on_qq_message("owner", "比较图片", "paste-failed", images)

    assert bridge.bindings["session-1"].state == "idle"
    assert qq.sent[-1] == (
        (
            "图片未提交：第 2 张图片未能粘贴到 Codex；已有 1 张留在输入框但未提交，"
            "可在电脑端清理或发送 /qq-key ctrl+c"
        ),
        "paste-failed",
    )


def test_screen_sanitizing_removes_ansi_and_keeps_tail() -> None:
    assert sanitize_screen("\x1b[31msecret\x1b[0m\x00", 20) == "secret"
    assert sanitize_screen("0123456789", 4).startswith("6789")


def test_help_lists_allowed_key_parameters() -> None:
    for key in ALLOWED_KEYS:
        assert key in HELP_TEXT
    assert "/qq-key down enter" in HELP_TEXT
    assert "/qq-screen-text" in HELP_TEXT
    assert "/qq-new <命令> [路径]" in HELP_TEXT


async def test_qq_new_requires_absolute_existing_directory(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_message("owner", "/qq-new codex relative/path", "relative")
    await bridge._on_qq_message("owner", "/qq-new codex /path/that/does/not/exist", "absent")

    assert "只接受绝对路径" in qq.sent[-2][0]
    assert "目录不可用" in qq.sent[-1][0]
    assert not any(call[0] == "launch" for call in kitty.calls)


async def test_qq_new_launches_codex_and_auto_binds_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "OpenAI Codex\nAsk Codex to do anything"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    project = tmp_path / "project with spaces"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    launch = asyncio.create_task(
        bridge._on_qq_message("owner", "/qq-new codex ~/project with spaces", "new-id")
    )
    await complete_qq_new_registration(bridge, kitty)
    await launch

    assert bridge.active_session_id == "session-new"
    assert bridge.bindings["session-new"].cwd == str(project)
    assert bridge.bindings["session-new"].kitty_window_id == 9
    assert not bridge._pending_launches
    launch_call = next(call for call in kitty.calls if call[0] == "launch")
    assert launch_call == (
        "launch",
        "unix:/run/user/1000/codex-qq-kitty-1",
        project,
        "codex",
    )
    socket = "unix:/run/user/1000/codex-qq-kitty-1"
    # The command is typed into the fresh shell, then qq-connect once ready.
    assert ("text", socket, 9, "codex") in kitty.calls
    assert ("text", socket, 9, "qq-connect") in kitty.calls
    assert kitty.calls.index(("text", socket, 9, "codex")) < kitty.calls.index(
        ("text", socket, 9, "qq-connect")
    )
    assert qq.sent[-1][1] == "new-id"
    assert "会话已新建并设为当前" in qq.sent[-1][0]


async def test_qq_new_without_path_uses_active_binding_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "OpenAI Codex"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    project = tmp_path / "current-project"
    project.mkdir()
    await bridge.register_kitty(
        {
            "session_id": "session-old",
            "cwd": str(project),
            "transcript_path": "/session.jsonl",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "7",
        }
    )
    launch = asyncio.create_task(
        bridge._on_qq_message("owner", "/qq-new codex", "new-default")
    )
    await complete_qq_new_registration(bridge, kitty)
    await launch

    assert bridge.bindings["session-new"].cwd == str(project)
    assert bridge.active_session_id == "session-new"


async def test_qq_new_discovers_single_managed_socket_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "OpenAI Codex"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    launch = asyncio.create_task(
        bridge._on_qq_message("owner", f"/qq-new codex {project}", "new-id")
    )
    await complete_qq_new_registration(bridge, kitty)
    await launch

    assert bridge.active_session_id == "session-new"
    assert ("managed_sockets",) in kitty.calls


async def test_session_start_without_pending_launch_is_not_auto_bound(
    tmp_path: Path,
) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]

    result = await bridge.hook_event(
        {
            "event": "session_start",
            "session_id": "session-new",
            "payload": {},
            "event_id": "untrusted-start-without-token",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "9",
        }
    )

    assert result == {"ok": True, "ignored": "session_not_bound"}
    assert not bridge.bindings


async def test_qq_new_waits_for_launch_then_sends_qq_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "OpenAI Codex"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    project = tmp_path / "early-session-start"
    project.mkdir()
    launch_started = asyncio.Event()
    release_launch = asyncio.Event()

    async def delayed_launch(socket: str, cwd: Path, title: str) -> int:
        kitty.calls.append(("launch", socket, cwd, title))
        launch_started.set()
        await release_launch.wait()
        return 9

    monkeypatch.setattr(kitty, "launch_shell", delayed_launch)
    launch = asyncio.create_task(
        bridge._on_qq_message("owner", f"/qq-new codex {project}", "early-new")
    )
    await launch_started.wait()
    early = await bridge.hook_event(
        {
            "event": "session_start",
            "session_id": "session-early",
            "payload": {"transcript_path": "/early.jsonl"},
            "event_id": "early-session-start",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "9",
        }
    )
    assert early == {"ok": True, "ignored": "session_not_bound"}
    assert not any(call[0] == "text" and call[-1] == "qq-connect" for call in kitty.calls)
    release_launch.set()
    await complete_qq_new_registration(
        bridge,
        kitty,
        session="session-connected",
    )
    await launch

    assert bridge.active_session_id == "session-connected"
    assert bridge.bindings["session-connected"].kitty_window_id == 9
    assert not bridge._pending_launches
    assert "会话已新建并设为当前" in qq.sent[-1][0]


async def test_explicit_screen_is_sent_as_png_and_temp_file_is_removed(
    tmp_path: Path,
) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_message("owner", "/qq-screen", "screen-id")

    assert len(qq.images) == 1
    image, reply_to = qq.images[0]
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert reply_to == "screen-id"
    assert not list((tmp_path / "screens").glob("*.png"))


async def test_screen_text_keeps_original_text_response(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_message("owner", "/qq-screen-text", "screen-text-id")

    assert qq.images == []
    assert qq.sent[-1] == ("[codex #1] [screen]\nCodex screen", "screen-text-id")


async def test_screen_image_failure_falls_back_to_text_and_cleans_temp_file(
    tmp_path: Path,
) -> None:
    qq = FakeQQ()
    qq.image_error = RuntimeError("upload failed")
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_message("owner", "/qq-screen", "screen-id")

    assert qq.sent[-1][1] == "screen-id"
    assert "图片发送失败" in qq.sent[-1][0]
    assert "Codex screen" in qq.sent[-1][0]
    assert not list((tmp_path / "screens").glob("*.png"))


async def test_register_and_inject_use_exact_window(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    await bridge._on_qq_message("owner", "hello", "m1")

    assert bridge.active_session_id == "session-1"
    assert ("text", "unix:/run/user/1000/codex-qq-kitty-1", 7, "hello") in kitty.calls
    assert "项目：/project" in qq.sent[0][0]
    assert qq.images == []


async def test_phone_slash_command_is_injected_and_returns_screen_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge._on_qq_message("owner", "/model", "m1")

    assert ("text", "unix:/run/user/1000/codex-qq-kitty-1", 7, "/model") in kitty.calls
    assert len(qq.images) == 1
    image, reply_to = qq.images[0]
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert reply_to == "m1"
    assert not any("[screen]" in text for text, _ in qq.sent)


@pytest.mark.parametrize(
    ("command", "expected_keys", "confirmation"),
    [
        ("/qq-key down enter", ["down", "enter"], "按键已发送"),
        ("/qq-stop", ["ctrl+c"], "已请求停止当前任务"),
    ],
)
async def test_key_commands_return_screen_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected_keys: list[str],
    confirmation: str,
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge._on_qq_message("owner", command, "key-message")

    assert (
        "keys",
        "unix:/run/user/1000/codex-qq-kitty-1",
        7,
        expected_keys,
        frozenset(),
    ) in kitty.calls
    assert (confirmation, "key-message") in qq.sent
    assert len(qq.images) == 1
    image, reply_to = qq.images[0]
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert reply_to == "key-message"


async def test_invalid_key_command_does_not_return_screen_image(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_message("owner", "/qq-key invalid", "invalid-key")

    assert qq.sent[-1] == (
        "非法按键；发送 /qq-help 查看允许的按键",
        "invalid-key",
    )
    assert qq.images == []


async def test_qq_prefixed_commands_are_reserved_and_old_commands_reach_tui(
    tmp_path: Path,
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_message("owner", "/qq-help", "help")
    await bridge._on_qq_message("owner", "/help", "native-help")
    await bridge._on_qq_message("owner", "/qq-unknown", "unknown")

    assert qq.sent[-1] == ("未知 QQ 命令；发送 /qq-help 查看可用命令", "unknown")
    assert HELP_TEXT in [text for text, _ in qq.sent]
    assert any(call[-1] == "/help" for call in kitty.calls if call[0] == "text")


async def test_pending_injections_are_fifo_for_identical_text(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    await bridge._inject_text("same")
    await bridge._inject_text("same")
    binding = bridge.bindings["session-1"]
    assert len(binding.pending_injections) == 2

    event = with_hook_identity(
        {
            "event": "user_prompt_submit",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "payload": {"prompt": "same"},
            "event_id": "one",
        }
    )
    await bridge.hook_event(event)
    event["event_id"] = "two"
    await bridge.hook_event(event)

    assert not binding.pending_injections
    assert all("电脑：same" not in text for text, _ in qq.sent)


PHONE_MESSAGE = "hr问上一份工作的离职原因，怎么委婉回答，就是合同到期没续约。"


def wrap_pasted(text: str, block_id: str = "80a7", *, id_in_close: bool = True) -> str:
    close = f'</pasted_content id="{block_id}">' if id_in_close else "</pasted_content>"
    return f'<pasted_content id="{block_id}">\n{text}\n{close}'


async def test_folded_paste_is_recognised_as_own_injection(tmp_path: Path) -> None:
    """A long phone message comes back wrapped and must not echo as 电脑 input."""
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    await bridge._inject_text(PHONE_MESSAGE)
    binding = bridge.bindings["session-1"]

    await bridge.hook_event(
        with_hook_identity(
            {
                "event": "user_prompt_submit",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "payload": {"prompt": wrap_pasted(PHONE_MESSAGE)},
                "event_id": "folded",
            }
        )
    )

    assert not binding.pending_injections
    assert all("电脑：" not in text for text, _ in qq.sent)


async def test_desktop_paste_is_echoed_without_wrapper(tmp_path: Path) -> None:
    """Text really typed on the desktop still echoes, but without the tags."""
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    await bridge.hook_event(
        with_hook_identity(
            {
                "event": "user_prompt_submit",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "payload": {"prompt": wrap_pasted("desktop notes", "beef")},
                "event_id": "desktop",
            }
        )
    )

    echoed = [text for text, _ in qq.sent if "电脑：" in text]
    assert echoed and echoed[-1].endswith("电脑：desktop notes")
    assert "pasted_content" not in echoed[-1]


def test_unwrap_pasted_content_variants() -> None:
    unwrap = KittyBridge._unwrap_pasted_content
    assert unwrap(wrap_pasted("hello")) == "hello"
    assert unwrap(wrap_pasted("hello", id_in_close=False)) == "hello"
    assert unwrap("plain prompt") == "plain prompt"
    # Mismatched ids are not a wrapper this bridge produced.
    mismatched = '<pasted_content id="a">x</pasted_content id="b">'
    assert unwrap(mismatched) == mismatched


async def test_stop_hook_is_deduplicated(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    event = with_hook_identity(
        {
            "event": "stop",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "payload": {"last_assistant_message": "done"},
            "event_id": "stable-event",
        }
    )
    await bridge.hook_event(event)
    await bridge.hook_event(event)
    assert [text for text, _ in qq.sent].count("[codex #1] done") == 1


async def test_request_user_input_reads_tool_input_questions(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    await bridge.hook_event(
        with_hook_identity({
            "event": "pre_tool_use",
            "session_id": "session-1",
            "event_id": "question-1",
            "payload": {
                "tool_input": {
                    "questions": [
                        {
                            "header": "兼容模式",
                            "question": "选择？",
                            "options": [
                                {"label": "A", "description": "保留兼容审批"},
                                {"label": "B", "description": ""},
                            ],
                        }
                    ]
                }
            },
        })
    )
    assert "兼容模式\n选择？\n\n1. A\n   保留兼容审批\n2. B" in qq.sent[-1][0]
    assert "/qq-key" in qq.sent[-1][0]
    assert "None" not in qq.sent[-1][0]


async def test_plan_stop_sends_transcript_plan_then_screen_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = (
        "previous output\nImplement this plan?\n"
        "1. Yes, implement this plan\n"
        "2. Yes, clear context and implement\n"
        "3. No, stay in Plan mode"
    )
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    transcript = tmp_path / "session.jsonl"
    write_plan_transcript(transcript, "turn-plan", "# Exact plan\n\nDo the work.")
    await register(bridge, transcript_path=str(transcript))
    qq.sent.clear()
    qq.events.clear()

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(
        with_hook_identity({
            "event": "stop",
            "session_id": "session-1",
            "turn_id": "turn-plan",
            "event_id": "plan-stop",
            "payload": {
                "permission_mode": "default",
                "last_assistant_message": None,
            },
        }, transcript_path=str(transcript))
    )
    tasks = list(bridge._plan_probe_tasks.values())
    await asyncio.gather(*tasks)

    notices = [text for text, _ in qq.sent if "[计划内容]" in text]
    assert notices == ["[codex #1] [计划内容]\n# Exact plan\n\nDo the work."]
    assert "Implement this plan?" not in notices[0]
    assert len(qq.images) == 1
    image, reply_to = qq.images[0]
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert reply_to is None
    assert [event for event, _ in qq.events] == ["text", "image"]


async def test_normal_stop_in_plan_mode_forwards_message_without_plan_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "ordinary response\nPlan mode"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(
        with_hook_identity({
            "event": "stop",
            "session_id": "session-1",
            "turn_id": "turn-normal",
            "event_id": "normal-stop",
            "payload": {"permission_mode": "default", "last_assistant_message": "done"},
        })
    )
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))
    assert any(text.endswith("done") for text, _ in qq.sent)
    assert not any("[计划完成]" in text for text, _ in qq.sent)
    assert qq.images == []


async def test_normal_stop_with_stale_confirmation_menu_is_not_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "Implement this plan?\n1. Yes\n3. No, stay in Plan mode"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(with_hook_identity({
        "event": "stop",
        "session_id": "session-1",
        "turn_id": "turn-normal-stale-menu",
        "event_id": "normal-stop-stale-menu",
        "payload": {
            "permission_mode": "default",
            "last_assistant_message": "ordinary final response",
        },
    }))
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))

    assert any(text.endswith("ordinary final response") for text, _ in qq.sent)
    assert not any("[计划内容]" in text for text, _ in qq.sent)
    assert qq.images == []


async def test_plan_stop_without_confirmation_menu_does_not_send_screen_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "plan output without confirmation menu\nDefault mode"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(
        with_hook_identity({
            "event": "stop",
            "session_id": "session-1",
            "turn_id": "turn-plan-no-menu",
            "event_id": "plan-stop-no-menu",
            "payload": {
                "permission_mode": "plan",
                "last_assistant_message": None,
            },
        })
    )
    tasks = list(bridge._plan_probe_tasks.values())
    await asyncio.gather(*tasks)

    assert qq.images == []
    assert not any("[计划完成]" in text for text, _ in qq.sent)


@pytest.mark.parametrize("permission_mode", ["default", "plan"])
async def test_plan_footer_with_empty_message_sends_notice_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    permission_mode: str,
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "completed plan\nstatus  Plan mode"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    transcript = tmp_path / f"session-{permission_mode}.jsonl"
    turn_id = f"turn-{permission_mode}"
    write_plan_transcript(transcript, turn_id, f"plan for {permission_mode}")
    await register(bridge, transcript_path=str(transcript))
    qq.sent.clear()
    qq.events.clear()

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    event = with_hook_identity({
        "event": "stop",
        "session_id": "session-1",
        "turn_id": turn_id,
        "event_id": f"stop-{permission_mode}",
        "payload": {
            "permission_mode": permission_mode,
            "last_assistant_message": None,
        },
    }, transcript_path=str(transcript))
    await bridge.hook_event(event)
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))
    await bridge.hook_event(event)

    assert [text for text, _ in qq.sent] == [
        f"[codex #1] [计划内容]\nplan for {permission_mode}"
    ]
    assert len(qq.images) == 1


async def test_proposed_plan_message_sends_inner_plan_once_then_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "plan rendered\n计划模式"
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()
    qq.events.clear()

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(with_hook_identity({
        "event": "stop",
        "session_id": "session-1",
        "turn_id": "turn-proposed-plan",
        "event_id": "stop-proposed-plan",
        "payload": {
            "permission_mode": "default",
            "last_assistant_message": "<proposed_plan>safe marker</proposed_plan>",
        },
    }))
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))

    assert [text for text, _ in qq.sent] == ["[codex #1] [计划内容]\nsafe marker"]
    assert len(qq.images) == 1
    assert [event for event, _ in qq.events] == ["text", "image"]


async def test_missing_plan_text_sends_warning_then_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen = "Implement this plan?\n1. Yes\n3. No, stay in Plan mode"
    transcript = tmp_path / "missing-plan.jsonl"
    transcript.write_text("{broken\n", encoding="utf-8")
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge, transcript_path=str(transcript))
    qq.sent.clear()
    qq.events.clear()

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(with_hook_identity({
        "event": "stop",
        "session_id": "session-1",
        "turn_id": "turn-missing-plan",
        "event_id": "stop-missing-plan",
        "payload": {"permission_mode": "default", "last_assistant_message": None},
    }, transcript_path=str(transcript)))
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))

    assert [text for text, _ in qq.sent] == [
        "[codex #1] Plan 已完成，但未能提取正文；下面发送当前屏幕。"
    ]
    assert len(qq.images) == 1
    assert [event for event, _ in qq.events] == ["text", "image"]


def test_extract_plan_from_transcript_uses_matching_turn(tmp_path: Path) -> None:
    transcript = tmp_path / "plans.jsonl"
    write_plan_transcript(transcript, "wanted-turn", "wanted plan")

    assert (
        KittyBridge._extract_plan_from_transcript(transcript, "wanted-turn")
        == "wanted plan"
    )
    assert KittyBridge._extract_plan_from_transcript(transcript, "missing-turn") is None


async def test_plan_probe_screen_failure_has_no_false_plan_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    kitty.screen_error = KittyError("window gone")
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)

    async def no_delay(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await bridge.hook_event(with_hook_identity({
        "event": "stop",
        "session_id": "session-1",
        "turn_id": "turn-screen-error",
        "event_id": "stop-screen-error",
        "payload": {"permission_mode": "default", "last_assistant_message": None},
    }))
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))

    assert not any("[计划完成]" in text for text, _ in qq.sent)
    assert qq.images == []


async def test_kitty_failure_marks_only_target_binding_offline(tmp_path: Path) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge, "session-1")
    await register(bridge, "session-2", window_id="8")
    bridge.active_session_id = "session-1"
    kitty.send_error = KittyError("window gone")

    await bridge._inject_text("hello", reply_to="m1")

    assert bridge.bindings["session-1"].state == "offline"
    assert bridge.bindings["session-2"].state == "idle"
    assert "已标记该会话离线" in qq.sent[-1][0]


def test_install_hooks_removes_old_qq_hook_but_keeps_desktop_notification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "hooks.json"
    target.write_text(
        '{"hooks":{"UserPromptSubmit":['
        '{"hooks":[{"type":"command","command":"python ~/.codex/hooks/qq_connect.py"}]},'
        '{"hooks":[{"type":"command","command":"python ~/.codex/hooks/codex_notify.py"}]}'
        '],"PermissionRequest":[{"hooks":[{"type":"command",'
        '"command":"codex-qq hook-permission-request"}]}]}}',
        encoding="utf-8",
    )
    install_hooks(target, tmp_path / "codex-qq")
    installed = target.read_text(encoding="utf-8")
    assert "qq_connect.py" not in installed
    assert "codex_notify.py" in installed
    assert "hook-permission-request" not in installed
    assert "hook-permission-notify" not in installed
    assert "PermissionRequest" not in installed


def test_hook_definition_inspection_reports_required_handlers(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    install_hooks(target, tmp_path / "codex-qq")
    install_hooks(target, tmp_path / "codex-qq")
    assert inspect_hook_definitions(target) == {"installed": True, "missing": []}
    installed = target.read_text(encoding="utf-8")
    assert "hook-permission-notify" not in installed
    assert installed.count("hook-event") == 8
    value = json.loads(installed)
    assert value["hooks"]["SessionStart"] == [
        {
            "matcher": "^(clear|resume)$",
            "hooks": [
                {
                    "type": "command",
                    "command": f"{tmp_path / 'codex-qq'} hook-event",
                    "timeout": 10,
                }
            ],
        }
    ]


async def test_gateway_disconnect_keeps_binding_and_reconnect_revalidates(
    tmp_path: Path,
) -> None:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    await bridge._on_qq_disconnected()
    assert "session-1" in bridge.bindings
    await bridge._on_qq_connected()
    assert bridge.bindings["session-1"].state == "idle"
    # Revalidation is the point of this test; the recovery notice is not, since a
    # resume this fast is the Gateway's routine hourly rotation and stays silent.
    assert (
        "validate",
        "unix:/run/user/1000/codex-qq-kitty-1",
        7,
        "codex",
    ) in kitty.calls


async def test_session_start_migrates_same_window(tmp_path: Path) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge, "session-old")
    await bridge.hook_event(
        {
            "event": "session_start",
            "session_id": "session-new",
            "payload": {
                "source": "clear",
                "transcript_path": "/session-new.jsonl",
            },
            "event_id": "migration",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "7",
        }
    )
    assert list(bridge.bindings) == ["session-new"]
    assert bridge.active_session_id == "session-new"
    assert bridge.bindings["session-new"].transcript_path == "/session-new.jsonl"


async def test_resume_session_start_migrates_same_window(tmp_path: Path) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge, "session-old")

    result = await bridge.hook_event(
        {
            "event": "session_start",
            "session_id": "session-resumed",
            "payload": {
                "source": "resume",
                "transcript_path": "/session-resumed.jsonl",
            },
            "event_id": "resume-migration",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "7",
        }
    )

    assert result == {"ok": True, "accepted": True}
    assert list(bridge.bindings) == ["session-resumed"]
    assert bridge.active_session_id == "session-resumed"
    assert bridge.bindings["session-resumed"].transcript_path == (
        "/session-resumed.jsonl"
    )


@pytest.mark.parametrize(
    "event,payload",
    [
        ("user_prompt_submit", {"prompt": "private internal prompt"}),
        ("stop", {"last_assistant_message": "private response"}),
        ("pre_tool_use", {"tool_input": {"questions": [{"question": "private?"}]}}),
        ("interrupt", {}),
        ("pre_compact", {}),
        ("post_compact", {}),
    ],
)
async def test_hook_identity_mismatch_is_fail_closed_without_qq_output(
    tmp_path: Path,
    event: str,
    payload: dict[str, object],
) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()

    result = await bridge.hook_event(
        with_hook_identity(
            {
                "event": event,
                "session_id": "session-1",
                "payload": payload,
                "event_id": f"bad-{event}",
            },
            transcript_path="/different-session.jsonl",
        )
    )

    assert result == {"ok": True, "ignored": "hook_identity_mismatch"}
    assert qq.sent == []
    assert "session-1" in bridge.bindings


async def test_missing_hook_identity_is_fail_closed(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()

    result = await bridge.hook_event(
        {
            "event": "user_prompt_submit",
            "session_id": "session-1",
            "payload": {"prompt": "private internal prompt"},
            "event_id": "missing-identity",
        }
    )

    assert result == {"ok": True, "ignored": "hook_identity_mismatch"}
    assert qq.sent == []


async def test_identity_mismatch_does_not_consume_event_id(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()
    event = {
        "event": "user_prompt_submit",
        "session_id": "session-1",
        "payload": {"prompt": "visible prompt"},
        "event_id": "retry-after-mismatch",
    }

    rejected = await bridge.hook_event(
        with_hook_identity(event, transcript_path="/different-session.jsonl")
    )
    accepted = await bridge.hook_event(with_hook_identity(event))

    assert rejected == {"ok": True, "ignored": "hook_identity_mismatch"}
    assert accepted == {"ok": True, "accepted": True}
    assert qq.sent == [("[codex #1] 电脑：visible prompt", None)]


async def test_session_end_identity_mismatch_keeps_binding(tmp_path: Path) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    result = await bridge.hook_event(
        with_hook_identity(
            {
                "event": "session_end",
                "session_id": "session-1",
                "payload": {},
                "event_id": "wrong-session-end",
            },
            window_id="8",
        )
    )

    assert result == {"ok": True, "ignored": "hook_identity_mismatch"}
    assert "session-1" in bridge.bindings


async def test_new_keeps_binding_until_replacement_session_start(tmp_path: Path) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge, "session-old")

    await bridge._inject_text("/new")
    end_result = await bridge.hook_event(
        with_hook_identity(
            {
                "event": "session_end",
                "session_id": "session-old",
                "payload": {},
                "event_id": "new-session-end",
            }
        )
    )

    assert end_result == {"ok": True, "accepted": True}
    assert list(bridge.bindings) == ["session-old"]

    start_result = await bridge.hook_event(
        {
            "event": "session_start",
            "session_id": "session-new",
            "payload": {
                "source": "clear",
                "transcript_path": "/session-new.jsonl",
            },
            "event_id": "new-session-start",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "7",
        }
    )

    assert start_result == {"ok": True, "accepted": True}
    assert list(bridge.bindings) == ["session-new"]
    assert bridge.active_session_id == "session-new"
    assert bridge.bindings["session-new"].session_transition_expires_at == 0.0


async def test_session_end_without_new_still_removes_binding(tmp_path: Path) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    result = await bridge.hook_event(
        with_hook_identity(
            {
                "event": "session_end",
                "session_id": "session-1",
                "payload": {},
                "event_id": "ordinary-session-end",
            }
        )
    )

    assert result == {"ok": True, "accepted": True}
    assert bridge.bindings == {}


async def test_compact_session_start_does_not_migrate_same_window(tmp_path: Path) -> None:
    bridge = KittyBridge(make_config(tmp_path), qq=FakeQQ(), kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge, "session-old")

    result = await bridge.hook_event(
        {
            "event": "session_start",
            "session_id": "session-compact",
            "payload": {"source": "compact"},
            "event_id": "compact-session",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "7",
        }
    )

    assert result == {"ok": True, "ignored": "session_not_bound"}
    assert list(bridge.bindings) == ["session-old"]
    assert bridge.active_session_id == "session-old"
    assert not any("会话已切换" in text for text, _ in bridge.qq.sent)


async def test_adapter_sends_key_sequence_as_separate_commands() -> None:
    adapter = KittyAdapter()
    calls: list[tuple[str, ...]] = []

    async def fake_run(socket: str, *args: str, stdin: str | None = None) -> str:
        del socket, stdin
        calls.append(args)
        return ""

    adapter._run = fake_run  # type: ignore[method-assign]
    await adapter.send_keys("unix:/run/user/1000/kitty", 7, ["down", "enter"])
    assert calls == [
        ("send-key", "--match", "id:7", "down"),
        ("send-key", "--match", "id:7", "enter"),
    ]


async def test_adapter_pastes_text_waits_then_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = KittyAdapter()
    events: list[tuple[str, object]] = []

    async def fake_run(socket: str, *args: str, stdin: str | None = None) -> str:
        del socket
        events.append(("run", (args, stdin)))
        return ""

    async def fake_sleep(delay: float) -> None:
        events.append(("sleep", delay))

    adapter._run = fake_run  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await adapter.send_text("unix:/run/user/1000/kitty", 7, "qq-connect")

    assert events == [
        (
            "run",
            (
                (
                    "send-text",
                    "--match",
                    "id:7",
                    "--stdin",
                    "--bracketed-paste=enable",
                ),
                "qq-connect",
            ),
        ),
        ("sleep", 0.15),
        ("run", (("send-key", "--match", "id:7", "enter"), None)),
    ]


async def test_adapter_pastes_images_then_prompt_and_submits(tmp_path: Path) -> None:
    adapter = KittyAdapter()
    calls: list[tuple[tuple[str, ...], str | None]] = []
    copied: list[Path] = []

    async def fake_run(socket: str, *args: str, stdin: str | None = None) -> str:
        del socket
        calls.append((args, stdin))
        return ""

    adapter._run = fake_run  # type: ignore[method-assign]

    async def fake_copy(path: Path) -> None:
        copied.append(path)

    adapter.wl_copy_command = "/usr/bin/wl-copy"
    adapter.clipboard_image_paste_available = lambda: True  # type: ignore[method-assign]
    adapter._copy_image_once = fake_copy  # type: ignore[method-assign]
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    await adapter.send_images(
        "unix:/run/user/1000/kitty", 7, [first, second], "分析图片"
    )

    assert copied == [first, second]
    pasted = [stdin for args, stdin in calls if "send-text" in args]
    assert pasted == ["分析图片"]
    assert [args[-1] for args, _ in calls if "send-key" in args] == [
        "ctrl+v",
        "ctrl+v",
        "enter",
    ]
    assert calls[-1] == (("send-key", "--match", "id:7", "enter"), None)


async def test_adapter_image_failure_never_submits_prompt(tmp_path: Path) -> None:
    adapter = KittyAdapter()
    calls: list[tuple[tuple[str, ...], str | None]] = []
    copied = 0

    async def fake_run(socket: str, *args: str, stdin: str | None = None) -> str:
        del socket
        calls.append((args, stdin))
        return ""

    async def fake_copy(_path: Path) -> None:
        nonlocal copied
        copied += 1
        if copied == 2:
            raise KittyImagePasteError("clipboard failure")

    adapter._run = fake_run  # type: ignore[method-assign]
    adapter.wl_copy_command = "/usr/bin/wl-copy"
    adapter.clipboard_image_paste_available = lambda: True  # type: ignore[method-assign]
    adapter._copy_image_once = fake_copy  # type: ignore[method-assign]

    with pytest.raises(KittyImagePasteError) as error:
        await adapter.send_images(
            "unix:/run/user/1000/kitty",
            7,
            [tmp_path / "first.png", tmp_path / "second.png"],
            "不要提交",
        )

    assert error.value.attached_count == 1
    assert not any(stdin == "不要提交" for _args, stdin in calls)
    assert all(args[-1] != "enter" for args, _stdin in calls if "send-key" in args)


async def test_adapter_launches_titled_shell_in_os_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    adapter = KittyAdapter()
    calls: list[tuple[str, ...]] = []

    async def fake_run(socket: str, *args: str, stdin: str | None = None) -> str:
        del socket, stdin
        calls.append(args)
        return "42\n"

    adapter._run = fake_run  # type: ignore[method-assign]
    window_id = await adapter.launch_shell(
        f"unix:{tmp_path}/kitty",
        tmp_path,
        "codex",
    )

    assert window_id == 42
    # No program argument: kitty starts the login shell, and the agent command
    # is typed in afterwards so shell functions and aliases can resolve.
    assert calls == [
        (
            "launch",
            "--type=os-window",
            f"--cwd={tmp_path}",
            "--title=codex",
        )
    ]


async def test_hourly_gateway_rotation_is_not_announced(tmp_path: Path) -> None:
    """The Gateway rotates sessions hourly (op 7 / 4009); resumes must stay quiet."""
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()

    await bridge._on_qq_disconnected()
    await bridge._on_qq_connected()

    assert qq.sent == []


async def test_long_outage_is_announced_with_its_duration(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()

    await bridge._on_qq_disconnected()
    # Backdate the disconnect well past the routine-rotation grace period.
    bridge._disconnected_at -= 600.0
    await bridge._on_qq_connected()

    assert len(qq.sent) == 1
    assert "已恢复" in qq.sent[0][0]
    assert "600 秒" in qq.sent[0][0]


async def test_short_reconnect_still_reports_an_offline_binding(tmp_path: Path) -> None:
    """A quick resume is quiet, but a binding that died meanwhile is not."""
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()
    kitty.invalid = True

    await bridge._on_qq_disconnected()
    await bridge._on_qq_connected()

    assert len(qq.sent) == 1
    assert "1 个 Kitty binding 当前离线" in qq.sent[0][0]


async def test_first_connect_after_restart_is_announced(tmp_path: Path) -> None:
    """No prior disconnect means the bridge itself restarted; that is worth saying."""
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)
    qq.sent.clear()

    await bridge._on_qq_connected()

    assert len(qq.sent) == 1
    assert "已恢复" in qq.sent[0][0]


async def test_repeated_disconnects_keep_the_earliest_timestamp(tmp_path: Path) -> None:
    qq = FakeQQ()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=FakeKitty())  # type: ignore[arg-type]
    await register(bridge)

    await bridge._on_qq_disconnected()
    first = bridge._disconnected_at
    await bridge._on_qq_disconnected()

    assert bridge._disconnected_at == first


async def test_binding_survives_failing_qq_notice(tmp_path: Path) -> None:
    """A failing connect notice must not undo the binding it announces.

    Regression: register_kitty awaited the notice inline and rolled the binding
    back on error, so a slow or failing QQ API left the session unbound.
    """
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)
    qq.error = RuntimeError("QQ API timeout")

    result = await bridge.register_kitty(
        {
            "session_id": "session-1",
            "cwd": "/project",
            "transcript_path": "/session.jsonl",
            "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
            "kitty_window_id": "7",
        }
    )

    assert result["ok"] is True
    assert result["state"] == "connected"
    await asyncio.gather(*list(bridge._notify_tasks), return_exceptions=True)
    assert "session-1" in bridge.bindings
    assert bridge.active_session_id == "session-1"
    assert qq.sent == []


async def test_register_does_not_wait_for_qq_notice(tmp_path: Path) -> None:
    """Registration returns without awaiting the outbound notice."""
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)
    released = asyncio.Event()
    original_send = qq.send

    async def blocking_send(text: str, *, reply_to: str | None = None) -> None:
        await released.wait()
        await original_send(text, reply_to=reply_to)

    qq.send = blocking_send  # type: ignore[method-assign]

    result = await asyncio.wait_for(
        bridge.register_kitty(
            {
                "session_id": "session-1",
                "cwd": "/project",
                "transcript_path": "/session.jsonl",
                "kitty_socket": "unix:/run/user/1000/codex-qq-kitty-1",
                "kitty_window_id": "7",
            }
        ),
        timeout=1,
    )

    assert result["ok"] is True
    assert qq.sent == []  # still in flight

    released.set()
    await asyncio.gather(*list(bridge._notify_tasks), return_exceptions=True)
    assert len(qq.sent) == 1
    assert "会话已连接并设为当前" in qq.sent[0][0]
