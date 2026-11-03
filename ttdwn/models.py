"""Value objects: a downloadable rendition (:class:`Format`) and a post
(:class:`Media`). Both are plain data — no I/O happens in this module.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

from .ui import format_bytes

__all__ = ["Format", "Media", "quality_label", "LADDER"]

LADDER = (2160, 1440, 1080, 720, 540, 480, 360, 240, 144)


def quality_label(width: int | None, height: int | None) -> str:
    """Name a rendition the way TikTok does: by the short side.

    A 576×1024 vertical clip is '540p', a 1080×1920 clip is '1080p'.
    """
    if not width or not height:
        return "?"
    short = min(width, height)
    for step in LADDER:
        if short >= step - 8:
            return f"{step}p"
    return f"{short}p"


@dataclass
class Format:
    """One selectable rendition, possibly reachable through several URLs."""

    width: int | None
    height: int | None
    bitrate: int | None = None
    gear: str = ""
    codec: str = "h264"
    size: int | None = None
    urls: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    watermarked: bool = False

    # -- derived -----------------------------------------------------
    @property
    def quality(self) -> str:
        return quality_label(self.width, self.height)

    @property
    def short_side(self) -> int:
        return min(self.width or 0, self.height or 0)

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "?"

    def add_url(self, url: str, source: str) -> None:
        if url and url not in self.urls:
            self.urls.append(url)
        if source not in self.sources:
            self.sources.append(source)

    def sort_key(self) -> tuple:
        return (self.watermarked, -self.pixels, -(self.bitrate or 0))

    def describe(self) -> str:
        parts = [self.quality, self.resolution]
        if self.bitrate:
            parts.append(f"{self.bitrate / 1000:.0f} kbps")
        parts.append(self.codec)
        if self.size:
            parts.append(format_bytes(self.size))
        if self.watermarked:
            parts.append("watermark")
        return " · ".join(parts)


@dataclass
class Media:
    """A TikTok post: video renditions, or a photo carousel."""

    id: str
    desc: str = ""
    author_id: str = ""
    author: str = ""
    nickname: str = ""
    created: int = 0
    duration_ms: int = 0
    formats: list[Format] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    cover_urls: list[str] = field(default_factory=list)
    music: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    url: str = ""
    sources: list[str] = field(default_factory=list)

    # -- derived -----------------------------------------------------
    @property
    def is_photo(self) -> bool:
        return bool(self.image_urls)

    @property
    def duration(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def best(self) -> Format | None:
        formats = self.video_formats
        return max(
            formats,
            key=lambda f: (f.pixels, 1 if f.codec == "h264" else 0, f.bitrate or 0),
        ) if formats else None

    @property
    def video_formats(self) -> list[Format]:
        return [f for f in self.formats if not f.watermarked]

    @property
    def watermarked_formats(self) -> list[Format]:
        return [f for f in self.formats if f.watermarked]

    @property
    def created_iso(self) -> str:
        if not self.created:
            return ""
        try:
            return _dt.datetime.fromtimestamp(self.created, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (OverflowError, OSError, ValueError):
            return ""

    @property
    def permalink(self) -> str:
        return self.url or (f"https://www.tiktok.com/@{self.author}/video/{self.id}" if self.author else "")

    def sort_formats(self) -> None:
        self.formats.sort(key=lambda f: f.sort_key())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.permalink,
            "description": self.desc,
            "created": self.created_iso,
            "duration": round(self.duration, 3),
            "author": {"unique_id": self.author, "nickname": self.nickname, "id": self.author_id},
            "music": self.music,
            "stats": self.stats,
            "sources": self.sources,
            "images": self.image_urls,
            "cover": self.cover_urls[:1],
            "formats": [
                {
                    "quality": f.quality,
                    "resolution": f.resolution,
                    "bitrate": f.bitrate,
                    "gear": f.gear,
                    "codec": f.codec,
                    "size": f.size,
                    "watermark": f.watermarked,
                    "urls": f.urls,
                }
                for f in self.formats
            ],
        }
