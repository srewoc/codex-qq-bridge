from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .agents import DEFAULT_KIND, PROFILES, normalize_kind
from .config import Config
from .kitty import KittyAdapter
from .kitty_bridge import KittyBridge
from .qq_panel import sync_command_panel
from .service import install as install_service
from .service import uninstall as uninstall_service

MARKER = "managed-by-codex-qq-bridge"
SERVICE_NAME = "codex-qq-bridge.service"


async def onboard(config: Config) -> int:
    from qqbot_agent_sdk import start_onboard

    def show_qr(url: str) -> None:
        print("请使用手机 QQ 扫描二维码绑定官方机器人：")
        import qrcode

        code = qrcode.QRCode(border=1)
        code.add_data(url)
        code.make(fit=True)
        code.print_ascii(invert=True)
        print(url)

    result = await start_onboard(on_qr_ready=show_qr)
    config.save_credentials(result.app_id, result.client_secret, result.user_openid)
    print(f"绑定成功，凭据已安全保存到 {config.credential_file}")
    print(f"机器人 AppID：{result.app_id}；主人 OpenID：{result.user_openid}")
    return 0


async def exchange(config: Config, payload: dict[str, object]) -> dict[str, object]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(config.control_socket),
            config.control_connect_timeout_seconds,
        )
    except (OSError, TimeoutError) as exc:
        return {"ok": False, "error": f"桥接服务未运行：{exc}"}
    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(
            reader.readline(), config.control_response_timeout_seconds
        )
        if not raw:
            return {"ok": False, "error": "桥接服务未返回响应"}
        return json.loads(raw.decode())
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"桥接服务响应失败：{exc}"}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def control(config: Config, payload: dict[str, object]) -> int:
    response = await exchange(config, payload)
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


def _launcher_path(launcher: Path | None = None) -> Path:
    if launcher is not None:
        return launcher.expanduser().resolve()
    executable = shutil.which("codex-qq")
    return Path(executable or sys.argv[0]).expanduser().resolve()


def _backup(path: Path, *, dry_run: bool) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    if not dry_run:
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    return backup


def _atomic_text(path: Path, content: str, *, mode: int = 0o600, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] 将写入 {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def install_kitty(target: Path | None = None, *, dry_run: bool = False) -> int:
    config_dir = Path("~/.config/kitty").expanduser()
    target = target or config_dir / "codex-qq.conf"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = f"# {MARKER}"
    if target.exists() and marker not in target.read_text(encoding="utf-8", errors="ignore"):
        raise RuntimeError(f"拒绝覆盖已有的非受管配置：{target}")
    content = (
        f"{marker}\n"
        "allow_remote_control socket-only\n"
        "listen_on unix:${XDG_RUNTIME_DIR}/codex-qq-kitty-{kitty_pid}\n"
    )
    _backup(target, dry_run=dry_run)
    _atomic_text(target, content, dry_run=dry_run)
    main = config_dir / "kitty.conf"
    include = f"include {target.name}"
    main_text = main.read_text(encoding="utf-8") if main.exists() else ""
    if include not in main_text.splitlines():
        _backup(main, dry_run=dry_run)
        updated = main_text.rstrip("\n") + ("\n\n" if main_text else "") + f"{marker}\n{include}\n"
        _atomic_text(main, updated, dry_run=dry_run)
    print(f"{'将安装' if dry_run else '已安装'} Kitty 配置：{target}")
    print("请完整退出所有 Kitty 进程后重新启动；listen_on 不支持热重载。")
    return 0


def install_hooks(
    target: Path | None = None, launcher: Path | None = None, *, dry_run: bool = False
) -> int:
    target = target or Path("~/.codex/hooks.json").expanduser()
    launcher = _launcher_path(launcher)
    command = shlex.quote(str(launcher)) + " hook-event"
    managed = {
        "SessionStart": [
            {
                "matcher": "^(clear|resume)$",
                "hooks": [{"type": "command", "command": command, "timeout": 10}],
            }
        ],
        "SessionEnd": [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "Interrupt": [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}],
        "PreCompact": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "PostCompact": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "PreToolUse": [
            {
                "matcher": "^request_user_input$",
                "hooks": [{"type": "command", "command": command, "timeout": 10}],
            }
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"现有 Hook 配置不是 JSON 对象：{target}")
    else:
        value = {"description": "Codex lifecycle hooks"}
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError(f"现有 Hook 配置 hooks 字段不是对象：{target}")
    for event in (*managed, "PermissionRequest"):
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise TypeError(f"现有 Hook 配置 {event} 字段不是数组")
        existing[:] = [
            group
            for group in existing
            if not _is_obsolete_or_managed_hook(group)
        ]
        existing.extend(managed.get(event, []))
        if existing:
            hooks[event] = existing
        else:
            hooks.pop(event, None)
    backup = _backup(target, dry_run=dry_run)
    _atomic_text(target, json.dumps(value, ensure_ascii=False, indent=2) + "\n", dry_run=dry_run)
    print(f"{'将安装' if dry_run else '已安装'} Codex Hooks：{target}")
    if backup:
        print(f"原配置{'将备份' if dry_run else '已备份'}到 {backup}")
    print("请重启 Codex，在 /hooks 中审核并信任这些 Hook。")
    return 0


def install_claude_hooks(
    target: Path | None = None, launcher: Path | None = None, *, dry_run: bool = False
) -> int:
    target = target or Path("~/.claude/settings.json").expanduser()
    launcher = _launcher_path(launcher)
    command = shlex.quote(str(launcher)) + " hook-event --agent claude"
    managed = {
        "SessionStart": [
            {
                "matcher": "^(clear|resume|fork)$",
                "hooks": [{"type": "command", "command": command, "timeout": 10}],
            }
        ],
        "SessionEnd": [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "PreCompact": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "PostCompact": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        "Notification": [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}],
        "PreToolUse": [
            {
                "matcher": "^(AskUserQuestion|ExitPlanMode)$",
                "hooks": [{"type": "command", "command": command, "timeout": 10}],
            }
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup: Path | None = None
    if target.exists():
        original = target.read_text(encoding="utf-8")
        value = json.loads(original)
        if not isinstance(value, dict):
            raise RuntimeError(f"现有 Claude 配置不是 JSON 对象：{target}")
    else:
        value = {}
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError(f"现有 Claude 配置 hooks 字段不是对象：{target}")
    for event in (*managed, "PermissionRequest"):
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise TypeError(f"现有 Claude 配置 {event} 字段不是数组")
        existing[:] = [
            group
            for group in existing
            if not _is_obsolete_or_managed_hook(group)
        ]
        existing.extend(managed.get(event, []))
        if existing:
            hooks[event] = existing
        else:
            hooks.pop(event, None)
    backup = _backup(target, dry_run=dry_run)
    _atomic_text(target, json.dumps(value, ensure_ascii=False, indent=2) + "\n", dry_run=dry_run)
    print(f"{'将安装' if dry_run else '已安装'} Claude Code Hooks：{target}")
    if backup is not None:
        print(f"原配置已备份到 {backup}")
    print("请重启 Claude Code；出现 Hook 变更提示时需确认信任。")
    return 0


def _is_obsolete_or_managed_hook(group: object) -> bool:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return False
    for handler in group["hooks"]:
        if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
            continue
        try:
            tokens = shlex.split(handler["command"])
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if token == "hook-event" and index > 0:
                launcher = tokens[index - 1]
                if Path(launcher).name == "codex-qq" or "codex-qq" in launcher:
                    return True
            if token in {"hook-permission-request", "hook-permission-notify"}:
                return True
        if any("qq_connect.py" in token for token in tokens):
            return True
    return False


def uninstall_hooks(target: Path, *, label: str, dry_run: bool = False) -> int:
    if not target.exists():
        print(f"未找到 {label} Hook 配置：{target}")
        return 0
    value = json.loads(target.read_text(encoding="utf-8"))
    hooks = value.get("hooks", {}) if isinstance(value, dict) else None
    if not isinstance(hooks, dict):
        raise TypeError(f"现有 {label} Hook 配置格式无效：{target}")
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise TypeError(f"现有 {label} 配置 {event} 字段不是数组")
        kept = [group for group in groups if not _is_obsolete_or_managed_hook(group)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    backup = _backup(target, dry_run=dry_run)
    _atomic_text(target, json.dumps(value, ensure_ascii=False, indent=2) + "\n", dry_run=dry_run)
    print(f"{'将移除' if dry_run else '已移除'} {label} 中由本项目管理的 Hooks")
    if backup:
        print(f"原配置{'将备份' if dry_run else '已备份'}到 {backup}")
    return 0


def uninstall_kitty(target: Path | None = None, *, dry_run: bool = False) -> int:
    config_dir = Path("~/.config/kitty").expanduser()
    target = target or config_dir / "codex-qq.conf"
    marker = f"# {MARKER}"
    if target.exists() and marker not in target.read_text(encoding="utf-8", errors="ignore"):
        raise RuntimeError(f"拒绝删除非受管配置：{target}")
    main = config_dir / "kitty.conf"
    if main.exists():
        lines = main.read_text(encoding="utf-8").splitlines()
        filtered = [line for line in lines if line not in {marker, f"include {target.name}"}]
        if filtered != lines:
            _backup(main, dry_run=dry_run)
            _atomic_text(main, "\n".join(filtered).rstrip() + "\n", dry_run=dry_run)
    if target.exists():
        _backup(target, dry_run=dry_run)
        if dry_run:
            print(f"[dry-run] 将删除 {target}")
        else:
            target.unlink()
    print(f"{'将卸载' if dry_run else '已卸载'} Kitty 配置")
    return 0


def _read_hook_payload() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Hook stdin 不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise TypeError("Hook stdin 必须是 JSON 对象")
    return value


async def register_kitty(config: Config, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {
        "command": "register_kitty",
        "session_id": args.session or os.environ.get("CODEX_THREAD_ID", ""),
        "cwd": args.cwd or os.getcwd(),
        "transcript_path": args.transcript or "",
        "kitty_socket": args.kitty_socket or os.environ.get("KITTY_LISTEN_ON", ""),
        "kitty_window_id": args.kitty_window_id or os.environ.get("KITTY_WINDOW_ID", ""),
        "agent": normalize_kind(args.agent),
    }
    return await control(config, payload)


async def hook_event(config: Config, agent: str = DEFAULT_KIND) -> int:
    agent = normalize_kind(agent)
    source = _read_hook_payload()
    hook_name = str(source.get("hook_event_name") or source.get("event") or "")
    normalized = hook_name.replace("-", "_").lower()
    prompt = str(source.get("prompt") or "").strip()
    if normalized == "userpromptsubmit" and prompt in {
        "qq-connect",
        "/qq-connect",
    }:
        registration = {
            "command": "register_kitty",
            "session_id": source.get("session_id") or "",
            "cwd": source.get("cwd") or os.getcwd(),
            "transcript_path": source.get("transcript_path") or "",
            "kitty_socket": os.environ.get("KITTY_LISTEN_ON", ""),
            "kitty_window_id": os.environ.get("KITTY_WINDOW_ID", ""),
            "agent": agent,
        }
        response = await exchange(config, registration)
        if response.get("ok"):
            decision: dict[str, object] = {
                "decision": "block",
                "reason": "QQ binding 已完成",
            }
            if agent == "claude":
                decision["suppressOriginalPrompt"] = True
            print(json.dumps(decision, ensure_ascii=False))
            return 0
        print(str(response.get("error") or "QQ binding 失败"), file=sys.stderr)
        return 2
    event_names = {
        "userpromptsubmit": "user_prompt_submit",
        "pretooluse": "pre_tool_use",
        "sessionstart": "session_start",
        "sessionend": "session_end",
        "precompact": "pre_compact",
        "postcompact": "post_compact",
        "notification": "notification",
    }
    normalized = event_names.get(normalized, normalized)
    payload: dict[str, object] = {
        "command": "hook_event",
        "event": normalized,
        "session_id": source.get("session_id") or os.environ.get("CODEX_THREAD_ID", ""),
        "turn_id": source.get("turn_id") or "",
        "payload": source,
        "kitty_socket": os.environ.get("KITTY_LISTEN_ON", ""),
        "kitty_window_id": os.environ.get("KITTY_WINDOW_ID", ""),
        "agent": agent,
    }
    response = await exchange(config, payload)
    if normalized == "stop":
        print("{}")
    elif not response.get("ok"):
        print(str(response.get("error") or "Hook 转发失败"), file=sys.stderr)
    return 0


async def doctor(config: Config, *, as_json: bool = False) -> int:
    command = shutil.which(config.kitty_command)
    codex_hooks = inspect_hook_definitions(Path("~/.codex/hooks.json").expanduser())
    claude_hooks = inspect_hook_definitions(
        Path("~/.claude/settings.json").expanduser(), ("hook-event --agent claude",)
    )
    bridge = await exchange(config, {"command": "status"})
    clipboard = KittyAdapter(config.kitty_command).clipboard_image_paste_available()
    warnings = [] if clipboard else [
        "wl-copy 不可用：Codex 图片粘贴不可用；文本和 Claude Code 路径传图不受影响"
    ]
    checks: dict[str, object] = {
        "ok": bool(command)
        and bool(bridge.get("ok"))
        and bool(bridge.get("qq"))
        and bool(codex_hooks["installed"] or claude_hooks["installed"]),
        "kitty_command": command,
        "bridge": bridge,
        "codex_hooks": codex_hooks,
        "claude_hooks": claude_hooks,
        "hook_trust": "unverified",
        "clipboard_image_paste": clipboard,
        "warnings": warnings,
    }
    socket = os.environ.get("KITTY_LISTEN_ON", "")
    window = os.environ.get("KITTY_WINDOW_ID", "")
    if command and socket and window.isdigit():
        failures = []
        for kind in PROFILES:
            try:
                await KittyAdapter(config.kitty_command).validate(socket, int(window), kind)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{kind}: {exc}")
            else:
                checks["kitty_remote_control"] = True
                checks["kitty_agent"] = kind
                break
        else:
            checks["ok"] = False
            checks["kitty_error"] = "; ".join(failures)
    else:
        warnings.append("未在可远程控制的 Kitty 窗口内运行，跳过当前窗口验证")
    if as_json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        print("Codex QQ Bridge doctor")
        for name in ("kitty_command", "codex_hooks", "claude_hooks", "bridge", "hook_trust"):
            print(f"- {name}: {checks[name]}")
        for warning in warnings:
            print(f"- WARN: {warning}")
        print(f"Result: {'PASS' if checks['ok'] else 'FAIL'}")
    return 0 if checks["ok"] else 1


def inspect_hook_definitions(
    path: Path, required: tuple[str, ...] = ("hook-event",)
) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"installed": False, "error": "hooks.json 不存在"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"installed": False, "error": str(exc)}
    serialized = json.dumps(value.get("hooks", {}) if isinstance(value, dict) else {})
    missing = [name for name in required if name not in serialized]
    return {"installed": not missing, "missing": missing}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="codex-qq")
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("onboard", help="scan QR code and configure the official QQ Bot")
    commands.add_parser("bridge", help="run the official QQ Bot bridge in the foreground")
    for name in (
        "install-kitty", "uninstall-kitty", "install-hooks", "uninstall-hooks",
        "install-claude-hooks", "uninstall-claude-hooks",
    ):
        item = commands.add_parser(name)
        item.add_argument("--dry-run", action="store_true")
    service = commands.add_parser("install-service", help="install the systemd user service")
    service.add_argument("--start", action="store_true")
    service.add_argument("--dry-run", action="store_true")
    service = commands.add_parser("uninstall-service", help="remove the systemd user service")
    service.add_argument("--stop", action="store_true")
    service.add_argument("--dry-run", action="store_true")
    commands.add_parser("status", help="show bridge state")
    disconnect_parser = commands.add_parser(
        "disconnect", help="unbind the selected Kitty session"
    )
    disconnect_parser.add_argument(
        "--thread", default=os.environ.get("CODEX_THREAD_ID")
    )
    doctor_parser = commands.add_parser("doctor", help="verify the local installation")
    doctor_parser.add_argument("--json", action="store_true")
    commands.add_parser(
        "sync-panel", help="create or update the owner's QQ command panel"
    )
    register = commands.add_parser("register-kitty", help="bind the current Kitty/Codex session")
    register.add_argument("--session", default=None)
    register.add_argument("--cwd", default=None)
    register.add_argument("--transcript", default=None)
    register.add_argument("--kitty-socket", default=None)
    register.add_argument("--kitty-window-id", default=None)
    register.add_argument("--agent", default=DEFAULT_KIND)
    hook_parser = commands.add_parser(
        "hook-event", help="forward an agent hook JSON payload from stdin"
    )
    hook_parser.add_argument("--agent", default=DEFAULT_KIND)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        config = Config.from_env(
            require_credentials=args.command in {"bridge", "sync-panel"}
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "onboard":
        raise SystemExit(asyncio.run(onboard(config)))
    if args.command == "install-kitty":
        raise SystemExit(install_kitty(dry_run=args.dry_run))
    if args.command == "uninstall-kitty":
        raise SystemExit(uninstall_kitty(dry_run=args.dry_run))
    if args.command == "install-hooks":
        raise SystemExit(install_hooks(dry_run=args.dry_run))
    if args.command == "uninstall-hooks":
        raise SystemExit(uninstall_hooks(Path("~/.codex/hooks.json").expanduser(), label="Codex", dry_run=args.dry_run))
    if args.command == "install-claude-hooks":
        raise SystemExit(install_claude_hooks(dry_run=args.dry_run))
    if args.command == "uninstall-claude-hooks":
        raise SystemExit(uninstall_hooks(Path("~/.claude/settings.json").expanduser(), label="Claude Code", dry_run=args.dry_run))
    if args.command == "install-service":
        raise SystemExit(install_service(_launcher_path(), start=args.start, dry_run=args.dry_run))
    if args.command == "uninstall-service":
        raise SystemExit(uninstall_service(stop=args.stop, dry_run=args.dry_run))
    if args.command == "bridge":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        asyncio.run(KittyBridge(config).run())
        return
    if args.command == "doctor":
        raise SystemExit(asyncio.run(doctor(config, as_json=args.json)))
    if args.command == "sync-panel":
        result = asyncio.run(sync_command_panel(config))
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return
    if args.command == "register-kitty":
        raise SystemExit(asyncio.run(register_kitty(config, args)))
    if args.command == "hook-event":
        raise SystemExit(asyncio.run(hook_event(config, args.agent)))
    payload: dict[str, object] = {"command": args.command}
    selected = getattr(args, "thread", None)
    if selected:
        payload["session_id"] = selected
    raise SystemExit(asyncio.run(control(config, payload)))
