# Architecture

The supported path is QQ Bot → local bridge → Kitty remote control → Codex CLI or Claude Code. Agent lifecycle Hooks send session identity and state changes back to the bridge over a local Unix socket.

The QQ transport authenticates with credentials created by `codex-qq onboard` and accepts commands only from the recorded owner OpenID. `KittyBridge` owns the active binding, checks the session identity on Hook events, injects text or keys, captures visible terminal content, and relays results to QQ.

Codex and Claude Code differ in interruption keys, readiness markers, and image delivery. These differences live in agent profiles. The App Server/JSON-RPC transport is intentionally not part of the public architecture.

State and credentials live outside the repository under the user's XDG-style config/state directories. The service unit contains executable paths but no secrets and uses a private umask.
