from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

MARKER = "managed-by-codex-qq-bridge"
NAME = "codex-qq-bridge.service"


def render_unit(executable: Path, kitten: Path) -> str:
    return f"""# {MARKER}
[Unit]
Description=Codex QQ Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} bridge
Environment=CODEX_QQ_KITTY_COMMAND={kitten}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=default.target
"""


def install(executable: Path, *, start: bool, dry_run: bool) -> int:
    target = Path("~/.config/systemd/user").expanduser() / NAME
    kitten = Path(shutil.which("kitten") or "kitten")
    if dry_run:
        print(f"[dry-run] 将写入 {target}\n{render_unit(executable, kitten)}")
        if start:
            print(f"[dry-run] 将启用并启动 {NAME}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(render_unit(executable, kitten), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if start:
        subprocess.run(["systemctl", "--user", "enable", "--now", NAME], check=True)
    print(f"已安装用户服务：{target}")
    return 0


def uninstall(*, stop: bool, dry_run: bool) -> int:
    target = Path("~/.config/systemd/user").expanduser() / NAME
    if target.exists() and MARKER not in target.read_text(encoding="utf-8", errors="ignore"):
        raise RuntimeError(f"拒绝删除非受管服务：{target}")
    if dry_run:
        print(f"[dry-run] 将删除 {target}")
        return 0
    if stop:
        subprocess.run(["systemctl", "--user", "disable", "--now", NAME], check=False)
    if target.exists():
        target.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("已卸载用户服务")
    return 0
