from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_qq_bridge import __version__
from codex_qq_bridge.cli import install_hooks, parser, uninstall_hooks
from codex_qq_bridge.service import render_unit


def test_public_version_is_010() -> None:
    assert __version__ == "0.1.0"


@pytest.mark.parametrize("removed", ["codex", "codex-provider", "connect", "install-wrapper"])
def test_legacy_app_server_commands_are_removed(removed: str) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args([removed])


def test_hook_path_migration_preserves_unrelated_and_uninstalls_managed(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    old = "/old/venv/bin/codex-qq hook-event"
    target.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "notify-send done"}]},
        {"hooks": [{"type": "command", "command": old}]},
    ]}}))

    install_hooks(target, tmp_path / "bin" / "codex-qq")
    value = json.loads(target.read_text())
    commands = [group["hooks"][0]["command"] for group in value["hooks"]["Stop"]]
    assert commands[0] == "notify-send done"
    assert old not in commands
    assert any(str(tmp_path / "bin" / "codex-qq") in command for command in commands)
    assert list(tmp_path.glob("hooks.json.bak.*"))

    uninstall_hooks(target, label="Codex")
    value = json.loads(target.read_text())
    assert value["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "notify-send done"}]}
    ]


def test_service_unit_has_no_repository_path_or_credentials() -> None:
    unit = render_unit(Path("/opt/bin/codex-qq"), Path("/usr/bin/kitten"))
    assert "ExecStart=/opt/bin/codex-qq bridge" in unit
    assert "WorkingDirectory=" not in unit
    assert "credentials" not in unit.lower()
    assert "/home/cubt" not in unit
    assert "NoNewPrivileges=true" in unit
