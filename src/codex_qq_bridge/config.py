from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    owner_openid: str
    control_socket: Path
    state_dir: Path
    credential_file: Path
    message_chunk_size: int = 1800
    kitty_command: str = "kitten"
    interaction_timeout_seconds: int = 600
    control_connect_timeout_seconds: float = 2.0
    control_response_timeout_seconds: float = 10.0
    screen_max_chars: int = 6000
    control_max_bytes: int = 262_144

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> Config:
        state_dir = Path(
            os.environ.get("CODEX_QQ_STATE_DIR", "~/.local/state/codex-qq-bridge")
        ).expanduser()
        config_dir = Path(
            os.environ.get("CODEX_QQ_CONFIG_DIR", "~/.config/codex-qq-bridge")
        ).expanduser()
        credential_file = Path(
            os.environ.get(
                "CODEX_QQ_CREDENTIAL_FILE", str(config_dir / "credentials.json")
            )
        ).expanduser()
        stored = _read_credentials(credential_file)
        app_id = os.environ.get("QQ_BOT_APP_ID", str(stored.get("app_id", ""))).strip()
        app_secret = os.environ.get(
            "QQ_BOT_APP_SECRET", str(stored.get("app_secret", ""))
        ).strip()
        owner_openid = os.environ.get(
            "QQ_BOT_OWNER_OPENID", str(stored.get("owner_openid", ""))
        ).strip()
        if require_credentials:
            missing = [
                name
                for name, value in (
                    ("QQ_BOT_APP_ID", app_id),
                    ("QQ_BOT_APP_SECRET", app_secret),
                    ("QQ_BOT_OWNER_OPENID", owner_openid),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "缺少官方 QQ 机器人配置："
                    + ", ".join(missing)
                    + "；请先运行 codex-qq onboard"
                )
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            owner_openid=owner_openid,
            control_socket=Path(
                os.environ.get(
                    "CODEX_QQ_CONTROL_SOCKET", str(state_dir / "bridge.sock")
                )
            ).expanduser(),
            state_dir=state_dir,
            credential_file=credential_file,
            message_chunk_size=int(
                os.environ.get("CODEX_QQ_MESSAGE_CHUNK_SIZE", "1800")
            ),
            kitty_command=os.environ.get("CODEX_QQ_KITTY_COMMAND", "kitten").strip()
            or "kitten",
            interaction_timeout_seconds=int(
                os.environ.get("CODEX_QQ_INTERACTION_TIMEOUT_SECONDS", "600")
            ),
            control_connect_timeout_seconds=float(
                os.environ.get("CODEX_QQ_CONTROL_CONNECT_TIMEOUT_SECONDS", "2")
            ),
            control_response_timeout_seconds=float(
                os.environ.get("CODEX_QQ_CONTROL_RESPONSE_TIMEOUT_SECONDS", "10")
            ),
            screen_max_chars=int(
                os.environ.get("CODEX_QQ_SCREEN_MAX_CHARS", "6000")
            ),
            control_max_bytes=int(
                os.environ.get("CODEX_QQ_CONTROL_MAX_BYTES", "262144")
            ),
        )

    def save_credentials(self, app_id: str, app_secret: str, owner_openid: str) -> None:
        self.credential_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credential_file.write_text(
            json.dumps(
                {
                    "app_id": app_id,
                    "app_secret": app_secret,
                    "owner_openid": owner_openid,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.credential_file.chmod(0o600)


def _read_credentials(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 QQ 机器人凭据文件 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"QQ 机器人凭据文件格式错误：{path}")
    return value
