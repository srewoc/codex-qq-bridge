from __future__ import annotations

import json

from codex_qq_bridge.config import Config


def test_credentials_round_trip_with_private_permissions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_QQ_CONFIG_DIR", str(tmp_path))
    config = Config.from_env(require_credentials=False)
    config.save_credentials("app", "secret", "owner")

    assert json.loads(config.credential_file.read_text()) == {
        "app_id": "app",
        "app_secret": "secret",
        "owner_openid": "owner",
    }
    assert config.credential_file.stat().st_mode & 0o777 == 0o600
