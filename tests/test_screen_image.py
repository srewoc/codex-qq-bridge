from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from codex_qq_bridge.screen_image import IMAGE_WIDTH, MAX_LINES, render_screen_images


def test_render_screen_image_supports_chinese_and_long_lines(tmp_path: Path) -> None:
    paths = render_screen_images(
        "› 用户问题\n" + "中文 and English " * 30,
        label="codex #2",
        project_name="codex-qq-bridge",
        output_dir=tmp_path,
        now=datetime(2026, 9, 20, 12, 34, tzinfo=UTC),
    )

    assert len(paths) == 1
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    with Image.open(paths[0]) as image:
        assert image.format == "PNG"
        assert image.width == IMAGE_WIDTH
        assert image.height > 300


def test_render_screen_image_caps_output_in_one_tall_image(tmp_path: Path) -> None:
    paths = render_screen_images(
        "\n".join(f"line {index}" for index in range(300)),
        label="claude #1",
        project_name="project",
        output_dir=tmp_path,
    )

    assert len(paths) == 1
    with Image.open(paths[0]) as image:
        assert image.height > MAX_LINES * 30
