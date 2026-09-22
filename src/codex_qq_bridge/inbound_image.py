from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
CACHE_TTL_SECONDS = 24 * 60 * 60
FORMAT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}

REJECTION_LABELS = {
    "insecure_url": "下载地址不安全",
    "download_failed": "下载失败",
    "too_large": "文件超过 20 MB",
    "unsupported_format": "格式不受支持",
    "too_many_pixels": "图片尺寸过大",
    "invalid_image": "图片已损坏或内容无效",
}


@dataclass(frozen=True)
class ImageRejection:
    index: int
    reason: str


@dataclass
class InboundImageBatch:
    paths: list[Path] = field(default_factory=list)
    candidate_count: int = 0
    rejected: list[ImageRejection] = field(default_factory=list)
    truncated_count: int = 0

    @classmethod
    def from_paths(cls, paths: Sequence[Path]) -> InboundImageBatch:
        values = list(paths)
        return cls(paths=values, candidate_count=len(values))

    @property
    def has_images(self) -> bool:
        return self.candidate_count > 0

    def rejection_summary(self) -> str:
        counts: dict[str, int] = {}
        for item in self.rejected:
            label = REJECTION_LABELS.get(item.reason, "处理失败")
            counts[label] = counts.get(label, 0) + 1
        if self.truncated_count:
            counts["超过每条消息 4 张限制"] = self.truncated_count
        return "；".join(
            f"{label} {count} 张" for label, count in counts.items()
        )


class ImageAttachment(Protocol):
    resolved_url: str
    content_type: str
    filename: str


async def download_inbound_images(
    http: httpx.AsyncClient,
    attachments: Sequence[ImageAttachment],
    cache_dir: Path,
    message_id: str,
) -> InboundImageBatch:
    prune_image_cache(cache_dir)
    images = [attachment for attachment in attachments if _looks_like_image(attachment)]
    result = InboundImageBatch(
        candidate_count=len(images),
        truncated_count=max(0, len(images) - MAX_IMAGES_PER_MESSAGE),
    )
    for index, attachment in enumerate(images[:MAX_IMAGES_PER_MESSAGE], start=1):
        downloaded, reason = await _download_one(
            http,
            attachment.resolved_url,
            cache_dir,
            message_id,
            index,
        )
        if downloaded is not None:
            result.paths.append(downloaded)
        elif reason:
            result.rejected.append(ImageRejection(index=index, reason=reason))
    return result


def _looks_like_image(attachment: ImageAttachment) -> bool:
    content_type = attachment.content_type.lower().strip()
    if content_type.startswith("image/"):
        return True
    return Path(attachment.filename.lower()).suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}


async def _download_one(
    http: httpx.AsyncClient,
    url: str,
    cache_dir: Path,
    message_id: str,
    index: int,
) -> tuple[Path | None, str | None]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None, "insecure_url"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_dir.chmod(0o700)
    digest = hashlib.sha256(f"{message_id}:{index}:{url}".encode()).hexdigest()[:24]
    cached = cache_dir / f"qq-{digest}.png"
    if cached.is_file():
        return cached, None
    temporary = cache_dir / f"qq-{digest}.part"
    size = 0
    try:
        async with http.stream("GET", url, timeout=30.0) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length", "0") or 0)
            if declared > MAX_IMAGE_BYTES:
                return None, "too_large"
            with temporary.open("wb") as stream:
                temporary.chmod(0o600)
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        return None, "too_large"
                    stream.write(chunk)
        with Image.open(temporary) as image:
            image_format = str(image.format or "").upper()
            if image_format not in FORMAT_EXTENSIONS:
                return None, "unsupported_format"
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return None, "too_many_pixels"
            image.seek(0)
            image.load()
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            normalized.save(cached, format="PNG", optimize=True)
        cached.chmod(0o600)
        return cached, None
    except httpx.HTTPError:
        return None, "download_failed"
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None, "invalid_image"
    finally:
        temporary.unlink(missing_ok=True)


def prune_image_cache(cache_dir: Path) -> None:
    if not cache_dir.is_dir():
        return
    cutoff = time.time() - CACHE_TTL_SECONDS
    for path in cache_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
