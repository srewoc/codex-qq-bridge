from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .agents import (
    DEFAULT_KIND,
    UNKNOWN_READY_DELAY_SECONDS,
    hint_for,
    normalize_kind,
    profile_for,
)
from .config import Config
from .inbound_image import InboundImageBatch, prune_image_cache
from .kitty import ALLOWED_KEYS, KittyAdapter, KittyError, KittyImagePasteError
from .qq import OfficialQQTransport
from .screen_image import render_screen_images

log = logging.getLogger(__name__)
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
TURN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# `/qq-new` accepts a bare command name only: no spaces, arguments, quotes,
# pipes or redirections ever reach the launched shell.
COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Claude Code folds a long bracketed paste into a pasted-content block and
# expands it again on submit, so the prompt the hook reports no longer matches
# the raw text this bridge typed into the TUI. The closing tag repeats the id.
PASTED_CONTENT_RE = re.compile(
    r'\A<pasted_content id="(?P<id>[^"]*)">\s*'
    r"(?P<body>.*?)"
    r'\s*</pasted_content(?: id="(?P=id)")?>\Z',
    re.DOTALL,
)
# PreToolUse fires before the tool runs, so the plan approval menu has not
# been drawn yet when the hook arrives.
PLAN_MENU_RENDER_DELAY_SECONDS = 0.8
# A freshly launched shell needs a moment before readline is up; typing earlier
# makes the terminal echo the bracketed paste markers literally.
SHELL_PROMPT_TIMEOUT_SECONDS = 5.0
# Codex keeps printing startup notices after its banner appears and can swallow
# the Enter that follows the paste, so qq-connect is nudged until it lands.
QQ_CONNECT_ATTEMPTS = 4
QQ_CONNECT_ATTEMPT_TIMEOUT_SECONDS = 7.0
# A cold Codex start was measured at 18s, so the banner wait has to be generous.
AGENT_TUI_TIMEOUT_SECONDS = 60.0
# The banner lands while the TUI is still streaming startup notices, and input
# sent during that window is dropped outright.
SCREEN_SETTLE_QUIET_SECONDS = 0.6
SCREEN_SETTLE_TIMEOUT_SECONDS = 6.0
PENDING_LAUNCH_TTL_SECONDS = 150.0
# `/new` ends the old agent session before the replacement SessionStart hook is
# emitted.  Keep the window binding briefly so that the latter can migrate it.
SESSION_TRANSITION_TTL_SECONDS = 30.0
# The QQ Gateway rotates every WebSocket session roughly hourly, closing it with
# op 7 / code 4009 "Session timed out". The SDK resumes within a few seconds and
# no inbound message is lost, so announcing those recoveries only spams the
# owner ~24 times a day. Only a recovery slower than this grace period, or one
# that left a Kitty binding offline, is worth a message.
ROUTINE_RECONNECT_GRACE_SECONDS = 60.0
HOOK_EVENTS = {
    "user_prompt_submit",
    "stop",
    "pre_tool_use",
    "interrupt",
    "session_start",
    "session_end",
    "pre_compact",
    "post_compact",
    "notification",
}

HELP_TEXT = """可用命令：
/qq-help 查看本帮助
/qq-activate 检查机器人状态
/qq-list 查看已连接的会话
/qq-use <序号> 选择要操作的会话
/qq-new <命令> [路径] 新建会话
  命令必填，只能是单个名字，如 codex、claude
  路径可选，默认沿用当前会话目录，支持 ~
/qq-screen 以图片查看当前会话屏幕
/qq-screen-text 以文字查看当前会话屏幕
/qq-key <按键...> 发送受控按键
  可用：up down left right enter esc tab shift+tab
       home end page_up page_down ctrl+c
  claude 会话不接受 ctrl+c，请改用 esc
  示例：/qq-key down enter
/qq-stop 停止当前会话的任务（codex 发 ctrl+c，claude 发 esc）
/qq-disconnect 断开当前选择的会话
/qq-disconnect-all 断开全部会话
直接发送图片或图文消息，可将图片提交给当前会话。
其他文字和 agent 原生 slash 命令将输入当前 TUI。"""


class QQTransport(Protocol):
    ready: bool

    async def run(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, text: str, *, reply_to: str | None = None) -> None: ...

    async def send_image(self, path: Path, *, reply_to: str | None = None) -> None: ...

    def health_status(self) -> dict[str, Any]: ...


@dataclass
class PendingInjection:
    text: str
    expires_at: float


@dataclass
class PendingLaunch:
    cwd: str
    command: str
    kitty_socket: str
    expires_at: float
    future: asyncio.Future[KittyBinding]
    kitty_window_id: int | None = None


@dataclass
class KittyBinding:
    session_id: str
    generation: str
    cwd: str
    kitty_socket: str
    kitty_window_id: int
    transcript_path: str | None
    connected_at: float
    last_seen_at: float
    kind: str = DEFAULT_KIND
    state: str = "idle"
    active_turn_id: str | None = None
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    pending_injections: deque[PendingInjection] = field(default_factory=deque, repr=False)
    session_transition_expires_at: float = field(default=0.0, repr=False)


class KittyBridge:
    def __init__(
        self,
        config: Config,
        qq: QQTransport | None = None,
        kitty: KittyAdapter | None = None,
    ):
        self.config = config
        self.bindings: dict[str, KittyBinding] = {}
        self.active_session_id: str | None = None
        self._plan_probe_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._notify_tasks: set[asyncio.Task[None]] = set()
        self._pending_launches: dict[str, PendingLaunch] = {}
        self._seen_messages: dict[str, float] = {}
        self._seen_events: dict[str, float] = {}
        self._control_server: asyncio.AbstractServer | None = None
        self._stopping = asyncio.Event()
        self._disconnected_at: float | None = None
        self._bindings_file = config.state_dir / "kitty-bindings.json"
        self.kitty = kitty or KittyAdapter(config.kitty_command)
        self.qq: QQTransport = qq or OfficialQQTransport(
            config,
            self._on_qq_message,
            self._on_qq_connected,
            self._on_qq_disconnected,
            self._on_qq_fatal,
        )

    async def run(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        prune_image_cache(self.config.state_dir / "incoming-images")
        await self._restore_bindings()
        self.config.control_socket.unlink(missing_ok=True)
        self._control_server = await asyncio.start_unix_server(
            self._handle_control, path=self.config.control_socket
        )
        self.config.control_socket.chmod(0o600)
        qq_task = asyncio.create_task(self.qq.run())
        try:
            async with self._control_server:
                await self._stopping.wait()
        finally:
            await self.qq.stop()
            qq_task.cancel()
            await asyncio.gather(qq_task, return_exceptions=True)
            self.config.control_socket.unlink(missing_ok=True)
            self._cancel_plan_probes()
            self._cancel_notifies()
            for pending in self._pending_launches.values():
                if not pending.future.done():
                    pending.future.cancel()
            self._pending_launches.clear()

    async def _on_qq_connected(self) -> None:
        downtime = (
            None
            if self._disconnected_at is None
            else time.monotonic() - self._disconnected_at
        )
        self._disconnected_at = None
        offline = 0
        for binding in list(self.bindings.values()):
            try:
                await self.kitty.validate(
                    binding.kitty_socket, binding.kitty_window_id, binding.kind
                )
            except Exception:  # noqa: BLE001 - reconnect checks all registrations
                binding.state = "offline"
                offline += 1
            else:
                if binding.state == "offline":
                    binding.state = "idle"
        if not self.bindings:
            return
        if offline == 0 and self._is_routine_reconnect(downtime):
            log.info(
                "QQ Gateway resumed after %.1fs; suppressing routine notice",
                downtime or 0.0,
            )
            self._save_bindings()
            return
        await self._send_qq(self._recovery_notice(downtime, offline))
        self._save_bindings()

    @staticmethod
    def _is_routine_reconnect(downtime: float | None) -> bool:
        """True for the Gateway's hourly session rotation, which needs no notice."""
        return downtime is not None and downtime < ROUTINE_RECONNECT_GRACE_SECONDS

    @staticmethod
    def _recovery_notice(downtime: float | None, offline: int) -> str:
        span = "" if downtime is None else f"（断线 {round(downtime)} 秒）"
        message = f"QQ Gateway 已恢复{span}；断线期间的消息未执行。"
        if offline:
            message += f"\n{offline} 个 Kitty binding 当前离线。"
        return message

    async def _on_qq_disconnected(self) -> None:
        if self._disconnected_at is None:
            self._disconnected_at = time.monotonic()

    async def _on_qq_fatal(self, reason: str) -> None:
        log.error("Official QQ Bot fatal error: %s", reason)
        await self._on_qq_disconnected()

    async def _on_qq_message(
        self,
        user_openid: str,
        text: str,
        message_id: str,
        images: InboundImageBatch | list[Path] | None = None,
    ) -> None:
        batch = (
            images
            if isinstance(images, InboundImageBatch)
            else InboundImageBatch.from_paths(images or [])
        )
        if user_openid != self.config.owner_openid or (not text and not batch.has_images):
            return
        if message_id and self._dedupe(self._seen_messages, message_id, 600):
            return
        if batch.has_images:
            if not batch.paths:
                detail = batch.rejection_summary() or "没有可用图片"
                await self._send_qq(f"图片未发送：{detail}", reply_to=message_id)
                return
            await self._inject_images(
                batch,
                text or "请查看并分析这张图片。",
                reply_to=message_id,
            )
            return
        parts = text.split()
        command = parts[0]
        if command == "/qq-help":
            await self._send_qq(HELP_TEXT, reply_to=message_id)
        elif command == "/qq-activate":
            await self._send_qq(
                f"官方 QQ 机器人在线。\n{self._format_binding_list()}",
                reply_to=message_id,
            )
        elif command == "/qq-list":
            await self._send_qq(self._format_binding_list(), reply_to=message_id)
        elif command == "/qq-new":
            await self._new_session(text, message_id)
        elif command == "/qq-use":
            await self._select(parts[1] if len(parts) == 2 else None, message_id)
        elif command == "/qq-disconnect-all":
            count = len(self.bindings)
            self.bindings.clear()
            self.active_session_id = None
            self._save_bindings()
            self._cancel_plan_probes()
            await self._send_qq(f"已断开全部 {count} 个会话", reply_to=message_id)
        elif command.startswith("/qq-") and command not in {
            "/qq-disconnect",
            "/qq-screen",
            "/qq-screen-text",
            "/qq-key",
            "/qq-stop",
        }:
            await self._send_qq(
                "未知 QQ 命令；发送 /qq-help 查看可用命令", reply_to=message_id
            )
        elif not self.bindings:
            return
        elif command == "/qq-disconnect":
            await self._remove_binding(self.active_session_id, "用户主动断开", message_id)
        elif command == "/qq-screen":
            await self._send_screen_image(reply_to=message_id)
        elif command == "/qq-screen-text":
            await self._send_screen_text(reply_to=message_id)
        elif command == "/qq-key":
            await self._key_command(parts[1:], message_id)
        elif command == "/qq-stop":
            await self._stop_command(message_id)
        else:
            await self._inject_text(text, reply_to=message_id)

    @staticmethod
    def _dedupe(cache: dict[str, float], key: str, ttl: float) -> bool:
        now = time.monotonic()
        for old_key, seen_at in list(cache.items()):
            if seen_at < now - ttl:
                cache.pop(old_key, None)
        if key in cache:
            return True
        cache[key] = now
        return False

    async def register_kitty(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = self._session_id(request.get("session_id"))
        socket = str(request.get("kitty_socket", ""))
        try:
            window_id = int(request.get("kitty_window_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("kitty_window_id 必须是正整数") from exc
        if window_id <= 0:
            raise ValueError("kitty_window_id 必须是正整数")
        # The registering side declares which agent it is; never guessed here.
        kind = normalize_kind(request.get("agent"))
        await self.kitty.validate(socket, window_id, kind)
        if not self.qq.ready:
            return {"ok": False, "error": "QQ Gateway 当前未连接，未创建 binding"}
        self._prune_pending_launches()
        pending_matches = [
            (key, pending)
            for key, pending in self._pending_launches.items()
            if pending.kitty_socket == socket and pending.kitty_window_id == window_id
        ]
        pending_key: str | None = None
        pending: PendingLaunch | None = None
        if len(pending_matches) == 1:
            pending_key, pending = pending_matches[0]
        conflict = next(
            (
                binding
                for binding in self.bindings.values()
                if (binding.kitty_socket, binding.kitty_window_id) == (socket, window_id)
                and binding.session_id != session_id
            ),
            None,
        )
        if conflict:
            raise ValueError("该 Kitty window 已绑定另一个 session")
        if session_id in self.bindings:
            existing = self.bindings[session_id]
            if (existing.kitty_socket, existing.kitty_window_id) != (socket, window_id):
                raise ValueError("该 session 已绑定另一个 Kitty window")
            self.active_session_id = session_id
            existing.last_seen_at = time.time()
            self._save_bindings()
            return {"ok": True, "state": "connected", "session_id": session_id}
        now = time.time()
        binding = KittyBinding(
            session_id=session_id,
            generation=uuid.uuid4().hex,
            cwd=pending.cwd if pending is not None else str(request.get("cwd") or ""),
            kitty_socket=socket,
            kitty_window_id=window_id,
            transcript_path=str(request.get("transcript_path") or "") or None,
            connected_at=now,
            last_seen_at=now,
            kind=kind,
        )
        self.bindings[session_id] = binding
        index = self._binding_index(session_id)
        activate = bool(request.get("activate")) or pending is not None
        notify = request.get("notify") is not False and pending is None
        if self.active_session_id is None or activate:
            self.active_session_id = session_id
            heading = "会话已连接并设为当前"
        else:
            heading = "会话已加入连接列表"
        self._save_bindings()
        if pending_key and pending:
            self._pending_launches.pop(pending_key, None)
            if not pending.future.done():
                pending.future.set_result(binding)
        if notify:
            # Binding must not depend on an outbound QQ call: the API client allows
            # 30s while the registering hook only budgets 10s, so awaiting the
            # notice here made a slow QQ API cancel the hook and roll the binding
            # back. Deliver it in the background instead.
            self._spawn_notify(
                f"{heading}\n编号：{index}\n类型：{kind}\n"
                f"项目：{binding.cwd or '未知'}\n"
                f"会话：{session_id}\nKitty 窗口：{window_id}"
            )
        return {
            "ok": True,
            "state": "connected",
            "session_id": session_id,
            "index": index,
            "agent": kind,
        }

    async def hook_event(self, request: dict[str, Any]) -> dict[str, Any]:
        event = str(request.get("event", "")).strip().lower()
        if event not in HOOK_EVENTS:
            raise ValueError(f"不支持的 Hook event：{event}")
        session_id = self._session_id(request.get("session_id"))
        payload = request.get("payload") or {}
        if not isinstance(payload, dict):
            raise TypeError("payload 必须是对象")
        binding = self.bindings.get(session_id)
        if event == "session_start":
            binding = await self._session_start(session_id, request, payload)
        if not binding:
            return {"ok": True, "ignored": "session_not_bound"}
        mismatches = self._hook_identity_mismatches(binding, request, payload)
        if mismatches:
            log.warning(
                "Ignoring %s hook for session %s: identity mismatch in %s",
                event,
                session_id,
                ",".join(mismatches),
            )
            return {"ok": True, "ignored": "hook_identity_mismatch"}
        event_id = str(request.get("event_id") or self._event_id(event, session_id, payload))
        if self._dedupe(self._seen_events, event_id, 3600):
            return {"ok": True, "duplicate": True}
        binding.last_seen_at = time.time()
        turn_id = str(request.get("turn_id") or payload.get("turn_id") or "")
        if turn_id and not TURN_RE.fullmatch(turn_id):
            raise ValueError("turn_id 格式无效")
        prefix = self._message_prefix(session_id)
        if event == "user_prompt_submit":
            self._cancel_plan_probes(session_id)
            raw_prompt = str(payload.get("prompt") or payload.get("user_prompt") or "").strip()
            prompt = self._unwrap_pasted_content(raw_prompt)
            self._prune_injections(binding)
            if binding.pending_injections and self._is_own_injection(
                binding.pending_injections[0].text, prompt
            ):
                binding.pending_injections.popleft()
            elif prompt and prompt not in {"qq-connect", "/qq-connect"}:
                await self._send_qq(f"{prefix}电脑：{prompt}")
        elif event == "stop":
            binding.state = "idle"
            binding.active_turn_id = None
            message = str(payload.get("last_assistant_message") or "").strip()
            direct_plan = self._extract_proposed_plan(message)
            permission_mode = str(payload.get("permission_mode") or "")
            log.info(
                "Hook stop received session=%s turn=%s permission_mode=%s "
                "message_present=%s",
                session_id,
                turn_id or "-",
                permission_mode or "-",
                bool(message),
            )
            if message and direct_plan is None:
                await self._send_qq(f"{prefix}{message}")
            self._schedule_plan_prompt_probe(binding, turn_id, payload)
        elif event == "pre_tool_use":
            binding.state = "interactive"
            if str(payload.get("tool_name") or "") == "ExitPlanMode":
                await self._send_plan(binding, payload, prefix)
            else:
                await self._send_question(binding, payload, prefix)
        elif event == "notification":
            message = str(payload.get("message") or "").strip()
            if message:
                await self._send_qq(f"{prefix}等待你处理：{message}")
        elif event == "interrupt":
            binding.state = "idle"
            binding.active_turn_id = None
            await self._send_qq(f"{prefix}任务已停止")
        elif event == "session_end":
            if binding.session_transition_expires_at > time.monotonic():
                log.info(
                    "Keeping binding for pending session transition session=%s",
                    session_id,
                )
                binding.state = "idle"
                binding.active_turn_id = None
            else:
                await self._remove_binding(session_id, "会话已结束")
        elif event == "pre_compact":
            await self._send_qq(f"{prefix}正在压缩上下文")
        elif event == "post_compact":
            await self._send_qq(f"{prefix}上下文压缩完成")
        self._save_bindings()
        return {"ok": True, "accepted": True}

    async def _inject_text(self, text: str, *, reply_to: str | None = None) -> None:
        binding = self._active_binding()
        if not binding or binding.state == "offline":
            await self._send_qq("当前会话离线", reply_to=reply_to)
            return
        async with binding.input_lock:
            binding.pending_injections.append(
                PendingInjection(text, time.monotonic() + 30)
            )
            if text.strip() == "/new":
                binding.session_transition_expires_at = (
                    time.monotonic() + SESSION_TRANSITION_TTL_SECONDS
                )
            try:
                before = await self.kitty.get_text(
                    binding.kitty_socket,
                    binding.kitty_window_id,
                    self.config.screen_max_chars,
                ) if text.startswith("/") else None
                await self.kitty.send_text(
                    binding.kitty_socket, binding.kitty_window_id, text
                )
                binding.state = "running"
                binding.last_seen_at = time.time()
                if before is not None:
                    try:
                        await self._screen_after_change(
                            binding, before, reply_to=reply_to
                        )
                    except (KittyError, OSError) as exc:
                        await self._mark_binding_offline(
                            binding, exc, reply_to=reply_to, action_completed=True
                        )
                        return
            except (KittyError, OSError) as exc:
                if binding.pending_injections:
                    binding.pending_injections.pop()
                await self._mark_binding_offline(binding, exc, reply_to=reply_to)
                return
        self._save_bindings()

    async def _inject_images(
        self,
        batch: InboundImageBatch,
        prompt: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        binding = self._active_binding()
        if not binding or binding.state == "offline":
            await self._send_qq("当前会话离线，图片未发送", reply_to=reply_to)
            return
        async with binding.input_lock:
            try:
                if profile_for(binding.kind).image_channel == "path":
                    # Claude Code opens image files itself, so the clipboard
                    # round trip is neither needed nor reliable there.
                    await self.kitty.send_text(
                        binding.kitty_socket,
                        binding.kitty_window_id,
                        " ".join([prompt, *(str(path) for path in batch.paths)]),
                    )
                else:
                    await self.kitty.send_images(
                        binding.kitty_socket,
                        binding.kitty_window_id,
                        batch.paths,
                        prompt,
                    )
                binding.state = "running"
                binding.last_seen_at = time.time()
            except KittyImagePasteError as exc:
                detail = str(exc)
                if exc.attached_count:
                    detail += (
                        f"；已有 {exc.attached_count} 张留在输入框但未提交，"
                        "可在电脑端清理或发送 /qq-key ctrl+c"
                    )
                await self._send_qq(f"图片未提交：{detail}", reply_to=reply_to)
                return
            except (KittyError, OSError) as exc:
                await self._mark_binding_offline(binding, exc, reply_to=reply_to)
                return
        self._save_bindings()
        rejected = batch.rejection_summary()
        suffix = f"；另有图片未处理：{rejected}" if rejected else ""
        await self._send_qq(
            f"已向 {binding.kind} 提交 {len(batch.paths)} 张图片和文字说明{suffix}",
            reply_to=reply_to,
        )

    async def _screen_after_change(
        self,
        binding: KittyBinding,
        before: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        current = before
        for _ in range(10):
            await asyncio.sleep(0.15)
            current = await self.kitty.get_text(
                binding.kitty_socket,
                binding.kitty_window_id,
                self.config.screen_max_chars,
            )
            if current != before:
                break
        await self._send_screen_image(
            reply_to=reply_to,
            binding=binding,
            screen=current,
        )

    async def _new_session(self, text: str, reply_to: str) -> None:
        # maxsplit=2 keeps the command a single bare token while leaving a
        # trailing path that contains spaces intact.
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await self._send_qq(
                "用法：/qq-new <命令> [路径]\n"
                "命令必填，例如 /qq-new claude 或 /qq-new codex ~/code/foo",
                reply_to=reply_to,
            )
            return
        command = parts[1]
        if not COMMAND_RE.fullmatch(command):
            await self._send_qq(
                "命令只能是单个名字（字母、数字和 . _ -），"
                "不能含空格、参数、引号或其他符号",
                reply_to=reply_to,
            )
            return
        source = self._active_binding()
        raw_path = parts[2].strip() if len(parts) == 3 else ""
        if not raw_path:
            if source is None or not source.cwd:
                await self._send_qq(
                    "当前没有可继承工作目录的会话；请使用 /qq-new <命令> <路径>",
                    reply_to=reply_to,
                )
                return
            requested = Path(source.cwd)
        else:
            requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            await self._send_qq(
                "/qq-new 的路径只接受绝对路径或 ~/... 路径", reply_to=reply_to
            )
            return
        try:
            cwd = requested.resolve(strict=True)
        except OSError as exc:
            await self._send_qq(f"目录不可用：{exc}", reply_to=reply_to)
            return
        if not cwd.is_dir():
            await self._send_qq("指定路径不是目录", reply_to=reply_to)
            return
        if source is not None:
            kitty_socket = source.kitty_socket
        else:
            sockets = await self.kitty.managed_sockets()
            kitty_socket = sockets[0] if len(sockets) == 1 else ""
        if not kitty_socket:
            await self._send_qq(
                "无法唯一确定受控 Kitty；请确保只运行一个已配置的 Kitty，"
                "或先手动绑定一个会话",
                reply_to=reply_to,
            )
            return
        self._prune_pending_launches()
        if len(self._pending_launches) >= 3:
            await self._send_qq("正在启动的会话过多，请稍后重试", reply_to=reply_to)
            return

        launch_id = uuid.uuid4().hex
        future: asyncio.Future[KittyBinding] = asyncio.get_running_loop().create_future()
        pending = PendingLaunch(
            cwd=str(cwd),
            command=command,
            kitty_socket=kitty_socket,
            expires_at=time.monotonic() + PENDING_LAUNCH_TTL_SECONDS,
            future=future,
        )
        self._pending_launches[launch_id] = pending
        try:
            window_id = await self.kitty.launch_shell(kitty_socket, cwd, command)
            pending.kitty_window_id = window_id
            await self._wait_for_shell_prompt(kitty_socket, window_id)
            await self.kitty.send_text(kitty_socket, window_id, command)
            await self._wait_for_agent_tui(kitty_socket, window_id, command)
            binding = await self._submit_qq_connect(kitty_socket, window_id, future)
            if binding.kitty_window_id != window_id:
                await self._remove_binding(
                    binding.session_id,
                    "自动绑定窗口与 Kitty launch 返回值不一致",
                    notify=False,
                )
                raise RuntimeError("自动绑定的 Kitty 窗口校验失败")
        except TimeoutError:
            await self._send_qq(
                f"Kitty 已启动并执行 {command}，但 qq-connect 多次重试后仍未完成绑定；"
                "请在新窗口手动执行 qq-connect",
                reply_to=reply_to,
            )
        except Exception as exc:  # noqa: BLE001 - report launch failure to owner
            await self._send_qq(f"新建会话失败：{exc}", reply_to=reply_to)
        else:
            await self._send_qq(
                f"会话已新建并设为当前\n编号：{self._binding_index(binding.session_id)}\n"
                f"类型：{binding.kind}\n项目：{binding.cwd}\n"
                f"会话：{binding.session_id}\n"
                f"Kitty 窗口：{binding.kitty_window_id}",
                reply_to=reply_to,
            )
        finally:
            self._pending_launches.pop(launch_id, None)
            if not future.done():
                future.cancel()

    async def _read_screen_quietly(self, socket: str, window_id: int) -> str:
        try:
            return await self.kitty.get_text(
                socket, window_id, self.config.screen_max_chars
            )
        except (KittyError, OSError):
            return ""

    async def _wait_for_shell_prompt(self, socket: str, window_id: int) -> None:
        deadline = time.monotonic() + SHELL_PROMPT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if (await self._read_screen_quietly(socket, window_id)).strip():
                return
            await asyncio.sleep(0.05)

    async def _submit_qq_connect(
        self,
        socket: str,
        window_id: int,
        future: asyncio.Future[KittyBinding],
    ) -> KittyBinding:
        """Type `qq-connect` and keep nudging until the binding lands.

        A swallowed Enter leaves the text sitting in the composer, so a bare
        Enter recovers that case; re-pasting covers a paste that never arrived.
        """
        for attempt in range(QQ_CONNECT_ATTEMPTS):
            screen = await self._read_screen_quietly(socket, window_id)
            if attempt and "qq-connect" in screen:
                await self.kitty.send_keys(socket, window_id, ["enter"])
            else:
                await self.kitty.send_text(socket, window_id, "qq-connect")
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=QQ_CONNECT_ATTEMPT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                continue
        raise TimeoutError("qq-connect 未能在多次重试内完成绑定")

    async def _wait_for_agent_tui(
        self, socket: str, window_id: int, command: str
    ) -> None:
        profile = hint_for(command)
        if profile is None:
            # No known banner for this command: wait a fixed moment and let the
            # qq-connect registration itself decide whether it worked.
            await asyncio.sleep(UNKNOWN_READY_DELAY_SECONDS)
            return
        deadline = time.monotonic() + AGENT_TUI_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            screen = await self._read_screen_quietly(socket, window_id)
            if any(marker in screen for marker in profile.ready_markers):
                await self._wait_for_screen_settled(socket, window_id)
                return
            await asyncio.sleep(0.2)
        raise KittyError(
            f"{command} 的 TUI 在 {AGENT_TUI_TIMEOUT_SECONDS:.0f} 秒内未就绪"
        )

    async def _wait_for_screen_settled(self, socket: str, window_id: int) -> None:
        deadline = time.monotonic() + SCREEN_SETTLE_TIMEOUT_SECONDS
        previous = await self._read_screen_quietly(socket, window_id)
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            current = await self._read_screen_quietly(socket, window_id)
            if current != previous:
                previous = current
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= SCREEN_SETTLE_QUIET_SECONDS:
                return

    def _prune_pending_launches(self) -> None:
        now = time.monotonic()
        for token, pending in list(self._pending_launches.items()):
            if pending.expires_at >= now:
                continue
            self._pending_launches.pop(token, None)
            if not pending.future.done():
                pending.future.cancel()

    async def _key_command(
        self, keys: list[str], reply_to: str | None, *, stop: bool = False
    ) -> None:
        binding = self._active_binding()
        if not binding:
            return
        if not keys or len(keys) > 16 or any(key not in ALLOWED_KEYS for key in keys):
            await self._send_qq(
                "非法按键；发送 /qq-help 查看允许的按键", reply_to=reply_to
            )
            return
        profile = profile_for(binding.kind)
        rejected = sorted({key for key in keys if key in profile.blocked_keys})
        if rejected:
            await self._send_qq(
                f"{binding.kind} 会话不支持 {' '.join(rejected)}；"
                f"请使用 /qq-stop 或 /qq-key {profile.stop_key}",
                reply_to=reply_to,
            )
            return
        self._cancel_plan_probes(binding.session_id)
        async with binding.input_lock:
            try:
                before = await self.kitty.get_text(
                    binding.kitty_socket,
                    binding.kitty_window_id,
                    self.config.screen_max_chars,
                )
                await self.kitty.send_keys(
                    binding.kitty_socket,
                    binding.kitty_window_id,
                    keys,
                    profile.blocked_keys,
                )
            except (KittyError, OSError) as exc:
                await self._mark_binding_offline(binding, exc, reply_to=reply_to)
                return
            await self._send_qq(
                "已请求停止当前任务" if stop else "按键已发送",
                reply_to=reply_to,
            )
            try:
                await self._screen_after_change(binding, before, reply_to=reply_to)
            except (KittyError, OSError) as exc:
                await self._mark_binding_offline(
                    binding, exc, reply_to=reply_to, action_completed=True
                )

    async def _stop_command(self, reply_to: str | None) -> None:
        binding = self._active_binding()
        if not binding:
            return
        await self._key_command(
            [profile_for(binding.kind).stop_key], reply_to, stop=True
        )

    async def _read_screen(
        self, binding: KittyBinding, *, reply_to: str | None = None
    ) -> str | None:
        try:
            return await self.kitty.get_text(
                binding.kitty_socket,
                binding.kitty_window_id,
                self.config.screen_max_chars,
            )
        except (KittyError, OSError) as exc:
            await self._mark_binding_offline(binding, exc, reply_to=reply_to)
            return None

    async def _send_screen_text(self, *, reply_to: str | None = None) -> None:
        binding = self._active_binding()
        if not binding:
            return
        screen = await self._read_screen(binding, reply_to=reply_to)
        if screen is None:
            return
        await self._send_qq(
            f"{self._message_prefix(binding.session_id)}[screen]\n{screen}",
            reply_to=reply_to,
        )

    async def _send_screen_image(
        self,
        *,
        reply_to: str | None = None,
        binding: KittyBinding | None = None,
        screen: str | None = None,
    ) -> None:
        binding = binding or self._active_binding()
        if not binding:
            return
        if screen is None:
            screen = await self._read_screen(binding, reply_to=reply_to)
        if screen is None:
            return
        paths: list[Path] = []
        try:
            paths = await asyncio.to_thread(
                render_screen_images,
                screen,
                label=f"{binding.kind} #{self._binding_index(binding.session_id)}",
                project_name=Path(binding.cwd).name or binding.cwd,
                output_dir=self.config.state_dir / "screens",
                kind=binding.kind,
            )
            for index, path in enumerate(paths):
                await self.qq.send_image(path, reply_to=reply_to if index == 0 else None)
        except Exception as exc:  # noqa: BLE001 - screen image has a text fallback
            log.warning("Failed to render or send screen image: %s", exc)
            await self._send_qq(
                "图片发送失败，以下为文字屏幕：\n"
                f"{self._message_prefix(binding.session_id)}[screen]\n{screen}",
                reply_to=reply_to,
            )
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    async def _send_question(
        self, binding: KittyBinding, payload: dict[str, Any], prefix: str
    ) -> None:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        legacy_input = payload.get("input") or {}
        if not isinstance(legacy_input, dict):
            legacy_input = {}
        questions = (
            tool_input.get("questions")
            or payload.get("questions")
            or legacy_input.get("questions")
            or []
        )
        if not isinstance(questions, list):
            questions = []
        lines = [f"{prefix}[结构化问题]"]
        question_items = [question for question in questions if isinstance(question, dict)]
        for question_number, question in enumerate(question_items, start=1):
            if len(question_items) > 1:
                lines.extend(["", f"问题 {question_number}/{len(question_items)}"])
            header = str(question.get("header") or "").strip()
            prompt = str(question.get("question") or "").strip()
            if header:
                lines.append(header)
            if prompt and prompt != header:
                lines.append(prompt)
            elif not header:
                lines.append("请选择")
            lines.append("")
            for index, option in enumerate(question.get("options") or [], start=1):
                label = option.get("label") if isinstance(option, dict) else option
                lines.append(f"{index}. {label}")
                if isinstance(option, dict):
                    description = str(option.get("description") or "").strip()
                    if description:
                        lines.append(f"   {description}")
        lines.extend(
            [
                "",
                (
                    "请使用 /qq-screen 查看当前界面，并用 /qq-key 操作 TUI；"
                    "自动数字选择需通过本机版本验收后启用。"
                ),
            ]
        )
        await self._send_qq("\n".join(lines))

    async def _send_plan(
        self, binding: KittyBinding, payload: dict[str, Any], prefix: str
    ) -> None:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        plan = str(tool_input.get("plan") or "").strip()
        header = f"{prefix}[计划待确认]"
        if plan:
            # OfficialQQTransport.send already splits on message_chunk_size.
            await self._send_qq(f"{header}\n{plan}")
        else:
            path = str(tool_input.get("planFilePath") or "").strip()
            detail = f"计划文件：{path}" if path else "未取到计划正文"
            await self._send_qq(f"{header}\n{detail}")
        # Capture the screen on a short delay and off the hook's critical path,
        # so the shot actually contains the approval menu.
        key = (
            binding.session_id,
            f"exit-plan-mode:{payload.get('tool_use_id') or ''}",
        )
        if key in self._plan_probe_tasks:
            return
        task = asyncio.create_task(
            self._send_plan_menu_screen(binding.session_id, binding.generation)
        )
        self._plan_probe_tasks[key] = task
        task.add_done_callback(
            lambda completed, current=key: self._finish_plan_probe(current, completed)
        )

    async def _send_plan_menu_screen(self, session_id: str, generation: str) -> None:
        await asyncio.sleep(PLAN_MENU_RENDER_DELAY_SECONDS)
        binding = self.bindings.get(session_id)
        if not binding or binding.generation != generation:
            return
        await self._send_screen_image(binding=binding)
        await self._send_qq(
            f"{self._message_prefix(session_id)}"
            "请使用 /qq-key up、down 和 enter 操作确认菜单。"
        )

    async def _session_start(
        self, session_id: str, request: dict[str, Any], payload: dict[str, Any]
    ) -> KittyBinding | None:
        existing = self.bindings.get(session_id)
        if existing is not None:
            return existing
        # Only user-visible `/clear`, resume and fork transitions can replace a
        # binding. Internal startup/compaction sessions may inherit Kitty env.
        if str(payload.get("source") or "") not in {"clear", "resume", "fork"}:
            return None
        transcript_path = str(
            payload.get("transcript_path") or request.get("transcript_path") or ""
        )
        if not transcript_path:
            return None
        socket = str(request.get("kitty_socket") or payload.get("kitty_socket") or "")
        window_raw = request.get("kitty_window_id") or payload.get("kitty_window_id")
        try:
            window_id = int(window_raw)
        except (TypeError, ValueError):
            return None
        old = next(
            (
                value
                for value in self.bindings.values()
                if (value.kitty_socket, value.kitty_window_id) == (socket, window_id)
            ),
            None,
        )
        if not old:
            return None
        if old.session_id == session_id:
            return old
        await self.kitty.validate(socket, window_id)
        old_id = old.session_id
        items = list(self.bindings.items())
        old.session_id = session_id
        old.generation = uuid.uuid4().hex
        old.transcript_path = transcript_path
        old.active_turn_id = None
        old.state = "idle"
        old.session_transition_expires_at = 0.0
        self.bindings = {
            (session_id if key == old_id else key): (old if key == old_id else value)
            for key, value in items
        }
        if self.active_session_id == old_id:
            self.active_session_id = session_id
        self._save_bindings()
        await self._send_qq(
            f"{self._message_prefix(session_id)}会话已切换\n旧会话：{old_id}\n新会话：{session_id}"
        )
        return old

    def _schedule_plan_prompt_probe(
        self,
        binding: KittyBinding,
        turn_id: str,
        payload: dict[str, Any],
    ) -> None:
        message = str(payload.get("last_assistant_message") or "").strip()
        permission_mode = str(payload.get("permission_mode") or "")
        probe_id = (
            turn_id
            or hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
        )
        key = (binding.session_id, probe_id)
        if key in self._plan_probe_tasks:
            log.info(
                "Plan probe ignored session=%s turn=%s reason=already_scheduled",
                binding.session_id,
                turn_id or "-",
            )
            return
        task = asyncio.create_task(
            self._probe_plan_prompt(
                binding.session_id,
                binding.generation,
                turn_id=turn_id,
                permission_mode=permission_mode,
                assistant_plan_candidate=not message or "<proposed_plan>" in message,
                message_present=bool(message),
                direct_plan=self._extract_proposed_plan(message),
                deferred_message=message if "<proposed_plan>" in message else "",
            )
        )
        self._plan_probe_tasks[key] = task
        task.add_done_callback(
            lambda completed, current=key: self._finish_plan_probe(current, completed)
        )

    def _finish_plan_probe(
        self, key: tuple[str, str], task: asyncio.Task[None]
    ) -> None:
        self._plan_probe_tasks.pop(key, None)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("Plan prompt probe failed: %s", exc)

    async def _probe_plan_prompt(
        self,
        session_id: str,
        generation: str,
        *,
        turn_id: str,
        permission_mode: str,
        assistant_plan_candidate: bool,
        message_present: bool,
        direct_plan: str | None,
        deferred_message: str,
    ) -> None:
        for delay in (0.2, 0.5, 1.0):
            await asyncio.sleep(delay)
            binding = self.bindings.get(session_id)
            if not binding or binding.generation != generation:
                return
            try:
                screen = await self.kitty.get_text(
                    binding.kitty_socket,
                    binding.kitty_window_id,
                    self.config.screen_max_chars,
                )
            except (KittyError, OSError) as exc:
                log.info(
                    "Plan probe finished session=%s turn=%s permission_mode=%s "
                    "message_present=%s screen_result=error reason=screen_read_failed",
                    session_id,
                    turn_id or "-",
                    permission_mode or "-",
                    message_present,
                )
                if deferred_message:
                    await self._send_qq(
                        f"{self._message_prefix(session_id)}{deferred_message}"
                    )
                await self._mark_binding_offline(binding, exc)
                return
            prompt = self._extract_plan_prompt(screen)
            footer_detected = self._has_plan_mode_footer(screen)
            if assistant_plan_candidate and (prompt or footer_detected):
                binding.state = "interactive"
                self._save_bindings()
                reason = "confirmation_menu" if prompt else "plan_footer"
                log.info(
                    "Plan probe matched session=%s turn=%s permission_mode=%s "
                    "message_present=%s screen_result=matched reason=%s",
                    session_id,
                    turn_id or "-",
                    permission_mode or "-",
                    message_present,
                    reason,
                )
                plan_text = direct_plan
                if plan_text is None and binding.transcript_path and turn_id:
                    plan_text = await asyncio.to_thread(
                        self._extract_plan_from_transcript,
                        Path(binding.transcript_path),
                        turn_id,
                    )
                if plan_text:
                    await self._send_qq(
                        f"{self._message_prefix(session_id)}[计划内容]\n{plan_text}"
                    )
                else:
                    log.warning(
                        "Plan text unavailable session=%s turn=%s",
                        session_id,
                        turn_id or "-",
                    )
                    await self._send_qq(
                        f"{self._message_prefix(session_id)}"
                        "Plan 已完成，但未能提取正文；下面发送当前屏幕。"
                    )
                await self._send_screen_image(binding=binding, screen=screen)
                return
        log.info(
            "Plan probe finished session=%s turn=%s permission_mode=%s "
            "message_present=%s screen_result=not_matched reason=%s",
            session_id,
            turn_id or "-",
            permission_mode or "-",
            message_present,
            "assistant_message_not_plan" if not assistant_plan_candidate else "no_plan_ui",
        )
        if deferred_message:
            await self._send_qq(
                f"{self._message_prefix(session_id)}{deferred_message}"
            )

    @staticmethod
    def _extract_proposed_plan(message: str) -> str | None:
        match = re.search(
            r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
            message,
            flags=re.DOTALL,
        )
        if not match:
            return None
        plan = match.group(1).strip()
        return plan or None

    @staticmethod
    def _extract_plan_from_transcript(path: Path, turn_id: str) -> str | None:
        try:
            stream = path.open("rb")
        except OSError as exc:
            log.warning("Plan transcript unavailable: %s", exc)
            return None
        with stream:
            stream.seek(0, 2)
            position = stream.tell()
            remainder = b""
            while position > 0:
                size = min(64 * 1024, position)
                position -= size
                stream.seek(position)
                remainder = stream.read(size) + remainder
                lines = remainder.split(b"\n")
                remainder = lines[0]
                for raw_line in reversed(lines[1:]):
                    plan = KittyBridge._plan_text_from_jsonl_line(raw_line, turn_id)
                    if plan:
                        return plan
            return KittyBridge._plan_text_from_jsonl_line(remainder, turn_id)

    @staticmethod
    def _plan_text_from_jsonl_line(raw_line: bytes, turn_id: str) -> str | None:
        if not raw_line.strip():
            return None
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            return None
        if payload.get("type") != "item_completed" or payload.get("turn_id") != turn_id:
            return None
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "Plan":
            return None
        text = str(item.get("text") or "").strip()
        return text or None

    @staticmethod
    def _extract_plan_prompt(screen: str) -> str | None:
        lines = screen.splitlines()
        start = next(
            (index for index, line in enumerate(lines) if "Implement this plan?" in line),
            None,
        )
        if start is None:
            return None
        return "\n".join(lines[start : start + 8]).strip()

    @staticmethod
    def _has_plan_mode_footer(screen: str) -> bool:
        lines = [line.strip() for line in screen.splitlines() if line.strip()]
        return any(
            line.endswith(("Plan mode", "计划模式")) for line in lines[-2:]
        )

    def _cancel_notifies(self) -> None:
        for task in list(self._notify_tasks):
            task.cancel()
        self._notify_tasks.clear()

    def _cancel_plan_probes(self, session_id: str | None = None) -> None:
        for key, task in list(self._plan_probe_tasks.items()):
            if session_id is None or key[0] == session_id:
                task.cancel()
                self._plan_probe_tasks.pop(key, None)

    async def _mark_binding_offline(
        self,
        binding: KittyBinding,
        exc: Exception,
        *,
        reply_to: str | None = None,
        action_completed: bool = False,
    ) -> None:
        binding.state = "offline"
        self._save_bindings()
        detail = str(exc) if isinstance(exc, KittyError) else f"{type(exc).__name__}: {exc}"
        status = "命令已送达，但屏幕读取失败" if action_completed else "Kitty 窗口操作失败"
        await self._send_qq(
            f"{self._message_prefix(binding.session_id)}{status}；已标记该会话离线：{detail}",
            reply_to=reply_to,
        )

    async def _select(self, selector: str | None, reply_to: str) -> None:
        if not selector or not selector.isdigit():
            await self._send_qq(
                f"用法：/qq-use <序号>\n{self._format_binding_list()}",
                reply_to=reply_to,
            )
            return
        index = int(selector)
        values = list(self.bindings.values())
        if not 1 <= index <= len(values):
            await self._send_qq(f"找不到会话 #{index}", reply_to=reply_to)
            return
        selected = values[index - 1]
        self.active_session_id = selected.session_id
        self._save_bindings()
        await self._send_qq(
            f"已切换到 {selected.kind} #{index}", reply_to=reply_to
        )

    async def _remove_binding(
        self,
        session_id: str | None,
        reason: str,
        reply_to: str | None = None,
        *,
        notify: bool = True,
    ) -> None:
        if not session_id:
            return
        self._cancel_plan_probes(session_id)
        index = self._binding_index(session_id)
        removed = self.bindings.pop(session_id, None)
        if not removed:
            return
        if self.active_session_id == session_id:
            self.active_session_id = next(iter(self.bindings), None)
        self._save_bindings()
        if notify:
            await self._send_qq(
                f"{removed.kind} #{index} 已断开：{reason}", reply_to=reply_to
            )

    def _format_binding_list(self) -> str:
        if not self.bindings:
            return "当前没有已连接的会话。请在目标会话执行 qq-connect。"
        lines = ["已连接的会话："]
        width = max(
            (len(value.kind) for value in self.bindings.values()),
            default=0,
        )
        for index, binding in enumerate(self.bindings.values(), start=1):
            marker = "*" if binding.session_id == self.active_session_id else " "
            label = f"[{binding.kind}]".ljust(width + 2)
            lines.append(
                f"{marker} {index}. {label} {binding.cwd or binding.session_id} "
                f"[{binding.state}] (window {binding.kitty_window_id})"
            )
        lines.append("* 表示手机当前选择；使用 /qq-use <序号> 切换。")
        return "\n".join(lines)

    def _active_binding(self) -> KittyBinding | None:
        return self.bindings.get(self.active_session_id or "")

    def _binding_index(self, session_id: str) -> int:
        return next(
            (i for i, value in enumerate(self.bindings, start=1) if value == session_id),
            0,
        )

    def _message_prefix(self, session_id: str) -> str:
        binding = self.bindings.get(session_id)
        kind = binding.kind if binding else DEFAULT_KIND
        return f"[{kind} #{self._binding_index(session_id)}] "

    async def _send_qq(self, text: str, *, reply_to: str | None = None) -> None:
        await self.qq.send(text, reply_to=reply_to)

    def _spawn_notify(self, text: str, *, reply_to: str | None = None) -> None:
        """Deliver a QQ notice off the caller's critical path."""
        task = asyncio.create_task(self._send_qq(text, reply_to=reply_to))
        self._notify_tasks.add(task)
        task.add_done_callback(self._finish_notify)

    def _finish_notify(self, task: asyncio.Task[None]) -> None:
        self._notify_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("QQ notice delivery failed: %s", exc)

    @staticmethod
    def _session_id(value: Any) -> str:
        result = str(value or "")
        if not SESSION_RE.fullmatch(result):
            raise ValueError("session_id 格式无效")
        return result

    @staticmethod
    def _event_id(event: str, session_id: str, payload: dict[str, Any]) -> str:
        raw = json.dumps([event, session_id, payload], sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _hook_identity_mismatches(
        binding: KittyBinding,
        request: dict[str, Any],
        payload: dict[str, Any],
    ) -> list[str]:
        mismatches: list[str] = []
        transcript_path = str(
            payload.get("transcript_path") or request.get("transcript_path") or ""
        )
        if not binding.transcript_path or transcript_path != binding.transcript_path:
            mismatches.append("transcript_path")
        socket = str(request.get("kitty_socket") or payload.get("kitty_socket") or "")
        if socket != binding.kitty_socket:
            mismatches.append("kitty_socket")
        window_raw = request.get("kitty_window_id") or payload.get("kitty_window_id")
        try:
            window_id = int(window_raw)
        except (TypeError, ValueError):
            window_id = 0
        if window_id != binding.kitty_window_id:
            mismatches.append("kitty_window_id")
        return mismatches

    @classmethod
    def _is_own_injection(cls, injected: str, prompt: str) -> bool:
        """Recognise text this bridge typed into the TUI.

        Claude Code may expand a submitted prompt (file references, command
        substitution), so requiring an exact match would echo the owner's own
        phone message straight back at them. A long paste additionally arrives
        wrapped in a pasted-content block, which has to come off first.
        """
        left = injected.strip()
        right = cls._unwrap_pasted_content(prompt)
        return bool(left) and (left == right or right.startswith(left))

    @staticmethod
    def _unwrap_pasted_content(prompt: str) -> str:
        """Strip the pasted-content wrapper Claude Code adds to long pastes."""
        match = PASTED_CONTENT_RE.match(prompt.strip())
        return (match.group("body") if match else prompt).strip()

    @staticmethod
    def _prune_injections(binding: KittyBinding) -> None:
        now = time.monotonic()
        while binding.pending_injections and binding.pending_injections[0].expires_at < now:
            binding.pending_injections.popleft()

    def _save_bindings(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "active_session_id": self.active_session_id,
            "bindings": [
                {
                    "session_id": value.session_id,
                    "generation": value.generation,
                    "cwd": value.cwd,
                    "kitty_socket": value.kitty_socket,
                    "kitty_window_id": value.kitty_window_id,
                    "transcript_path": value.transcript_path,
                    "connected_at": value.connected_at,
                    "last_seen_at": value.last_seen_at,
                    "kind": value.kind,
                    "state": value.state,
                }
                for value in self.bindings.values()
            ],
        }
        temporary = self._bindings_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self._bindings_file)

    async def _restore_bindings(self) -> None:
        try:
            payload = json.loads(self._bindings_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        for raw in payload.get("bindings", []):
            try:
                # Bindings persisted before multi-agent support carry no kind.
                kind = normalize_kind(raw.get("kind"))
                await self.kitty.validate(
                    raw["kitty_socket"], int(raw["kitty_window_id"]), kind
                )
                binding = KittyBinding(
                    session_id=self._session_id(raw["session_id"]),
                    generation=str(raw["generation"]),
                    cwd=str(raw.get("cwd") or ""),
                    kitty_socket=str(raw["kitty_socket"]),
                    kitty_window_id=int(raw["kitty_window_id"]),
                    transcript_path=raw.get("transcript_path"),
                    connected_at=float(raw.get("connected_at") or time.time()),
                    last_seen_at=time.time(),
                    kind=kind,
                )
            except Exception as exc:  # noqa: BLE001 - invalid persisted item is skipped
                log.warning("Skipping invalid Kitty binding: %s", exc)
                continue
            self.bindings[binding.session_id] = binding
        selected = str(payload.get("active_session_id") or "")
        self.active_session_id = selected if selected in self.bindings else next(iter(self.bindings), None)
        self._save_bindings()

    async def _handle_control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readline()
            if len(raw) > self.config.control_max_bytes or not raw.endswith(b"\n"):
                raise ValueError("控制消息过大或未以换行结束")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise TypeError("控制消息必须是 JSON 对象")
            command = request.get("command")
            if command == "register_kitty":
                result = await self.register_kitty(request)
            elif command == "hook_event":
                result = await self.hook_event(request)
            elif command == "status":
                result = {
                    "ok": True,
                    "transport": "kitty",
                    "state": "connected" if self.bindings else "disconnected",
                    "session_id": self.active_session_id,
                    "connection_count": len(self.bindings),
                    "qq": self.qq.ready,
                    "qq_gateway": self.qq.health_status(),
                    "connections": [
                        {
                            "index": index,
                            "session_id": binding.session_id,
                            "cwd": binding.cwd,
                            "kitty_window_id": binding.kitty_window_id,
                            "state": binding.state,
                            "active": binding.session_id == self.active_session_id,
                        }
                        for index, binding in enumerate(self.bindings.values(), start=1)
                    ],
                }
            elif command == "disconnect":
                await self._remove_binding(
                    str(request.get("session_id") or self.active_session_id or ""),
                    "用户主动断开",
                )
                result = {"ok": True, "connection_count": len(self.bindings)}
            else:
                raise ValueError("unknown command")
        except Exception as exc:  # noqa: BLE001 - protocol returns structured errors
            result = {"ok": False, "error": str(exc)}
        try:
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
