"""Optional ffmpeg extras: faststart remux and audio extraction.

Nothing here is required for a normal download — a watermark-free MP4 is
already playable everywhere. These are conveniences for people who want the
file in a particular shape.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import PostProcessError

__all__ = ["have_ffmpeg", "have_ffprobe", "remux", "extract_audio", "probe", "ffmpeg_version"]

AUDIO_ARGS = {
    "mp3": (["-vn", "-c:a", "libmp3lame", "-q:a", "2"], ".mp3"),
    "m4a": (["-vn", "-c:a", "aac", "-b:a", "192k"], ".m4a"),
    "opus": (["-vn", "-c:a", "libopus", "-b:a", "160k"], ".opus"),
    "wav": (["-vn", "-c:a", "pcm_s16le"], ".wav"),
}


def _binary(name: str) -> str | None:
    return shutil.which(name)


def have_ffmpeg() -> bool:
    return _binary("ffmpeg") is not None


def have_ffprobe() -> bool:
    return _binary("ffprobe") is not None


def ffmpeg_version() -> str:
    binary = _binary("ffmpeg")
    if not binary:
        return ""
    try:
        out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=10)
        return out.stdout.splitlines()[0] if out.stdout else ""
    except (OSError, subprocess.SubprocessError):
        return ""


GLOBAL_ARGS = ["-hide_banner", "-loglevel", "error", "-nostdin"]


def _run(args: list[str], *, timeout: int = 300) -> None:
    binary = _binary("ffmpeg")
    if not binary:
        raise PostProcessError("ffmpeg is not installed (or not on PATH)")
    try:
        # global flags must precede the input/output arguments
        proc = subprocess.run([binary, *GLOBAL_ARGS, *args],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PostProcessError(f"ffmpeg timed out after {timeout}s") from None
    except OSError as exc:
        raise PostProcessError(f"could not run ffmpeg: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise PostProcessError("ffmpeg failed: " + (detail[-1] if detail else f"exit {proc.returncode}"))


def remux(source: Path, dest: Path | None = None, *, overwrite: bool = False) -> Path:
    """Rewrite the container in place with the moov atom first (instant start)."""
    source = Path(source)
    dest = Path(dest) if dest else source.with_name(source.stem + ".fs.mp4")
    if dest.exists() and not overwrite:
        return dest
    # keep the extension: ffmpeg picks the muxer from it
    tmp = dest.with_name(dest.stem + ".tmp" + dest.suffix)
    _run(["-y" if overwrite else "-n", "-i", str(source), "-c", "copy", "-movflags", "+faststart", str(tmp)])
    if dest.exists() and overwrite:
        dest.unlink()
    tmp.replace(dest)
    return dest


def extract_audio(source: Path, dest: Path | None = None, *, fmt: str = "mp3",
                  overwrite: bool = False) -> Path:
    source = Path(source)
    if fmt not in AUDIO_ARGS:
        raise PostProcessError(f"unsupported audio format '{fmt}' (use {', '.join(AUDIO_ARGS)})")
    args, ext = AUDIO_ARGS[fmt]
    dest = Path(dest) if dest else source.with_suffix(ext)
    if dest.exists() and not overwrite:
        return dest
    _run(["-y", "-i", str(source), *args, str(dest)])
    return dest


def probe(path: Path) -> dict[str, Any]:
    """Return width/height/duration/codec, or an empty dict if ffprobe is absent."""
    binary = _binary("ffprobe")
    if not binary:
        return {}
    try:
        proc = subprocess.run(
            [binary, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "duration": float(fmt.get("duration") or 0) or None,
        "bitrate": int(fmt.get("bit_rate") or 0) or None,
        "size": int(fmt.get("size") or 0) or None,
    }
