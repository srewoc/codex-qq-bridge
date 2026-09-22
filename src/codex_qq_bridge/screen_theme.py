from __future__ import annotations

from dataclasses import dataclass

# Glyphs the bundled Noto Sans Mono CJK face has no coverage for, mapped to the
# closest shape it does render. Without this the TUI markers that matter most
# (Claude Code's tool-call dot, its auto-accept chevrons) come out as .notdef
# boxes in the QQ screenshot.
GLYPH_FALLBACKS: dict[str, str] = {
    "⏺": "●",  # ⏺ tool call  -> ●
    "⏵": "▶",  # ⏵ auto mode  -> ▶
    "▸": "▶",  # ▸            -> ▶
    "✻": "✽",  # ✻ spinner    -> ✽
    "✳": "✽",  # ✳ spinner    -> ✽
    "⣿": "█",  # ⣿ progress   -> █
    "✔": "✓",  # ✔            -> ✓
    "✗": "×",  # ✗            -> ×
    "⌥": "Alt",     # ⌥            -> Alt
}

DEFAULT_TEXT_COLOR = "#e6edf3"
MUTED_TEXT_COLOR = "#8793a2"


# Leading characters that make up a composer border plus its inner padding.
BOX_EDGE_CHARS = "\u2502\u2503| \t"


@dataclass(frozen=True)
class LineRule:
    """Colour applied to a screen line whose stripped form starts with a prefix."""

    prefixes: tuple[str, ...]
    color: str
    after_box: bool = False
    """Match after peeling a leading composer border, so the prompt inside the
    input box wins over the border rule that would otherwise claim the line."""


@dataclass(frozen=True)
class ScreenTheme:
    """Per-agent rendering rules for the `/qq-screen` image."""

    kind: str
    rules: tuple[LineRule, ...]
    default_color: str = DEFAULT_TEXT_COLOR
    font_size: int = 23
    line_height: int = 36

    def color_for(self, line: str) -> str:
        stripped = line.lstrip()
        if not stripped:
            return self.default_color
        inner = stripped.lstrip(BOX_EDGE_CHARS)
        for rule in self.rules:
            target = inner if rule.after_box else stripped
            if target and target.startswith(rule.prefixes):
                return rule.color
        return self.default_color


# Codex TUI: the original palette, kept byte-for-byte so existing screenshots
# do not change appearance.
CODEX_THEME = ScreenTheme(
    kind="codex",
    rules=(
        LineRule(("›",), "#67d4ff"),              # › prompt
        LineRule(("└", "│"), MUTED_TEXT_COLOR),  # └ │ tree
        LineRule(("•",), "#b7c3d0"),              # • bullet
    ),
)

# Claude Code TUI: different markers entirely. Rules are ordered most specific
# first, and accept both the original glyph and its GLYPH_FALLBACKS shape so
# matching does not depend on whether substitution already ran.
CLAUDE_THEME = ScreenTheme(
    kind="claude",
    rules=(
        # ⏵⏵ auto mode / permission banner
        LineRule(("⏵", "▶"), "#4fb06d"),
        # ⏺ tool invocation
        LineRule(("⏺", "●"), "#4ec9b0"),
        # ⎿ tool result / collapsed output
        LineRule(("⎿",), MUTED_TEXT_COLOR),
        # ✻ ✽ thinking + "Worked for" status
        LineRule(("✻", "✽", "✳"), "#b48ead"),
        # user prompt, both bare and wrapped in the composer border
        LineRule((">",), "#67d4ff", after_box=True),
        # composer / table box drawing
        LineRule(
            ("╭", "╮", "╰", "╯", "├", "┤",
             "┬", "┴", "┼", "─", "│"),
            "#3f4a59",
        ),
        # Added diff lines. There is deliberately no matching rule for a bare
        # leading "-": Claude Code renders diffs with a line-number gutter, so a
        # line starting with "-" is far more often a Markdown bullet in prose.
        LineRule(("+",), "#4fb06d"),
        # inline hints
        LineRule(("Tip:", "Note:", "Warning:"), "#e0c27a"),
        # bullets in assistant prose
        LineRule(("•",), "#b7c3d0"),
    ),
)

THEMES: dict[str, ScreenTheme] = {
    CODEX_THEME.kind: CODEX_THEME,
    CLAUDE_THEME.kind: CLAUDE_THEME,
}

DEFAULT_THEME = CODEX_THEME


def theme_for(kind: object) -> ScreenTheme:
    """Screen theme for an agent kind, falling back to the Codex palette."""
    return THEMES.get(str(kind or "").strip().lower(), DEFAULT_THEME)


def substitute_glyphs(text: str) -> str:
    """Replace glyphs the render font cannot draw with shapes it can."""
    if not any(char in text for char in GLYPH_FALLBACKS):
        return text
    return "".join(GLYPH_FALLBACKS.get(char, char) for char in text)
