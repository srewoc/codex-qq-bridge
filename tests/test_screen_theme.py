from __future__ import annotations

from codex_qq_bridge.screen_theme import (
    CLAUDE_THEME,
    CODEX_THEME,
    DEFAULT_TEXT_COLOR,
    MUTED_TEXT_COLOR,
    substitute_glyphs,
    theme_for,
)


def test_theme_for_selects_per_agent_palette() -> None:
    assert theme_for("claude") is CLAUDE_THEME
    assert theme_for("codex") is CODEX_THEME
    assert theme_for("CLAUDE") is CLAUDE_THEME


def test_theme_for_falls_back_to_codex_on_unknown_kind() -> None:
    assert theme_for(None) is CODEX_THEME
    assert theme_for("gemini") is CODEX_THEME


def test_claude_theme_colours_its_own_markers_distinctly() -> None:
    tool_call = CLAUDE_THEME.color_for("⏺ Bash(ls)")
    tool_result = CLAUDE_THEME.color_for("  ⎿ 271: matched")
    auto_mode = CLAUDE_THEME.color_for("⏵⏵ auto mode on")

    assert tool_call != DEFAULT_TEXT_COLOR
    assert tool_result == MUTED_TEXT_COLOR
    assert auto_mode != DEFAULT_TEXT_COLOR
    assert len({tool_call, tool_result, auto_mode}) == 3


def test_claude_theme_matches_fallback_glyphs_too() -> None:
    """Rules must fire whether or not glyph substitution already ran."""
    assert CLAUDE_THEME.color_for("⏺ Bash(ls)") == CLAUDE_THEME.color_for(
        "● Bash(ls)"
    )


def test_composer_prompt_wins_over_surrounding_border() -> None:
    bare = CLAUDE_THEME.color_for("> 实现群聊支持")
    boxed = CLAUDE_THEME.color_for("│ > 实现群聊支持              │")
    border = CLAUDE_THEME.color_for("╭───╮")

    assert boxed == bare
    assert boxed != border


def test_markdown_bullet_is_not_coloured_as_a_removed_diff_line() -> None:
    assert CLAUDE_THEME.color_for("- 新增群白名单配置项") == DEFAULT_TEXT_COLOR


def test_codex_palette_is_unchanged() -> None:
    assert CODEX_THEME.color_for("› 用户问题") == "#67d4ff"
    assert CODEX_THEME.color_for("└ tool") == MUTED_TEXT_COLOR
    assert CODEX_THEME.color_for("• item") == "#b7c3d0"
    assert CODEX_THEME.color_for("plain text") == DEFAULT_TEXT_COLOR


def test_substitute_glyphs_maps_unrenderable_markers() -> None:
    assert substitute_glyphs("⏺ done") == "● done"
    assert substitute_glyphs("⏵⏵ auto") == "▶▶ auto"
    assert substitute_glyphs("✻ thinking") == "✽ thinking"


def test_substitute_glyphs_leaves_covered_text_untouched() -> None:
    text = "│ ─ ⎿ 中文 code()"
    assert substitute_glyphs(text) is text
