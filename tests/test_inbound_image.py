from __future__ import annotations

import io
import stat
from types import SimpleNamespace

import httpx
from PIL import Image

from codex_qq_bridge.inbound_image import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    download_inbound_images,
)


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(output, format="PNG")
    return output.getvalue()


async def test_downloads_and_validates_https_image(tmp_path) -> None:
    content = png_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cdn.example/photo"
        return httpx.Response(200, content=content)

    attachment = SimpleNamespace(
        resolved_url="https://cdn.example/photo",
        content_type="image/png",
        filename="photo.png",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        paths = await download_inbound_images(
            http, [attachment], tmp_path / "images", "message-1"
        )

    assert len(paths.paths) == 1
    assert paths.candidate_count == 1
    assert paths.rejected == []
    assert paths.paths[0].suffix == ".png"
    with Image.open(paths.paths[0]) as image:
        assert image.format == "PNG"
        assert image.size == (8, 6)
    assert stat.S_IMODE(paths.paths[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.paths[0].parent.stat().st_mode) == 0o700


async def test_rejects_non_https_invalid_and_oversized_images(tmp_path) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, content=b"not an image")
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_IMAGE_BYTES + 1)},
            content=b"",
        )

    attachments = [
        SimpleNamespace(
            resolved_url="http://cdn.example/plain.png",
            content_type="image/png",
            filename="plain.png",
        ),
        SimpleNamespace(
            resolved_url="https://cdn.example/invalid.png",
            content_type="image/png",
            filename="invalid.png",
        ),
        SimpleNamespace(
            resolved_url="https://cdn.example/large.png",
            content_type="image/png",
            filename="large.png",
        ),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        paths = await download_inbound_images(
            http, attachments, tmp_path / "images", "message-2"
        )

    assert paths.paths == []
    assert paths.candidate_count == 3
    assert [item.reason for item in paths.rejected] == [
        "insecure_url",
        "invalid_image",
        "too_large",
    ]
    assert requests == 2


async def test_reports_images_over_per_message_limit(tmp_path) -> None:
    content = png_bytes()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    attachments = [
        SimpleNamespace(
            resolved_url=f"https://cdn.example/{index}.png",
            content_type="image/png",
            filename=f"{index}.png",
        )
        for index in range(MAX_IMAGES_PER_MESSAGE + 2)
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await download_inbound_images(
            http, attachments, tmp_path / "images", "message-3"
        )

    assert len(result.paths) == MAX_IMAGES_PER_MESSAGE
    assert result.candidate_count == MAX_IMAGES_PER_MESSAGE + 2
    assert result.truncated_count == 2
