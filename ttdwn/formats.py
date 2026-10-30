"""Quality selection.

The ladder a post offers is not fixed: 540p/720p/1080p are common, vertical
clips are described by their short side, and TikTok sometimes ships 576×1024
instead of 540×960. So selection works on the short side of the frame, accepts
``1080``/``1080p``/``best``/``worst``/``2`` (1-based index), and always falls
back to the closest thing it has instead of failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .errors import UsageError
from .models import LADDER, Format, Media

__all__ = ["pick", "ranked", "parse_quality", "Selection"]

_QUALITY_RE = re.compile(r"^\s*(\d{3,4})\s*[pP]?\s*$")


@dataclass
class Selection:
    fmt: Format
    note: str = ""
    available: int = 0


def ranked(media: Media, *, allow_watermark: bool = False,
           max_quality: int | None = None,
           codec: str | None = None) -> list[Format]:
    """Watermark-free renditions, best first."""
    items = [f for f in media.formats if allow_watermark or not f.watermarked]
    if max_quality:
        capped = [f for f in items if f.short_side and f.short_side <= max_quality]
        items = capped or sorted(items, key=lambda f: f.pixels)[:1]
    if codec:
        preferred = [f for f in items if f.codec == codec]
        items = preferred or items
    return sorted(items, key=_sort_key, reverse=True)


def _sort_key(fmt: Format) -> tuple:
    # resolution first, then the codec almost every player understands, then bitrate
    return (fmt.pixels, 1 if fmt.codec == "h264" else 0, fmt.bitrate or 0)


def parse_quality(spec: str | None) -> tuple[str, int | None]:
    """Normalise a ``--quality`` value into (kind, value)."""
    if spec is None:
        return ("best", None)
    text = str(spec).strip().lower()
    if text in ("", "best", "max", "highest", "b", "auto"):
        return ("best", None)
    if text in ("worst", "min", "lowest", "smallest"):
        return ("worst", None)
    if text.startswith("#"):
        text = text[1:]
    match = _QUALITY_RE.match(text)
    if match:
        value = int(match.group(1))
        if value < 100 or value > 4320:
            raise UsageError(f"quality '{spec}' is out of range")
        return ("height", value)
    if text.isdigit():
        index = int(text)
        if index < 1 or index > 99:
            raise UsageError(f"quality index '{spec}' is out of range")
        return ("index", index)
    raise UsageError(
        f"cannot parse quality '{spec}' — use best, worst, a height such as 1080, or an index like 2"
    )


def pick(
    media: Media,
    spec: str | None = None,
    *,
    allow_watermark: bool = False,
    max_quality: str | None = None,
    codec: str | None = None,
) -> Selection:
    """Choose one rendition. Raises if the post has nothing to download."""
    cap_kind, cap = parse_quality(max_quality) if max_quality else ("best", None)
    cap_value = cap if cap_kind == "height" else None
    items = ranked(media, allow_watermark=allow_watermark, max_quality=cap_value, codec=codec)
    if not items:
        raise UsageError(f"post {media.id} has no watermark-free video stream")

    kind, value = parse_quality(spec)
    note = ""

    if kind == "best":
        chosen = items[0]
    elif kind == "worst":
        chosen = items[-1]
    elif kind == "index":
        if value and value <= len(items):
            chosen = items[value - 1]
        else:
            raise UsageError(f"quality index {value} does not exist — this post has {len(items)} renditions")
    else:  # height
        target = value or 0
        exact = [f for f in items if f.short_side == target or f.quality == f"{target}p"]
        if exact:
            chosen = max(exact, key=_sort_key)
        else:
            at_or_below = [f for f in items if f.short_side <= target]
            if at_or_below:
                chosen = max(at_or_below, key=_sort_key)
                note = f"no {target}p in this post — using {chosen.quality}"
            else:
                chosen = min(items, key=_sort_key)
                note = f"{target}p not available — this post tops out at {items[0].quality}"
    if codec and chosen.codec != codec:
        note = (note + "; " if note else "") + f"no {codec} stream at {chosen.quality}, using {chosen.codec}"
    if chosen.watermarked:
        note = (note + "; " if note else "") + "watermarked stream"
    return Selection(fmt=chosen, note=note, available=len(items))


def ladder_summary(media: Media) -> str:
    """'540p · 720p · 1080p' — one line for logs."""
    seen: list[str] = []
    for fmt in sorted(media.video_formats, key=lambda f: f.short_side):
        if fmt.quality not in seen:
            seen.append(fmt.quality)
    return " · ".join(seen) if seen else "none"


def human_ladder(formats: Sequence[Format]) -> list[str]:
    out: list[str] = []
    for fmt in formats:
        out.append(fmt.describe())
    return out


def standard_steps() -> Sequence[int]:
    return LADDER
