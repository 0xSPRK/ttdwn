"""Downloading: atomic rename, resume, retries, expired links."""

from __future__ import annotations

from pathlib import Path

import pytest

from ttdwn.download import Downloader, safe_filename
from ttdwn.errors import DownloadError, URLExpired
from ttdwn.http import Session
from ttdwn.models import Format

BODY = bytes(range(256)) * 4096  # 1 MiB of deterministic bytes


def fmt_for(url: str, size: int | None = None) -> Format:
    return Format(width=576, height=1024, size=size, gear="test", urls=[url])


def test_download_writes_atomically(tmp_path: Path, media_server):
    server = media_server(BODY)
    dest = tmp_path / "video.mp4"
    result = Downloader(Session(retries=1), retries=1, quiet=True).download(fmt_for(server.url), dest)
    assert result.path == dest
    assert dest.read_bytes() == BODY
    assert not (tmp_path / "video.mp4.part").exists()


def test_existing_file_is_not_clobbered(tmp_path: Path, media_server):
    server = media_server(BODY)
    dest = tmp_path / "video.mp4"
    dest.write_bytes(b"keep me")
    with pytest.raises(DownloadError):
        Downloader(Session(retries=1), retries=1, quiet=True).download(fmt_for(server.url), dest)
    assert dest.read_bytes() == b"keep me"
    # --force is the way out
    Downloader(Session(retries=1), retries=1, quiet=True).download(
        fmt_for(server.url), dest, overwrite=True)
    assert dest.read_bytes() == BODY


def test_resume_uses_a_range_request(tmp_path: Path, media_server):
    server = media_server(BODY)
    dest = tmp_path / "video.mp4"
    part = tmp_path / "video.mp4.part"
    part.write_bytes(BODY[: 300_000])
    result = Downloader(Session(retries=1), retries=1, quiet=True).download(
        fmt_for(server.url, size=len(BODY)), dest)
    assert result.resumed is True
    assert dest.read_bytes() == BODY
    assert any("range" in r for r in server.requests), "expected a Range header"


def test_server_ignoring_range_still_produces_a_good_file(tmp_path: Path, media_server):
    server = media_server(BODY, ignore_range=True)
    dest = tmp_path / "video.mp4"
    (tmp_path / "video.mp4.part").write_bytes(b"stale")
    Downloader(Session(retries=1), retries=1, quiet=True).download(fmt_for(server.url), dest)
    assert dest.read_bytes() == BODY


def test_403_is_reported_as_expired_and_can_be_refreshed(tmp_path: Path, media_server):
    good = media_server(BODY)
    dead = media_server(BODY, fail_first=99)
    dest = tmp_path / "video.mp4"
    calls = {"n": 0}

    def refresh() -> Format:
        calls["n"] += 1
        return fmt_for(good.url)

    result = Downloader(Session(retries=1), retries=2, quiet=True).download(
        fmt_for(dead.url), dest, refresh=refresh)
    assert calls["n"] == 1
    assert result.path.read_bytes() == BODY


def test_expired_link_without_refresh_fails(tmp_path: Path, media_server):
    dead = media_server(BODY, fail_first=99)
    with pytest.raises((URLExpired, DownloadError)):
        Downloader(Session(retries=1), retries=1, quiet=True).download(
            fmt_for(dead.url), tmp_path / "v.mp4")


def test_transient_403_then_success(tmp_path: Path, media_server):
    server = media_server(BODY, fail_first=1)
    result = Downloader(Session(retries=1), retries=3, quiet=True).download(
        fmt_for(server.url), tmp_path / "v.mp4")
    assert result.path.read_bytes() == BODY


def test_promo_second_url_is_used_when_the_first_fails(tmp_path: Path, media_server):
    dead = media_server(BODY, fail_first=99)
    good = media_server(BODY)
    fmt = Format(width=576, height=1024, gear="x", urls=[dead.url, good.url])
    result = Downloader(Session(retries=1), retries=1, quiet=True).download(fmt, tmp_path / "v.mp4")
    assert result.path.read_bytes() == BODY


def test_size_limit_aborts(tmp_path: Path, media_server):
    server = media_server(BODY)
    with pytest.raises(DownloadError):
        Downloader(Session(retries=1), retries=1, quiet=True).download(
            fmt_for(server.url), tmp_path / "v.mp4", max_bytes=1024)


@pytest.mark.parametrize("raw,expected", [
    ("a/b:c", "a_b_c"),
    ("  spaced  name  ", "spaced name"),
    ("CON", "_CON"),
    ("x" * 300, None),  # only length matters
    ("", "tiktok"),
])
def test_safe_filename(raw: str, expected: str | None):
    out = safe_filename(raw)
    assert "/" not in out and ":" not in out
    if expected is not None:
        assert out == expected
    else:
        assert len(out) <= 120
