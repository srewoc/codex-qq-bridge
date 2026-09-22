from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .agents import DEFAULT_KIND, profile_for

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ALLOWED_KEYS = frozenset(
    {
        "up",
        "down",
        "left",
        "right",
        "enter",
        "esc",
        "tab",
        "shift+tab",
        "home",
        "end",
        "page_up",
        "page_down",
        "ctrl+c",
    }
)


class KittyError(RuntimeError):
    pass


class KittyImagePasteError(KittyError):
    def __init__(self, message: str, *, attached_count: int = 0):
        super().__init__(message)
        self.attached_count = attached_count


def sanitize_screen(value: str, limit: int) -> str:
    value = CONTROL_RE.sub("", ANSI_RE.sub("", value)).rstrip()
    if len(value) <= limit:
        return value
    return value[-limit:] + "\n[screen 已截断]"


class KittyAdapter:
    def __init__(self, command: str = "kitten", *, timeout: float = 5.0):
        self.command = command
        self.timeout = timeout
        self.wl_copy_command = shutil.which("wl-copy")

    def clipboard_image_paste_available(self) -> bool:
        return bool(
            self.wl_copy_command
            and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            and os.environ.get("WAYLAND_DISPLAY")
        )

    async def _run(
        self, socket: str, *args: str, stdin: str | None = None
    ) -> str:
        process = await asyncio.create_subprocess_exec(
            self.command,
            "@",
            "--to",
            socket,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise KittyError("Kitty Remote Control 超时") from exc
        if process.returncode:
            message = stderr.decode(errors="replace").strip()
            raise KittyError(message or f"Kitty 命令失败 ({process.returncode})")
        return stdout.decode(errors="replace")

    async def windows(self, socket: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(await self._run(socket, "ls"))
        except json.JSONDecodeError as exc:
            raise KittyError("Kitty ls 返回了无效 JSON") from exc
        return value if isinstance(value, list) else []

    async def managed_sockets(self) -> list[str]:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        result: list[str] = []
        for path in sorted(runtime.glob("codex-qq-kitty-*")):
            socket = f"unix:{path}"
            try:
                self._validate_socket(socket)
                await self.windows(socket)
            except (KittyError, OSError):
                continue
            result.append(socket)
        return result

    async def validate(
        self, socket: str, window_id: int, kind: str = DEFAULT_KIND
    ) -> None:
        self._validate_socket(socket)
        profile = profile_for(kind)
        for os_window in await self.windows(socket):
            for tab in os_window.get("tabs", []):
                for window in tab.get("windows", []):
                    if int(window.get("id", -1)) != window_id:
                        continue
                    foreground = window.get("foreground_processes") or []
                    command_lines = " ".join(
                        " ".join(map(str, process.get("cmdline") or []))
                        for process in foreground
                        if isinstance(process, dict)
                    ).lower()
                    title = str(window.get("title", "")).lower()
                    haystack = f"{command_lines} {title}"
                    if not any(
                        marker in haystack for marker in profile.window_markers
                    ):
                        raise KittyError(
                            f"目标 Kitty 窗口未识别到 {profile.kind}"
                        )
                    return
        raise KittyError(f"Kitty window {window_id} 不存在")

    @staticmethod
    def _validate_socket(socket: str) -> None:
        if not socket.startswith("unix:"):
            raise KittyError("Kitty Socket 必须使用 unix: 地址")
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        raw_path = Path(socket.removeprefix("unix:")).resolve()
        try:
            raw_path.relative_to(runtime.resolve())
        except ValueError as exc:
            raise KittyError("Kitty Socket 必须位于当前用户运行目录") from exc

    async def launch_shell(self, socket: str, cwd: Path, title: str) -> int:
        """Open a fresh OS window running the user's login shell.

        The agent command is typed in afterwards with `send-text`, because
        shell functions and aliases only exist inside an interactive shell
        that has sourced the user's rc files.
        """
        self._validate_socket(socket)
        value = await self._run(
            socket,
            "launch",
            "--type=os-window",
            f"--cwd={cwd}",
            f"--title={title}",
        )
        try:
            window_id = int(value.strip())
        except ValueError as exc:
            raise KittyError(f"Kitty launch 未返回有效窗口 ID：{value.strip()!r}") from exc
        if window_id <= 0:
            raise KittyError("Kitty launch 返回了无效窗口 ID")
        return window_id

    async def send_text(self, socket: str, window_id: int, text: str) -> None:
        match = f"id:{window_id}"
        await self._run(
            socket,
            "send-text",
            "--match",
            match,
            "--stdin",
            "--bracketed-paste=enable",
            stdin=text,
        )
        # Kitty handles paste and key injection as separate remote-control
        # commands. Give the TUI one frame to consume the bracketed paste
        # before submitting it, otherwise Enter can arrive first.
        await asyncio.sleep(0.15)
        await self.send_keys(socket, window_id, ["enter"])

    async def send_images(
        self,
        socket: str,
        window_id: int,
        paths: list[Path],
        prompt: str,
    ) -> None:
        if not self.clipboard_image_paste_available():
            raise KittyImagePasteError("当前环境缺少 Wayland/wl-copy 图片粘贴能力")
        match = f"id:{window_id}"
        attached = 0
        for index, path in enumerate(paths, start=1):
            try:
                await self._copy_image_once(path)
                await self._send_key(socket, window_id, "ctrl+v")
                attached += 1
                await asyncio.sleep(0.15)
            except (KittyError, OSError) as exc:
                raise KittyImagePasteError(
                    f"第 {index} 张图片未能粘贴到 Codex",
                    attached_count=attached,
                ) from exc
        await self._run(
            socket,
            "send-text",
            "--match",
            match,
            "--stdin",
            "--bracketed-paste=enable",
            stdin=prompt,
        )
        await asyncio.sleep(0.15)
        await self.send_keys(socket, window_id, ["enter"])

    async def _copy_image_once(self, path: Path) -> None:
        if not self.wl_copy_command:
            raise KittyImagePasteError("找不到 wl-copy")
        with path.open("rb") as image_stream:
            process = await asyncio.create_subprocess_exec(
                self.wl_copy_command,
                "--paste-once",
                "--type",
                "image/png",
                stdin=image_stream,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise KittyImagePasteError("wl-copy 设置图片剪贴板超时") from exc
        if process.returncode:
            message = stderr.decode(errors="replace").strip()
            raise KittyImagePasteError(message or "wl-copy 设置图片剪贴板失败")

    async def send_keys(
        self,
        socket: str,
        window_id: int,
        keys: list[str],
        blocked: frozenset[str] = frozenset(),
    ) -> None:
        if not keys or len(keys) > 16 or any(key not in ALLOWED_KEYS for key in keys):
            raise KittyError("按键序列不在允许列表中，或长度超过 16")
        if any(key in blocked for key in keys):
            raise KittyError("该按键在当前 agent 上已禁用")
        # Kitty treats multiple positional keys as one chord. Execute separately so
        # `/qq-key down enter` remains an ordered key sequence.
        for key in keys:
            await self._send_key(socket, window_id, key)

    async def _send_key(self, socket: str, window_id: int, key: str) -> None:
        await self._run(socket, "send-key", "--match", f"id:{window_id}", key)

    async def get_text(self, socket: str, window_id: int, limit: int) -> str:
        value = await self._run(
            socket,
            "get-text",
            "--match",
            f"id:{window_id}",
            "--extent=screen",
        )
        return sanitize_screen(value, limit)
