"""Turn whatever the user pasted into an aweme id.

Accepts bare ids, www/mobile links, embed links, ``/t/`` and ``vm.tiktok.com``
short links (followed over the network), and links with tracking parameters.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from .errors import DownloadError, ResolveError
from .http import Session

__all__ = ["Ref", "resolve", "parse_reference", "BROWSER_UA"]

# A desktop Chrome UA. Short links answer differently (or not at all) to
# anything that looks like a bot, so this is deliberately unremarkable.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

TIKTOK_HOSTS = (
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "t.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "v.tiktok.com",
    "tiktokv.com",
    "www.tiktokv.com",
    "m.tiktokv.com",
    "tiktok.com.tr",
)

SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "v.tiktok.com", "t.tiktok.com", "tiktokv.com"}

_ID_RE = re.compile(r"^\d{6,25}$")
_PATH_ID_RE = re.compile(r"/(?:video|photo|v|embed/v2|embed|player/v1)/(\d{6,25})")
_QUERY_ID_RE = re.compile(r"[?&](?:aweme_id|item_id|video_id)=(\d{6,25})")
_AUTHOR_RE = re.compile(r"/@([A-Za-z0-9._]{1,64})")
_SHORT_PATH_RE = re.compile(r"^/[A-Za-z0-9]{4,}/?$")


@dataclass
class Ref:
    """A resolved reference to a post."""

    aweme_id: str
    author: str = ""
    kind: str = "video"  # 'video' or 'photo', best guess from the URL
    url: str = ""
    original: str = ""
    hops: list[str] = field(default_factory=list)

    @property
    def permalink(self) -> str:
        if self.url:
            return self.url
        if self.author:
            section = "photo" if self.kind == "photo" else "video"
            return f"https://www.tiktok.com/@{self.author}/{section}/{self.aweme_id}"
        return f"https://www.tiktok.com/@/video/{self.aweme_id}"


def parse_reference(text: str) -> Ref | None:
    """Parse without touching the network. Returns None if a redirect is needed."""
    text = (text or "").strip().strip("<>\"'")
    if not text:
        raise ResolveError("empty reference")
    if _ID_RE.match(text):
        if len(text) < 17:
            raise ResolveError(f"'{text}' does not look like a TikTok id (expected 19 digits)")
        return Ref(aweme_id=text, original=text)

    raw = text if "://" in text else "https://" + text.lstrip("/")
    parts = urllib.parse.urlsplit(raw)
    host = parts.netloc.lower().split(":")[0]
    if host.startswith("www."):
        bare_host = host[4:]
    else:
        bare_host = host
    if bare_host not in {h.replace("www.", "") for h in TIKTOK_HOSTS}:
        raise ResolveError(f"not a TikTok link: {host or raw}")

    m = _PATH_ID_RE.search(parts.path) or _QUERY_ID_RE.search("?" + parts.query)
    author_m = _AUTHOR_RE.search(parts.path)
    kind = "photo" if "/photo/" in parts.path else "video"
    canonical = urllib.parse.urlunsplit(("https", "www.tiktok.com" if bare_host not in SHORT_HOSTS else host,
                                         parts.path, "", ""))
    if m:
        return Ref(aweme_id=m.group(1), author=author_m.group(1) if author_m else "",
                   kind=kind, url=canonical if author_m else "", original=text)
    return None  # short link — needs a network round trip


def resolve(session: Session, text: str, *, max_hops: int = 6) -> Ref:
    """Parse ``text``, following short links until an id appears."""
    ref = parse_reference(text)
    if ref is not None:
        return ref

    current = text if "://" in text else "https://" + text.lstrip("/")
    hops: list[str] = []
    for _ in range(max_hops):
        hops.append(current)
        try:
            resp = session.get(
                current,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        except DownloadError as exc:
            raise ResolveError(f"could not follow short link {current}: {exc}") from None
        try:
            final = resp.raw.geturl()
        finally:
            resp.close()
        if final and final != current:
            current = final
        ref = parse_reference(current)
        if ref is not None:
            ref.original = text
            ref.hops = hops
            if not ref.url:
                ref.url = current.split("?")[0]
            return ref
        if not _SHORT_PATH_RE.match(urllib.parse.urlsplit(current).path or ""):
            break
        hops[-1] = current
    raise ResolveError(
        "could not find a video id in that link"
        + (f" (ended at {current})" if hops else "")
        + " — paste the full video URL instead of a short one"
    )
