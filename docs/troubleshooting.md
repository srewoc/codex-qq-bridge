# Troubleshooting

Start with `codex-qq doctor`; use `--json` for machine-readable output.

## Hook never binds

Restart the agent after installation. In Codex, run `/hooks`, inspect the exact Hook definitions, and trust them. Changed non-managed Hooks are skipped until trusted. Confirm you sent exactly `/qq-connect` in the intended session.

## Kitty remote control fails

Run `codex-qq install-kitty`, fully exit every Kitty process, and reopen it. `listen_on` is not hot-reloaded. Run diagnosis from the affected Kitty window when you need per-window validation.

## Bridge is unavailable

For foreground mode, keep `codex-qq bridge` running. For service mode, inspect:

```bash
systemctl --user status codex-qq-bridge.service
journalctl --user -u codex-qq-bridge.service -n 100
```

## Images fail but text works

Codex image paste on Wayland requires `wl-copy`. Its absence is a warning, not a bridge failure. Claude Code uses local image paths and does not require this clipboard path.

## Safe rollback

Installer edits create timestamped `.bak.<UTC timestamp>` files. Uninstall commands remove only managed entries and preserve unrelated configuration.
