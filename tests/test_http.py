"""HTTP body limits and decompression."""

from __future__ import annotations

import gzip

import pytest

from ttdwn.errors import DownloadError
from ttdwn.http import Session, _decompress


def test_get_bytes_reads_a_body_within_the_limit(media_server):
    body = b"x" * 1024
    server = media_server(body)
    assert Session(retries=1).get_bytes(server.url, max_bytes=2048) == body


def test_get_bytes_refuses_a_body_over_the_limit(media_server):
    server = media_server(b"x" * 4096)
    with pytest.raises(DownloadError):
        Session(retries=1).get_bytes(server.url, max_bytes=1024)


def test_gzip_expansion_respects_the_limit():
    with pytest.raises(DownloadError):
        _decompress(gzip.compress(b"x" * 10_000), "gzip", 1024, "https://example.test/")


def test_gzip_body_within_the_limit_is_decoded():
    assert _decompress(gzip.compress(b"hello"), "gzip", 1024, "u") == b"hello"
