"""Save bytes to disk: resumable, atomic, honest about progress.

A download writes to ``<name>.part`` and is renamed only after the byte count
is verified, so an interrupted run never leaves a truncated ``.mp4`` behind.
When a signed CDN link ages out mid-transfer the caller can hand in a fresh
one (``refresh``) and the transfer continues from the last byte.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import DownloadError, URLExpired
from .http import Session
from .models import Format
from .ui import Progress, format_bytes

__all__ = ["Downloader", "DownloadResult", "safe_filename"]

CHUNK = 256 * 1024
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CDN_HEADERS = {
    "User-Agent": UA_BROWSER,
    "Referer": "https://www.tiktok.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_ILLEGAL = '<>:"/\\|?*\0'
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, *, limit: int = 120) -> str:
    """Make a name that survives Windows, macOS and Linux."""
    cleaned = "".join("_" if ch in _ILLEGAL or ord(ch) < 32 else ch for ch in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > limit:
        root, dot, ext = cleaned.rpartition(".")
        cleaned = (root[: limit - len(ext) - 1] + "." + ext) if dot and len(ext) <= 5 else cleaned[:limit]
    return cleaned or "tiktok"


@dataclass
class DownloadResult:
    path: Path
    size: int
    seconds: float
    resumed: bool = False
    url_source: str = ""

    @property
    def speed(self) -> float:
        return self.size / self.seconds if self.seconds > 0 else 0.0


class Downloader:
    """Download one rendition, retrying and resuming as needed."""

    def __init__(
        self,
        session: Session,
        *,
        retries: int = 3,
        chunk: int = CHUNK,
        quiet: bool = False,
        progress_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.session = session
        self.retries = max(1, retries)
        self.chunk = chunk
        self.quiet = quiet
        self.progress_factory = progress_factory or Progress

    # -- public ------------------------------------------------------
    def download(
        self,
        fmt: Format,
        dest: Path,
        *,
        resume: bool = True,
        overwrite: bool = False,
        max_bytes: int | None = None,
        refresh: Callable[[], Format] | None = None,
        label: str = "",
        extra_headers: dict[str, str] | None = None,
        urls: list[str] | None = None,
    ) -> DownloadResult:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        if dest.exists() and not overwrite:
            raise DownloadError(f"{dest} already exists — pass --force to overwrite")
        if part.exists() and not resume:
            part.unlink()

        candidates = list(urls or []) or list(fmt.urls)
        if not candidates:
            raise DownloadError("no URL to download from")

        started = time.monotonic()
        resumed = part.exists() and part.stat().st_size > 0
        current = fmt
        declared: int | None = None
        attempts = 0
        last_error: Exception | None = None

        while attempts < self.retries:
            attempts += 1
            try:
                declared = self._transfer(
                    candidates, part, label=label, quiet=self.quiet,
                    extra_headers=extra_headers, max_bytes=max_bytes, hint=fmt.size,
                )
                last_error = None
                break
            except URLExpired as exc:
                last_error = exc
                if attempts >= self.retries:
                    break
                if refresh is not None:
                    current = refresh()
                    candidates = list(current.urls)
                else:
                    # every mirror refused: give it a moment, it is often throttling
                    time.sleep(min(6.0, 1.5 * attempts))
            except DownloadError as exc:
                last_error = exc
                if attempts >= self.retries:
                    break
                time.sleep(min(6.0, 1.2 * attempts))

        if last_error is not None:
            raise last_error

        size = part.stat().st_size
        # A declared length (Content-Length / Content-Range) is authoritative;
        # the size in post metadata is only a hint.
        if declared and size < declared:
            raise DownloadError(
                f"incomplete download: {format_bytes(size)} of {format_bytes(declared)}"
            )
        os.replace(part, dest)
        return DownloadResult(path=dest, size=size, seconds=time.monotonic() - started,
                              resumed=resumed, url_source=", ".join(current.sources))

    # -- internals ---------------------------------------------------
    def _transfer(
        self,
        candidates: list[str],
        part: Path,
        *,
        label: str,
        quiet: bool,
        extra_headers: dict[str, str] | None,
        max_bytes: int | None,
        hint: int | None,
    ) -> int | None:
        offset = part.stat().st_size if part.exists() else 0
        if hint and offset >= hint > 0:
            return offset  # nothing left to do
        headers = dict(CDN_HEADERS)
        if extra_headers:
            headers.update(extra_headers)

        errors: list[str] = []
        expired = 0
        for url in candidates:
            try:
                return self._stream_to(url, part, offset, hint, label=label, quiet=quiet,
                                       headers=headers, max_bytes=max_bytes)
            except URLExpired as exc:
                # try the next CDN mirror before giving up on the signature
                expired += 1
                errors.append(str(exc))
            except DownloadError as exc:
                errors.append(str(exc))
            offset = part.stat().st_size if part.exists() else 0
            if hint and offset >= hint:
                return offset
        message = "; ".join(dict.fromkeys(errors)) or "transfer failed"
        if expired and expired == len(candidates):
            raise URLExpired(message)
        raise DownloadError(message)

    def _stream_to(
        self,
        url: str,
        part: Path,
        offset: int,
        hint: int | None,
        *,
        label: str,
        quiet: bool,
        headers: dict[str, str],
        max_bytes: int | None,
    ) -> int | None:
        if offset:
            headers = {**headers, "Range": f"bytes={offset}-"}
        # 401/403 must reach us as a status, not as an exception: an expired
        # signature is recoverable by re-extracting the post.
        resp = self.session.request("GET", url, headers=headers, retries=0,
                                    allow_status=(200, 206, 401, 403, 416))
        try:
            status = resp.status
            if status in (401, 403):
                raise URLExpired(f"HTTP {status}: signed link expired")
            if status == 416:  # range past the end: the part is already whole
                if offset:
                    return offset
                raise DownloadError("HTTP 416: server rejected the range request")
            if status not in (200, 206):
                raise DownloadError(f"HTTP {status}")

            declared: int | None = None
            if status == 206:
                content_range = resp.header("Content-Range") or ""
                if "/" in content_range:
                    tail = content_range.rsplit("/", 1)[-1].strip()
                    declared = int(tail) if tail.isdigit() else None
                if offset == 0:
                    status = 200
                declared = declared or hint
            else:
                declared = resp.content_length or hint
                offset = 0

            mode = "ab" if status == 206 and offset else "wb"
            if mode == "wb":
                offset = 0
            written = offset
            progress = self.progress_factory(declared, label=label, enabled=not quiet)
            progress.done = offset
            if not quiet:
                progress.start()
            try:
                with open(part, mode) as fh:
                    for block in resp.iter_chunks(self.chunk):
                        fh.write(block)
                        written += len(block)
                        if not quiet:
                            progress.update(len(block))
                        if max_bytes and written > max_bytes:
                            raise DownloadError(
                                f"size limit exceeded ({format_bytes(max_bytes)})"
                            )
            finally:
                if not quiet:
                    progress.finish()
            if declared and written < declared:
                raise DownloadError(f"short transfer: {written} of {declared} bytes")
            return declared
        finally:
            resp.close()

    @staticmethod
    def free_space(path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return 0
