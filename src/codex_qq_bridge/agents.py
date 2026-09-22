from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    """Per-agent behaviour that differs between Codex and Claude Code TUIs."""

    kind: str
    ready_markers: tuple[str, ...]
    window_markers: tuple[str, ...]
    stop_key: str
    blocked_keys: frozenset[str]
    image_channel: str


DEFAULT_KIND = "codex"
UNKNOWN_READY_DELAY_SECONDS = 3.0

PROFILES: dict[str, AgentProfile] = {
    "codex": AgentProfile(
        kind="codex",
        ready_markers=("OpenAI Codex",),
        window_markers=("codex",),
        stop_key="ctrl+c",
        blocked_keys=frozenset(),
        image_channel="clipboard",
    ),
    "claude": AgentProfile(
        kind="claude",
        ready_markers=("Claude Code",),
        window_markers=("claude",),
        # Claude Code interrupts on `esc`; a single `ctrl+c` only clears the
        # composer and two in a row quit the program, which would drop the
        # binding without the user meaning to.
        stop_key="esc",
        blocked_keys=frozenset({"ctrl+c"}),
        # Claude Code reads the clipboard itself with two separate `wl-paste`
        # calls, which `wl-copy --paste-once` cannot reliably serve. Absolute
        # paths in the prompt are read back by its own Read tool instead.
        image_channel="path",
    ),
}

# Launch-time hint only: the authoritative kind always comes from the
# registering hook's `--agent` declaration.
LAUNCH_HINTS: dict[str, str] = {
    "codex": "codex",
    "claude": "claude",
}


def normalize_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in PROFILES else DEFAULT_KIND


def profile_for(kind: object) -> AgentProfile:
    return PROFILES[normalize_kind(kind)]


def hint_for(command: str) -> AgentProfile | None:
    """Readiness profile for a `/qq-new` command token, or None when unknown."""
    kind = LAUNCH_HINTS.get(command.strip().lower())
    return PROFILES[kind] if kind else None
