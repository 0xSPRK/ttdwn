"""Get media information out of TikTok.

Three doors, tried in this order, results merged:

1. **mobile feed API** — ``/aweme/v1/feed/?aweme_id=…`` called with an Android
   app User-Agent. Fast, needs no signature, and every rendition it returns is
   watermark-free (``video.bit_rate[]``). The catch: its ladder usually stops
   at 720p.
2. **web video page** — ``/@author/video/<id>`` plus the JSON island
   ``__UNIVERSAL_DATA_FOR_REHYDRATION__``. This is where the 1080p gear lives
   (``video.bitrateInfo[]``). Requires the author handle, and the CDN links it
   hands out only work with the cookies from the same page load — which is why
   everything shares one :class:`~ttdwn.http.Session`.
3. **embed player** — ``/embed/v2/<id>``. Only a single stream, but it works
   from a bare id and tells us the author, which lets us retry step 2.

``video.download_addr`` is the watermarked copy; it is kept aside and never
picked unless the user explicitly asks for a watermark.
"""

from __future__ import annotations

import json
import random
import re
import string
from typing import Any, Sequence

from .errors import ExtractionError
from .http import Session
from .models import Format, Media
from .resolve import BROWSER_UA, Ref, resolve

__all__ = ["Extractor", "UA_ANDROID", "DEVICES", "API_HOSTS"]

# Android app UA: package/version, then a device that actually exists.
UA_ANDROID = (
    "com.zhiliaoapp.musically/2021905030 (Linux; U; Android 13; en_US; "
    "Pixel 6a; Build/TQ3A.230805.001; Cronet/58.0.2991.0)"
)

DEVICES: dict[str, dict[str, str]] = {
    "pixel6a": {
        "device_type": "Pixel 6a", "device_brand": "google", "device_model": "Pixel 6a",
        "os_version": "13", "os_api": "33", "resolution": "1080*2400", "dpi": "420",
    },
    "pixel7": {
        "device_type": "Pixel 7", "device_brand": "google", "device_model": "Pixel 7",
        "os_version": "14", "os_api": "34", "resolution": "1080*2400", "dpi": "420",
    },
    "s23": {
        "device_type": "SM-S911B", "device_brand": "samsung", "device_model": "SM-S911B",
        "os_version": "13", "os_api": "33", "resolution": "1080*2340", "dpi": "450",
    },
    "oneplus": {
        "device_type": "CPH2451", "device_brand": "OnePlus", "device_model": "CPH2451",
        "os_version": "13", "os_api": "33", "resolution": "1080*2412", "dpi": "420",
    },
}

# Ordered best-first from the sandbox probes: the useast1a edge answers 429
# far more often than the h2 gateway.
API_HOSTS = (
    "api-h2.tiktokv.com",
    "api16-normal-c-alisg.tiktokv.com",
    "api22-normal-c-alisg.tiktokv.com",
    "api16-normal-c-useast1a.tiktokv.com",
    "api16-normal-useast5.us.tiktokv.com",
)

MOBILE_APP = {
    "version_code": "190500",
    "version_name": "19.5.0",
    "manifest_version_code": "190500",
    "update_version_code": "190500",
    "build_number": "19.5.0",
    "app_name": "musically_go",
    "aid": "1340",
    "channel": "googleplay",
    "device_platform": "android",
    "language": "en",
    "region": "US",
    "sys_region": "US",
    "carrier_region": "US",
    "timezone_name": "America/New_York",
    "timezone_offset": "-14400",
    "locale": "en",
    "ac": "wifi",
    "app_type": "normal",
    "op_region": "US",
    "pass-region": "1",
}

WEB_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class Extractor:
    """Fetch and normalise post metadata."""

    def __init__(
        self,
        session: Session | None = None,
        *,
        device: str = "pixel6a",
        user_agent: str | None = None,
        use_mobile: bool = True,
        use_web: bool = True,
        hosts: Sequence[str] = API_HOSTS,
        verbose: Any = None,
    ) -> None:
        self.session = session or Session(user_agent=UA_ANDROID)
        self.device = DEVICES.get(device.lower()) or DEVICES["pixel6a"]
        self.user_agent = user_agent or UA_ANDROID
        self.use_mobile = use_mobile
        self.use_web = use_web
        self.hosts = tuple(hosts)
        self.verbose = verbose or (lambda *a, **k: None)
        self._openudid = "".join(random.choices(string.hexdigits.lower()[:16], k=16))

    # -- public ------------------------------------------------------
    def extract(self, ref: Ref | str) -> Media:
        if isinstance(ref, str):
            ref = resolve(self.session, ref)

        media: Media | None = None
        notes: list[str] = []

        if self.use_web and ref.author:
            media = self._attempt("web", ref.aweme_id, ref.author, notes)

        if self.use_mobile:
            mobile = self._attempt("mobile", ref.aweme_id, ref.author, notes)
            media = _merge(media, mobile) if media else mobile

        if media is None:
            media = self._attempt("embed", ref.aweme_id, ref.author, notes)
            if media is not None and self.use_web and not media.formats:
                media = _merge(media, self._attempt("web", ref.aweme_id, media.author, notes))
            elif media is not None and self.use_web and media.author and ref.author != media.author:
                # embed told us who the author is: retry the page for 1080p
                media = _merge(media, self._attempt("web", ref.aweme_id, media.author, notes))

        if media is None:
            detail = ("\n  " + "\n  ".join(notes)) if notes else ""
            raise ExtractionError(f"could not extract {ref.aweme_id}: no source returned media{detail}")

        media.id = media.id or ref.aweme_id
        _drop_duplicate_streams(media)
        if ref.author and not media.author:
            media.author = ref.author
        if not media.url:
            media.url = f"https://www.tiktok.com/@{media.author}/video/{media.id}" if media.author else ""
        media.sort_formats()
        if not media.formats and not media.image_urls:
            raise ExtractionError(
                f"post {ref.aweme_id} has no downloadable media "
                "(private, deleted, region-locked, or a live stream)"
            )
        return media

    # -- sources -----------------------------------------------------
    def _attempt(self, source: str, aweme_id: str, author: str, notes: list[str]) -> Media | None:
        handler = {"web": self._web, "mobile": self._mobile, "embed": self._embed}[source]
        try:
            if source == "web":
                return handler(aweme_id, author)  # type: ignore[operator]
            return handler(aweme_id)  # type: ignore[operator]
        except Exception as exc:  # one dead source must not kill the run
            notes.append(f"{source}: {exc}")
            self.verbose(f"{source} failed: {exc}")
            return None

    # -- mobile ------------------------------------------------------
    def _mobile(self, aweme_id: str) -> Media | None:
        params = dict(MOBILE_APP)
        params.update(self.device)
        params.update({
            "aweme_id": aweme_id,
            "iid": self._iid,
            "openudid": self._openudid,
            "uuid": self._openudid,
            "device_id": self._device_id,
        })
        query = "&".join(f"{k}={_quote(str(v))}" for k, v in params.items())
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "sdk-version": "2",
            "x-tt-store-region": "us",
            "x-tt-store-region-src": "did",
        }
        errors: list[str] = []
        for host in self.hosts:
            url = f"https://{host}/aweme/v1/feed/?{query}"
            try:
                data = self.session.get_json(url, headers=headers, max_bytes=12 * 1024 * 1024)
            except Exception as exc:
                errors.append(f"{host}: {exc}")
                continue
            items = _as_list(data.get("aweme_list") if isinstance(data, dict) else None)
            if not items:
                errors.append(f"{host}: empty aweme_list")
                continue
            item = items[0]
            if str(item.get("aweme_id")) != str(aweme_id):
                other = _as_list(data.get("aweme_list"))
                match = [i for i in other if str(i.get("aweme_id")) == str(aweme_id)]
                if not match:
                    errors.append(f"{host}: id mismatch")
                    continue
                item = match[0]
            media = _from_mobile(item)
            if media is not None:
                return media
            errors.append(f"{host}: unparsable item")
        raise ExtractionError("; ".join(errors) or "mobile API unreachable")

    @property
    def _iid(self) -> str:
        if not hasattr(self, "_iid_cache"):
            self._iid_cache = "7" + "".join(random.choices(string.digits, k=18))
        return self._iid_cache

    @property
    def _device_id(self) -> str:
        if not hasattr(self, "_did_cache"):
            self._did_cache = "".join(random.choices(string.digits, k=19))
        return self._did_cache

    # -- web ---------------------------------------------------------
    def _web(self, aweme_id: str, author: str = "") -> Media | None:
        if not author:
            return None
        for section in ("video", "photo"):
            url = f"https://www.tiktok.com/@{author}/{section}/{aweme_id}"
            html = self.session.get_bytes(
                url, headers=WEB_HEADERS, max_bytes=24 * 1024 * 1024
            ).decode("utf-8", "replace")
            item = _parse_web_item(html)
            if item:
                media = _from_web(item)
                if media is not None:
                    media.url = url
                    if section == "photo" or media.image_urls:
                        media.url = url
                    return media
            if '/404' in html[:2000] and section == "video":
                continue  # maybe a photo post
        return None

    # -- embed -------------------------------------------------------
    def _embed(self, aweme_id: str) -> Media | None:
        url = f"https://www.tiktok.com/embed/v2/{aweme_id}"
        html = self.session.get_bytes(url, headers=WEB_HEADERS, max_bytes=16 * 1024 * 1024)
        text = html.decode("utf-8", "replace")
        data = _parse_json_island(text, "__FRONTITY_CONNECT_STATE__")
        if not data:
            return None
        video_data = _find_first(data, lambda d: isinstance(d.get("itemInfos"), dict))
        if not video_data:
            return None
        info = video_data["itemInfos"]
        media = Media(id=str(info.get("id") or aweme_id))
        media.desc = info.get("text") or ""
        author_info = video_data.get("authorInfos") or {}
        media.author = author_info.get("uniqueId") or author_info.get("unique_id") or ""
        media.nickname = author_info.get("nickname") or ""
        media.author_id = str(author_info.get("userId") or author_info.get("id") or "")
        media.created = _to_int(info.get("createTime")) or 0
        media.cover_urls = _as_list(info.get("covers"))
        video = info.get("video") or {}
        meta = video.get("videoMeta") or {}
        fmt = Format(
            width=_to_int(meta.get("width")),
            height=_to_int(meta.get("height")),
            size=_to_int(meta.get("size")),
            gear="embed",
        )
        for u in _as_list(video.get("urls")):
            fmt.add_url(str(u), "embed")
        media.duration_ms = int(float(meta.get("duration") or 0) * 1000) or 0
        if fmt.urls:
            media.formats.append(fmt)
        media.sources.append("embed")
        return media


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def _from_mobile(item: dict) -> Media | None:
    aweme_id = str(item.get("aweme_id") or item.get("awemeId") or "")
    if not aweme_id:
        return None
    media = Media(id=aweme_id)
    media.desc = item.get("desc") or ""
    author = item.get("author") or {}
    media.author = author.get("unique_id") or author.get("uniqueId") or ""
    media.nickname = author.get("nickname") or ""
    media.author_id = str(author.get("uid") or author.get("sec_uid") or "")
    media.created = _to_int(item.get("create_time")) or 0
    video = item.get("video") or {}
    media.duration_ms = _to_int(video.get("duration")) or _to_int(item.get("duration")) or 0
    media.cover_urls = _urls(video.get("cover")) or _urls(video.get("origin_cover"))
    media.stats = _stats(item.get("statistics") or item.get("stats") or {})
    media.music = _music(item.get("music"))

    # photo / carousel posts
    image_post = item.get("image_post_info") or item.get("imagePostInfo") or {}
    for image in _as_list(image_post.get("images")):
        urls = _urls(image.get("display_image")) or _urls(image.get("thumbnail"))
        if urls:
            media.image_urls.append(urls[0])
    if media.image_urls and not video.get("bit_rate"):
        media.sources.append("mobile")

    seen: dict[tuple, Format] = {}

    def add(width, height, bitrate, gear, size, urls, codec="h264", watermarked=False):
        key = (width, height, gear, watermarked)
        fmt = seen.get(key)
        if fmt is None:
            fmt = Format(width=_to_int(width), height=_to_int(height), bitrate=_to_int(bitrate),
                         gear=gear or "", codec=codec, size=_to_int(size), watermarked=watermarked)
            seen[key] = fmt
        for u in urls:
            fmt.add_url(u, "mobile")

    ladder = _as_list(video.get("bit_rate")) + _as_list(video.get("bitrate_info"))
    for entry in ladder:
        addr = entry.get("play_addr") or entry.get("playAddr") or {}
        gear = entry.get("gear_name") or entry.get("gearName") or ""
        # is_bytevc1 == 1 means the stream is HEVC, whatever the gear name says
        bytevc1 = entry.get("is_bytevc1", entry.get("isBytevc1"))
        is_h265 = str(bytevc1) in ("1", "True", "true") or "bytevc1" in gear or "265" in gear
        add(addr.get("width") or entry.get("width"), addr.get("height") or entry.get("height"),
            entry.get("bit_rate") or entry.get("bitrate"), gear,
            addr.get("data_size") or addr.get("dataSize"), _urls(addr),
            codec="h265" if is_h265 else "h264")

    default_codec = "h265" if str(video.get("is_bytevc1")) in ("1", "True", "true") else "h264"
    for name, codec in (("play_addr", default_codec), ("play_addr_h264", "h264"), ("play_addr_265", "h265")):
        addr = video.get(name)
        if isinstance(addr, dict) and _urls(addr):
            add(addr.get("width") or video.get("width"), addr.get("height") or video.get("height"),
                None, name, addr.get("data_size"), _urls(addr), codec=codec)

    # the only watermarked streams TikTok hands out
    for name in ("download_addr", "download_suffix_logo_addr"):
        addr = video.get(name)
        if isinstance(addr, dict) and _urls(addr):
            add(addr.get("width") or video.get("width"), addr.get("height") or video.get("height"),
                None, name, addr.get("data_size"), _urls(addr), watermarked=True)

    media.formats = list(seen.values())
    media.sources.append("mobile")
    return media


def _from_web(item: dict) -> Media | None:
    aweme_id = str(item.get("id") or item.get("awemeId") or "")
    if not aweme_id:
        return None
    media = Media(id=aweme_id)
    media.desc = item.get("desc") or ""
    author = item.get("author") or {}
    media.author = author.get("uniqueId") or author.get("unique_id") or ""
    media.nickname = author.get("nickname") or ""
    media.author_id = str(author.get("id") or author.get("secUid") or "")
    media.created = _to_int(item.get("createTime")) or 0
    video = item.get("video") or {}
    duration = video.get("duration")
    media.duration_ms = int(float(duration) * 1000) if duration else 0
    media.cover_urls = [u for u in (video.get("originCover"), video.get("cover"), video.get("dynamicCover")) if u]
    media.stats = _stats(item.get("statsV2") or item.get("stats") or {})
    media.music = _music(item.get("music"))

    image_post = item.get("imagePost") or item.get("imagePostInfo") or {}
    for image in _as_list(image_post.get("images")):
        urls = _urls({"url_list": image.get("imageURL", {}).get("urlList", [])}) or _urls(image.get("imageURL"))
        if urls:
            media.image_urls.append(urls[0])

    seen: dict[tuple, Format] = {}

    def add(width, height, bitrate, gear, size, urls, codec="h264", watermarked=False):
        key = (width, height, gear, watermarked)
        fmt = seen.get(key)
        if fmt is None:
            fmt = Format(width=_to_int(width), height=_to_int(height), bitrate=_to_int(bitrate),
                         gear=gear or "", codec=codec, size=_to_int(size), watermarked=watermarked)
            seen[key] = fmt
        for u in urls:
            fmt.add_url(u, "web")

    for entry in _as_list(video.get("bitrateInfo")) + _as_list(video.get("bitRateList")):
        addr = entry.get("PlayAddr") or entry.get("playAddr") or {}
        gear = entry.get("GearName") or entry.get("gearName") or ""
        urls = _as_list(addr.get("UrlList")) or _as_list(addr.get("urlList"))
        codec_type = str(entry.get("CodecType") or entry.get("codecType") or video.get("codecType") or "")
        add(addr.get("Width") or addr.get("width"), addr.get("Height") or addr.get("height"),
            entry.get("Bitrate") or entry.get("bitrate"), gear,
            addr.get("DataSize") or addr.get("dataSize"), [u for u in urls if u],
            codec="h265" if ("265" in codec_type or "hvc1" in codec_type or "hevc" in codec_type) else "h264")

    for name, codec in (("playAddr", "h264"), ("playAddr_h264", "h264"), ("playAddrH264", "h264")):
        addr = video.get(name)
        if isinstance(addr, dict) and _urls(addr):
            add(addr.get("width") or video.get("width"), addr.get("height") or video.get("height"),
                video.get("bitrate"), name, addr.get("dataSize"), _urls(addr), codec=codec)

    for name in ("downloadAddr", "download_addr"):
        addr = video.get(name)
        if isinstance(addr, dict) and _urls(addr):
            add(addr.get("width") or video.get("width"), addr.get("height") or video.get("height"),
                None, name, addr.get("dataSize"), _urls(addr), watermarked=True)
        elif isinstance(addr, str) and addr:
            add(video.get("width"), video.get("height"), None, name, None, [addr], watermarked=True)

    media.formats = list(seen.values())
    media.sources.append("web")
    return media


def _parse_web_item(html: str) -> dict | None:
    """Dig the post out of whichever JSON island the page ships."""
    for island in ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "__NEXT_DATA__", "SIGI_STATE"):
        data = _parse_json_island(html, island)
        if not data:
            continue
        item = None
        scope = data.get("__DEFAULT_SCOPE__") if isinstance(data, dict) else None
        if isinstance(scope, dict):
            detail = scope.get("webapp.video-detail") or {}
            if detail.get("statusCode") and str(detail.get("statusCode")) != "0":
                continue
            item = ((detail.get("itemInfo") or {}).get("itemStruct")) or None
            if item is None:
                item = _find_first(scope, lambda d: bool(d.get("id") and d.get("video") and d.get("author")))
        if item is None:
            item = _find_first(data, lambda d: bool(d.get("id") and isinstance(d.get("video"), dict) and d.get("desc") is not None))
        if isinstance(item, dict) and item.get("video"):
            return item
    return None


def _parse_json_island(html: str, element_id: str) -> dict | None:
    match = re.search(
        r'<script[^>]*id="%s"[^>]*>(.*?)</script>' % re.escape(element_id), html, re.S
    )
    if not match:
        match = re.search(r'"%s"\s*:\s*(\{.*?\})\s*[,}]' % re.escape(element_id), html, re.S)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_unescape_js(raw))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _unescape_js(raw: str) -> str:
    return (raw.replace("\\u002F", "/").replace("\\/", "/")
               .replace("\\u0026", "&").replace("\\u003C", "<").replace("\\u003E", ">"))


def _find_first(node: Any, predicate, _depth: int = 0) -> dict | None:
    """Depth-first search for the first dict satisfying ``predicate``."""
    if _depth > 24:
        return None
    if isinstance(node, dict):
        if predicate(node):
            return node
        for value in node.values():
            found = _find_first(value, predicate, _depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_first(value, predicate, _depth + 1)
            if found is not None:
                return found
    return None


def _urls(addr: Any) -> list[str]:
    if isinstance(addr, str):
        return [addr] if addr else []
    if not isinstance(addr, dict):
        return []
    for key in ("url_list", "urlList", "UrlList", "urls"):
        value = addr.get(key)
        if isinstance(value, list):
            return [str(u) for u in value if isinstance(u, str) and u]
    return []


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _stats(raw: dict) -> dict:
    mapping = {
        "play_count": ("playCount", "play_count"),
        "like_count": ("diggCount", "digg_count"),
        "comment_count": ("commentCount", "comment_count"),
        "share_count": ("shareCount", "share_count"),
        "save_count": ("collectCount", "collect_count"),
    }
    out: dict[str, int] = {}
    for target, keys in mapping.items():
        for key in keys:
            value = _to_int(raw.get(key))
            if value is not None:
                out[target] = value
                break
    return out


def _music(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    urls = _urls(raw.get("play_url")) or _urls(raw) or ([raw["playUrl"]] if raw.get("playUrl") else [])
    return {
        "title": raw.get("title") or "",
        "author": raw.get("author") or raw.get("authorName") or "",
        "url": urls[0] if urls else "",
        "duration": _to_int(raw.get("duration")) or 0,
    }


def _merge(base: Media | None, extra: Media | None) -> Media | None:
    """Combine two extractions of the same post, keeping every URL."""
    if base is None:
        return extra
    if extra is None:
        return base
    if str(base.id) != str(extra.id):
        return base
    for fmt in extra.formats:
        match = next(
            (f for f in base.formats
             if f.gear == fmt.gear or (f.quality == fmt.quality and f.watermarked == fmt.watermarked
                                       and (f.bitrate or 0) == (fmt.bitrate or 0))),
            None,
        )
        if match is None:
            base.formats.append(fmt)
        else:
            for u in fmt.urls:
                match.add_url(u, fmt.sources[0] if fmt.sources else "other")
            match.bitrate = match.bitrate or fmt.bitrate
            match.size = match.size or fmt.size
            match.width = match.width or fmt.width
            match.height = match.height or fmt.height
            if fmt.watermarked:
                match.watermarked = True
    base.author = base.author or extra.author
    base.nickname = base.nickname or extra.nickname
    base.author_id = base.author_id or extra.author_id
    base.desc = base.desc or extra.desc
    base.created = base.created or extra.created
    base.duration_ms = base.duration_ms or extra.duration_ms
    if not base.cover_urls:
        base.cover_urls = extra.cover_urls
    if not base.image_urls:
        base.image_urls = extra.image_urls
    if not base.stats:
        base.stats = extra.stats
    if not base.music:
        base.music = extra.music
    base.url = base.url or extra.url
    for source in extra.sources:
        if source not in base.sources:
            base.sources.append(source)
    return base


def _drop_duplicate_streams(media: Media) -> None:
    """Different API fields often point at one and the same file.

    ``play_addr_h264`` is literally ``bit_rate[normal_540_0]``; the mobile and
    web ladders repeat each other at 540p. Signed URLs differ between sources
    (other CDN, other signature) even when the bytes are identical, so compare
    by shape *and* byte size, not by URL. Keep the richest entry per file so
    the quality table shows renditions, not aliases.
    """
    def richness(fmt: Format) -> tuple:
        return (1 if fmt.bitrate else 0, 1 if fmt.size else 0, len(fmt.sources))

    # pass 1: same frame, same byte count -> same file
    buckets: dict[tuple, Format] = {}
    for fmt in sorted(media.formats, key=richness, reverse=True):
        key = (fmt.width, fmt.height, fmt.codec, fmt.size if fmt.size else id(fmt))
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = fmt
            continue
        for url in fmt.urls:
            existing.add_url(url, fmt.sources[0] if fmt.sources else "other")
        for source in fmt.sources:
            if source not in existing.sources:
                existing.sources.append(source)
        existing.watermarked = existing.watermarked or fmt.watermarked
    candidates = list(buckets.values())

    # pass 2: one URL set contained in another -> the smaller entry is an alias
    kept: list[Format] = []
    for fmt in sorted(candidates, key=richness, reverse=True):
        urls = set(fmt.urls)
        if not urls:
            continue
        for other in kept:
            if urls <= set(other.urls):
                for source in fmt.sources:
                    if source not in other.sources:
                        other.sources.append(source)
                break
        else:
            kept.append(fmt)
    media.formats = kept


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="*-._")
