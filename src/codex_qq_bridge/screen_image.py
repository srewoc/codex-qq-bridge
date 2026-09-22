from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .screen_theme import ScreenTheme, substitute_glyphs, theme_for

IMAGE_WIDTH = 1400
MAX_LINES = 120
FONT_SIZE = 23
SMALL_FONT_SIZE = 18
LINE_HEIGHT = 36
HEADER_HEIGHT = 76
FOOTER_HEIGHT = 58
PADDING_X = 44
# (path, ttc_index). A TUI screenshot only lines up when every cell advances by
# the same width, so the monospace face inside the CJK collection is required:
# index 0 is the proportional `Noto Sans CJK JP` (M=18.67px vs i=6.33px), while
# index 7 is `Noto Sans Mono CJK SC` with a strict 1:2 halfwidth:fullwidth ratio.
FONT_CANDIDATES: tuple[tuple[Path, int], ...] = (
    (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 7),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"), 0),
)


class ScreenRenderError(RuntimeError):
    pass


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in FONT_CANDIDATES:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size, index=index)
        except OSError:
            continue
    raise ScreenRenderError("找不到可用的终端字体")


def _wrap_line(
    line: str, draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    line = line.expandtabs(4)
    if not line:
        return [""]
    indent = line[: len(line) - len(line.lstrip())]
    wrapped: list[str] = []
    current = ""
    for character in line:
        candidate = current + character
        if current and draw.textlength(candidate, font=font) > max_width:
            wrapped.append(current.rstrip())
            current = indent + character if character.strip() else indent
        else:
            current = candidate
    wrapped.append(current.rstrip())
    return wrapped


def _visual_lines(text: str, font: ImageFont.FreeTypeFont) -> list[str]:
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_width = IMAGE_WIDTH - PADDING_X * 2
    lines: list[str] = []
    for line in substitute_glyphs(text).rstrip().splitlines() or [""]:
        lines.extend(_wrap_line(line, measure, font, max_width))
    if len(lines) > MAX_LINES:
        lines = ["[screen 前部已截断]"] + lines[-(MAX_LINES - 1) :]
    return lines


def render_screen_images(
    text: str,
    *,
    label: str,
    project_name: str,
    output_dir: Path,
    kind: str | None = None,
    now: datetime | None = None,
) -> list[Path]:
    theme: ScreenTheme = theme_for(kind)
    font = _load_font(theme.font_size)
    small = _load_font(SMALL_FONT_SIZE)
    lines = _visual_lines(text, font)
    page = lines or [""]
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    safe_project = " ".join(project_name.split()) or "unknown"
    safe_label = " ".join(label.split()) or "session"
    paths: list[Path] = []
    try:
        height = HEADER_HEIGHT + 30 + len(page) * theme.line_height + FOOTER_HEIGHT + 28
        image = Image.new("RGB", (IMAGE_WIDTH, height), "#0b0f14")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (14, 14, IMAGE_WIDTH - 14, height - 14),
            radius=24,
            fill="#121821",
            outline="#2b3542",
            width=2,
        )
        for x, color in ((44, "#ff5f57"), (72, "#febc2e"), (100, "#28c840")):
            draw.ellipse((x - 8, 32, x + 8, 48), fill=color)
        draw.text(
            (136, 27),
            f"{safe_label}  ·  {safe_project}",
            font=small,
            fill="#aeb9c7",
        )
        draw.line((34, HEADER_HEIGHT, IMAGE_WIDTH - 34, HEADER_HEIGHT), fill="#28313d", width=2)
        y = HEADER_HEIGHT + 24
        for line in page:
            draw.text((PADDING_X, y), line, font=font, fill=theme.color_for(line))
            y += theme.line_height
        draw.line(
            (34, height - FOOTER_HEIGHT - 10, IMAGE_WIDTH - 34, height - FOOTER_HEIGHT - 10),
            fill="#28313d",
            width=2,
        )
        draw.text(
            (PADDING_X, height - FOOTER_HEIGHT + 4),
            f"/qq-screen · {timestamp} · 当前可见屏幕",
            font=small,
            fill="#7f8b99",
        )
        path = output_dir / f"screen-{uuid.uuid4().hex}.png"
        image.save(path, format="PNG", optimize=True)
        os.chmod(path, 0o600)
        paths.append(path)
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return paths
