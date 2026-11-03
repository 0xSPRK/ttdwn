"""Optional ffprobe parsing."""

from __future__ import annotations

import json
from pathlib import Path

import ttdwn.ffmpeg as ffmpeg


class _Completed:
    returncode = 0

    def __init__(self, payload: dict) -> None:
        self.stdout = json.dumps(payload)


def test_probe_tolerates_na_metadata(monkeypatch):
    payload = {
        "format": {"duration": "N/A", "bit_rate": "N/A", "size": "N/A"},
        "streams": [],
    }
    monkeypatch.setattr(ffmpeg, "_binary", lambda name: "ffprobe")
    monkeypatch.setattr(ffmpeg.subprocess, "run", lambda *args, **kwargs: _Completed(payload))

    result = ffmpeg.probe(Path("missing.mp4"))
    assert result["duration"] is None
    assert result["bitrate"] is None
    assert result["size"] is None


def test_probe_reads_numeric_metadata(monkeypatch):
    payload = {
        "format": {"duration": "1.5", "bit_rate": "640712", "size": "1024"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280}],
    }
    monkeypatch.setattr(ffmpeg, "_binary", lambda name: "ffprobe")
    monkeypatch.setattr(ffmpeg.subprocess, "run", lambda *args, **kwargs: _Completed(payload))

    result = ffmpeg.probe(Path("missing.mp4"))
    assert result["duration"] == 1.5
    assert result["bitrate"] == 640712
    assert result["size"] == 1024
    assert result["codec"] == "h264"
