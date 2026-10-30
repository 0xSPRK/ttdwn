"""Tiny HTTP layer over urllib: cookie jar, proxy, retries, streaming.

urllib is not fast, but it is always there. Every request in ttdwn goes
through :class:`Session` so that cookies obtained while resolving a page are
reused when the CDN link is opened a moment later — TikTok's web video hosts
reject requests from a cold jar.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import io
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Iterator, Mapping

from . import __version__
from .errors import DownloadError, TtdwnError

__all__ = ["Session", "Response"]

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class Response:
    """A thin wrapper that keeps the underlying stream open for callers."""

    def __init__(self, raw, url: str) -> None:
        self.raw = raw
        self.url = url
        self.status: int = raw.status
        self.headers = raw.headers

    @property
    def content_length(self) -> int | None:
        value = self.headers.get("Content-Length")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, n: int = -1) -> bytes:
        return self.raw.read(n)

    def iter_chunks(self, chunk: int = 256 * 1024) -> Iterator[bytes]:
        while True:
            block = self.raw.read(chunk)
            if not block:
                break
            yield block

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:
            pass

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Session:
    """Cookie-persistent, proxy-aware, retrying HTTP client."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        proxy: str | None = None,
        cookie_file: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.jar = http.cookiejar.CookieJar()
        if cookie_file:
            self._load_cookies(cookie_file)
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        ]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler())  # honour env vars
        self.opener = urllib.request.build_opener(*handlers)
        self.user_agent = user_agent or f"ttdwn/{__version__}"
        self.last_error: str | None = None

    # -- cookies -----------------------------------------------------
    def _load_cookies(self, path: str) -> None:
        try:
            self.jar.load(path, ignore_discard=True, ignore_expires=True)
        except FileNotFoundError:
            raise TtdwnError(f"cookie file not found: {path}") from None
        except Exception as exc:  # pragma: no cover - platform specific
            raise TtdwnError(f"could not read cookie file {path}: {exc}") from None

    def save_cookies(self, path: str) -> None:
        self.jar.save(path, ignore_discard=True, ignore_expires=True)

    # -- core --------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        allow_status: tuple[int, ...] = (200,),
    ) -> Response:
        """Perform a request, retrying transient failures.

        Raises :class:`DownloadError` on non-2xx after the last attempt.
        """
        attempts = (self.retries if retries is None else retries) + 1
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            hdrs.update({k: v for k, v in headers.items() if v is not None})
        last: Exception | None = None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                raw = self.opener.open(req, timeout=timeout or self.timeout)
                resp = Response(raw, url)
                if resp.status in allow_status or 200 <= resp.status < 300:
                    return resp
                resp.close()
                if resp.status in RETRY_STATUS and attempt + 1 < attempts:
                    last = DownloadError(f"HTTP {resp.status} for {_short(url)}")
                else:
                    raise DownloadError(f"HTTP {resp.status} for {_short(url)}")
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUS and attempt + 1 < attempts:
                    last = DownloadError(f"HTTP {exc.code} for {_short(url)}")
                elif exc.code in allow_status:
                    return Response(exc, url)  # type: ignore[arg-type]
                else:
                    raise DownloadError(f"HTTP {exc.code} for {_short(url)}" + _hint(exc.code)) from None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                last = DownloadError(f"{type(reason).__name__}: {reason} ({_short(url)})")
                if attempt + 1 >= attempts:
                    raise last from None
            if attempt + 1 < attempts:
                time.sleep(_backoff(attempt))
        raise last or DownloadError(f"request failed: {_short(url)}")

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def get_bytes(self, url: str, *, max_bytes: int = 8 * 1024 * 1024, **kwargs: Any) -> bytes:
        kwargs.setdefault("headers", {})
        kwargs["headers"] = {"Accept-Encoding": "gzip, deflate", **kwargs["headers"]}
        with self.get(url, **kwargs) as resp:
            data = _decompress(resp.raw, resp.header("Content-Encoding"))
            if len(data) > max_bytes:
                data = data[:max_bytes]
            return data

    def get_json(self, url: str, **kwargs: Any) -> Any:
        data = self.get_bytes(url, **kwargs)
        if not data.strip():
            raise DownloadError(f"empty response from {_short(url)}")
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            preview = data[:120].decode("utf-8", "replace").replace("\n", " ")
            raise DownloadError(f"non-JSON response from {_short(url)}: {preview!r}") from None

    def stream(self, url: str, *, headers: Mapping[str, str] | None = None,
               offset: int = 0, retries: int | None = None) -> Response:
        """Open a media URL. Supports byte offsets for resume."""
        hdrs = dict(headers or {})
        if offset:
            hdrs["Range"] = f"bytes={offset}-"
        return self.request("GET", url, headers=hdrs, retries=0 if retries is None else retries,
                            allow_status=(200, 206))

    # -- helpers -----------------------------------------------------
    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)


def _decompress(raw, encoding: str | None) -> bytes:
    """urllib never decompresses for us, so do it by hand."""
    encoding = (encoding or "").lower()
    data = _read_all(raw)
    if encoding == "gzip":
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    if encoding == "deflate":
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(data, wbits)
            except zlib.error:
                continue
        return data
    return data


def _read_all(raw) -> bytes:
    buf = io.BytesIO()
    while True:
        block = raw.read(65536)
        if not block:
            return buf.getvalue()
        buf.write(block)


def _backoff(attempt: int) -> float:
    return min(8.0, (0.6 * (2 ** attempt)) + random.uniform(0, 0.35))


def _short(url: str, limit: int = 90) -> str:
    host = urllib.parse.urlsplit(url).netloc or url
    return host if len(host) <= limit else host[: limit - 1] + "…"


def _hint(status: int) -> str:
    if status in (401, 403):
        return " — the signed link expired or the request was refused; try again or pass --cookies"
    if status == 404:
        return " — the resource is gone"
    if status == 429:
        return " — rate limited; slow down (--sleep) or use --proxy"
    return ""
