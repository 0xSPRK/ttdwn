"""Command line interface for ttdwn.

Everything human-readable goes to stderr, the saved path (or ``--json``
payload) goes to stdout, so ``ttdwn URL | xargs mpv`` works.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import string
import sys
import time
from pathlib import Path
from typing import Sequence

from . import __app__, __version__
from .download import Downloader, safe_filename
from .errors import TtdwnError, UsageError
from .extract import API_HOSTS, DEVICES, UA_ANDROID, Extractor
from .ffmpeg import extract_audio, ffmpeg_version, have_ffmpeg, remux
from .formats import parse_quality, pick, ranked
from .http import Session
from .models import Format, Media
from .resolve import BROWSER_UA
from .ui import (
    C,
    banner,
    error,
    format_bytes,
    format_duration,
    hrule,
    info,
    ok,
    set_color,
    supports_color,
    table,
    term_width,
    truncate,
    warn,
)

DEFAULT_FILENAME = "{author}_{id}_{quality}"
UA_PRESETS = {
    "android": UA_ANDROID,
    "pixel": UA_ANDROID,
    "chrome": BROWSER_UA,
    "desktop": BROWSER_UA,
    "iphone": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
}
TOKENS = (
    "id", "author", "nickname", "desc", "quality", "resolution", "width", "height",
    "bitrate", "gear", "codec", "date", "duration", "index", "ext", "source",
)
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*(b|kb|kib|mb|mib|gb|gib)?\s*$", re.I)
_SIZE_UNITS = {"": 1, "b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2,
               "mib": 1024 ** 2, "gb": 1000 ** 3, "gib": 1024 ** 3}


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="ttdwn",
        formatter_class=formatter,
        description="Download TikTok videos without the watermark, at the best quality the post serves.",
        epilog=(
            "examples:\n"
            "  ttdwn https://www.tiktok.com/@user/video/7106594312292453675\n"
            "  ttdwn -q 720 vm.tiktok.com/ZMabc123/\n"
            "  ttdwn --list https://vm.tiktok.com/ZMabc123/\n"
            "  ttdwn -q best -o ~/Videos --filename '{author}-{id}-{resolution}' <url>\n"
            "  ttdwn --audio mp3 --json <url>\n"
            "  ttdwn -i links.txt --sleep 2 --max-quality 1080\n"
            "\n"
            "quality: best (default), worst, a height (144/360/540/720/1080/1440/2160), or an index from --list.\n"
            "filename tokens: " + ", ".join("{" + t + "}" for t in TOKENS) + "\n"
            "\n"
            "exit codes: 0 ok · 1 failure · 2 bad usage · 3 bad link · 4 no media · 5 download · 6 ffmpeg\n"
            "ttdwn downloads publicly available clips for personal use. Respect creators and the ToS."
        ),
    )
    parser.add_argument("urls", nargs="*", metavar="URL", help="post links or bare 19-digit ids")

    src = parser.add_argument_group("input")
    src.add_argument("-i", "--input", metavar="FILE", action="append", default=[],
                     help="read links from a file, one per line (repeatable, '-' for stdin)")
    src.add_argument("--cookies", metavar="FILE",
                     help="Netscape cookie file, for age/region restricted posts")

    out = parser.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="DIR", default=".", help="destination directory (default: .)")
    out.add_argument("-f", "--filename", metavar="TPL", default=DEFAULT_FILENAME,
                     help=f"filename template (default: {DEFAULT_FILENAME})")
    out.add_argument("--out-file", metavar="PATH", help="exact output path, for a single link")
    out.add_argument("--force", action="store_true", help="overwrite existing files")
    out.add_argument("--no-resume", action="store_true", help="discard partial downloads instead of resuming")
    out.add_argument("--max-size", metavar="SIZE", help="refuse files larger than SIZE, e.g. 200MB")

    qual = parser.add_argument_group("quality")
    qual.add_argument("-q", "--quality", metavar="SPEC", default="best",
                      help="best | worst | height (720, 1080p) | index from --list (default: best)")
    qual.add_argument("--max-quality", metavar="SPEC", help="never exceed this height")
    qual.add_argument("--codec", choices=("h264", "h265"), help="prefer a codec (falls back if absent)")
    qual.add_argument("-l", "--list", action="store_true", help="list available renditions and exit")
    qual.add_argument("--watermark", action="store_true",
                      help="also offer TikTok's watermarked renditions")

    extra = parser.add_argument_group("extras")
    extra.add_argument("--audio", nargs="?", const="mp3", metavar="FMT",
                       choices=("mp3", "m4a", "opus", "wav"), help="extract audio (needs ffmpeg)")
    extra.add_argument("--audio-only", action="store_true", help="keep only the audio file")
    extra.add_argument("--remux", action="store_true", help="rewrite MP4 with +faststart (needs ffmpeg)")
    extra.add_argument("--thumbnail", action="store_true", help="also save the cover image")
    extra.add_argument("--metadata", action="store_true", help="also save post metadata as JSON")
    extra.add_argument("--music", action="store_true", help="also save the original sound")
    extra.add_argument("--images", dest="images", action="store_true", default=None,
                       help="download slides of photo posts (default: on)")
    extra.add_argument("--no-images", dest="images", action="store_false",
                       help="skip photo posts entirely")

    net = parser.add_argument_group("network")
    net.add_argument("--ua", metavar="UA", help="User-Agent string or preset: android, iphone, chrome")
    net.add_argument("--device", metavar="NAME", default="pixel6a",
                     help="Android device profile for the mobile API: " + ", ".join(DEVICES))
    net.add_argument("--no-mobile", action="store_true", help="skip the Android API (web only)")
    net.add_argument("--no-web", action="store_true", help="skip the web page (mobile only)")
    net.add_argument("--proxy", metavar="URL", help="http(s):// or socks5:// proxy")
    net.add_argument("--timeout", type=float, default=20.0, metavar="SEC", help="per-request timeout (default: 20)")
    net.add_argument("--retries", type=int, default=3, metavar="N", help="retries per request (default: 3)")
    net.add_argument("--sleep", type=float, default=0.0, metavar="SEC",
                     help="pause between links, kinder to rate limits")

    misc = parser.add_argument_group("output control")
    misc.add_argument("--json", action="store_true", help="print one JSON object per post on stdout")
    misc.add_argument("--info", action="store_true", help="show metadata only, do not download")
    misc.add_argument("--quiet", action="store_true", help="only errors and the final path")
    misc.add_argument("-v", "--verbose", action="count", default=0, help="explain what is happening")
    misc.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    misc.add_argument("--no-progress", action="store_true", help="disable the progress bar")
    misc.add_argument("--check", action="store_true", help="run connectivity diagnostics and exit")
    misc.add_argument("-V", "--version", action="version", version=f"{__app__} {__version__}")
    return parser


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def parse_size(text: str | None) -> int | None:
    if not text:
        return None
    match = _SIZE_RE.match(text)
    if not match:
        raise UsageError(f"cannot parse size '{text}'")
    value = float(match.group(1))
    unit = (match.group(2) or "").lower()
    return int(value * _SIZE_UNITS[unit])


def resolve_ua(value: str | None) -> str | None:
    if not value:
        return None
    return UA_PRESETS.get(value.lower(), value)


def validate_template(template: str) -> None:
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise UsageError(f"bad filename template '{template}': {exc}") from None
    for _, field, _, _ in fields:
        if field and field.split(".")[0].split("[")[0] not in TOKENS:
            raise UsageError(
                f"unknown filename token {{{field}}} — available: "
                + ", ".join("{" + t + "}" for t in TOKENS)
            )


def render_name(template: str, media: Media, fmt: Format | None, *, index: int = 1,
                ext: str = "mp4") -> str:
    created = (_dt.datetime.fromtimestamp(media.created, _dt.timezone.utc)
               if media.created else None)
    values = {
        "id": media.id,
        "author": media.author or "tiktok",
        "nickname": media.nickname or media.author or "tiktok",
        "desc": truncate(media.desc or "", 60),
        "quality": fmt.quality if fmt else "",
        "resolution": (f"{fmt.width}x{fmt.height}" if fmt and fmt.width and fmt.height else ""),
        "width": str(fmt.width) if fmt and fmt.width else "",
        "height": str(fmt.height) if fmt and fmt.height else "",
        "bitrate": str(fmt.bitrate) if fmt and fmt.bitrate else "",
        "gear": fmt.gear if fmt else "",
        "codec": fmt.codec if fmt else "",
        "date": created.strftime("%Y-%m-%d") if created else "",
        "duration": format_duration(media.duration) if media.duration else "",
        "index": f"{index:02d}" if index else "",
        "ext": ext,
        "source": (fmt.sources[0] if fmt and fmt.sources else ""),
    }
    try:
        raw = template.format_map(_SafeDict(values))
    except (KeyError, IndexError, ValueError) as exc:
        raise UsageError(f"bad filename template '{template}': {exc}") from None
    name = safe_filename(raw)
    if name.lower().endswith("." + ext):
        name = name[: -(len(ext) + 1)]
    return name or f"tiktok_{media.id}"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - validated earlier
        return "{" + key + "}"


def human_count(n: int | None) -> str:
    if not n:
        return "0"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            value = n / limit
            return f"{value:.1f}{suffix}".replace(".0" + suffix, suffix)
    return str(n)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def post_block(media: Media) -> str:
    width = min(term_width(), 88)
    lines = [
        hrule(width),
        f"{C.paint(media.id, C.bold)}  {C.paint('@' + (media.author or '?'), C.italic)}"
        + (f"  {C.paint(media.nickname, C.dim)}" if media.nickname and media.nickname != media.author else ""),
    ]
    if media.desc:
        lines.append(C.paint(truncate(media.desc, width - 4), C.dim))
    facts = []
    if media.duration:
        facts.append(format_duration(media.duration))
    if media.created_iso:
        facts.append(media.created_iso)
    if media.is_photo:
        facts.append(f"{len(media.image_urls)} photos")
    if media.stats.get("play_count"):
        facts.append(f"{human_count(media.stats['play_count'])} plays")
    if media.stats.get("like_count"):
        facts.append(f"{human_count(media.stats['like_count'])} likes")
    if media.sources:
        facts.append("via " + "+".join(media.sources))
    if facts:
        lines.append(C.paint(" · ".join(facts), C.dim))
    return "\n".join(lines)


def format_table(media: Media, selection_fmt: Format | None, *, allow_watermark: bool,
                 max_quality: int | None = None, codec: str | None = None) -> str:
    formats = ranked(media, allow_watermark=allow_watermark,
                     max_quality=max_quality, codec=codec)
    rows: list[list[str]] = []
    for i, fmt in enumerate(formats, 1):
        marker = "→" if selection_fmt is not None and fmt is selection_fmt else " "
        rows.append([
            f"{marker}{i}",
            fmt.quality,
            fmt.resolution,
            f"{fmt.bitrate / 1000:.0f} kbps" if fmt.bitrate else "—",
            fmt.codec,
            format_bytes(fmt.size) if fmt.size else "—",
            "watermark" if fmt.watermarked else "",
        ])
    headers = ["#", "quality", "resolution", "bitrate", "codec", "size", ""]
    body = table(rows, headers=headers, align=["<", "<", "<", ">", "<", ">", "<"])
    return body


def selection_line(sel) -> str:
    fmt = sel.fmt
    bits = [
        C.paint(fmt.quality, C.bold),
        fmt.resolution,
        f"{fmt.bitrate / 1000:.0f} kbps" if fmt.bitrate else "",
        fmt.codec,
        format_bytes(fmt.size) if fmt.size else "",
    ]
    line = " · ".join(b for b in bits if b)
    if fmt.watermarked:
        line += C.paint("  watermarked", C.strike)
    return line


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.quiet = args.quiet or args.json
        self.max_bytes = parse_size(args.max_size)
        self.session = Session(
            timeout=args.timeout,
            retries=args.retries,
            proxy=args.proxy,
            cookie_file=args.cookies,
            user_agent=resolve_ua(args.ua) or UA_ANDROID,
        )
        self.extractor = Extractor(
            self.session,
            device=args.device,
            user_agent=resolve_ua(args.ua) or UA_ANDROID,
            use_mobile=not args.no_mobile,
            use_web=not args.no_web,
            verbose=self.verbose,
        )
        self.downloader = Downloader(
            self.session,
            retries=max(2, args.retries),
            quiet=self.quiet or args.no_progress,
        )
        self.ok_count = 0
        self.fail_count = 0
        self.failures: list[tuple[str, str]] = []
        self.fail_codes: list[int] = []

    # -- logging -----------------------------------------------------
    def verbose(self, text: str) -> None:
        if self.args.verbose and not self.quiet:
            info(text, prefix="·")

    def say(self, text: str, *, force: bool = False) -> None:
        if not self.quiet or force:
            print(text, file=sys.stderr, flush=True)

    # -- entry -------------------------------------------------------
    def run(self) -> int:
        args = self.args
        if args.check:
            return self.diagnostics()

        targets = self.collect_targets()
        if not targets:
            build_parser().print_help(sys.stderr)
            return 2

        if not self.quiet:
            self.say(banner(__version__))
        if args.out_file and len(targets) > 1:
            raise UsageError("--out-file works with a single link only")

        out_dir = Path(args.output).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UsageError(f"cannot create output directory {out_dir}: {exc}") from None

        for index, target in enumerate(targets, 1):
            if index > 1 and args.sleep:
                time.sleep(args.sleep)
            try:
                self.process(target, out_dir, index, len(targets))
                self.ok_count += 1
            except KeyboardInterrupt:
                raise
            except TtdwnError as exc:
                self.fail_count += 1
                self.fail_codes.append(exc.exit_code)
                self.failures.append((target, str(exc)))
                error(f"{target}: {exc}")
                if args.json:
                    print(json.dumps({"ok": False, "input": target, "error": str(exc)},
                                     ensure_ascii=False), flush=True)
            except Exception as exc:  # pragma: no cover - unexpected
                self.fail_count += 1
                self.fail_codes.append(1)
                self.failures.append((target, f"{type(exc).__name__}: {exc}"))
                error(f"{target}: {type(exc).__name__}: {exc}")
                if args.verbose:
                    import traceback

                    traceback.print_exc()

        if not self.quiet and len(targets) > 1:
            self.say("")
            summary = f"{self.ok_count} done"
            if self.fail_count:
                summary += C.paint(f" · {self.fail_count} failed", C.red)
            self.say(summary)
        if self.fail_count == 0:
            return 0
        # a single link should explain itself through the exit code
        return self.fail_codes[0] if len(targets) == 1 else 1

    def collect_targets(self) -> list[str]:
        targets: list[str] = list(self.args.urls)
        for path in self.args.input:
            if path == "-":
                data = sys.stdin.read()
            else:
                try:
                    data = Path(path).expanduser().read_text(encoding="utf-8")
                except OSError as exc:
                    raise UsageError(f"cannot read {path}: {exc}") from None
            for line in data.splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    targets.append(line)
        # de-duplicate while keeping order
        seen: set[str] = set()
        unique: list[str] = []
        for target in targets:
            if target not in seen:
                seen.add(target)
                unique.append(target)
        return unique

    # -- one post ----------------------------------------------------
    def process(self, target: str, out_dir: Path, index: int, total: int) -> None:
        args = self.args
        if args.verbose and not self.quiet:
            label = f"[{index}/{total}] " if total > 1 else ""
            info(f"{label}resolving {truncate(target, 70)}", prefix="→")

        media = self.extractor.extract(target)

        if not self.quiet:
            self.say("")
            self.say(post_block(media))

        if args.info:
            self.emit_info(media)
            return

        if args.list and media.is_photo and not media.video_formats:
            self.list_photos(media)
            return

        if media.is_photo and args.images is not False:
            # a photo carousel also carries a music track; the slides are the point
            self.handle_photo(media, out_dir, index)
            return
        if media.is_photo and not media.video_formats:
            raise UsageError("this post is a photo carousel — drop --no-images to fetch the slides")

        sel = pick(
            media,
            args.quality,
            allow_watermark=args.watermark,
            max_quality=args.max_quality,
            codec=args.codec,
        )
        if sel.note:
            warn(sel.note)

        if args.list:
            self.emit_list(media, sel)
            return

        if not self.quiet:
            self.say("  " + C.paint("quality  ", C.dim) + selection_line(sel))

        dest = self.destination(media, sel.fmt, out_dir)
        if self.max_bytes and sel.fmt.size and sel.fmt.size > self.max_bytes:
            raise UsageError(
                f"file is {format_bytes(sel.fmt.size)}, above --max-size {format_bytes(self.max_bytes)}"
            )

        refresh_box: dict[str, Format] = {}

        def refresh() -> Format:
            """Re-extract to get fresh signed URLs after a 403."""
            self.verbose("signed link expired — refreshing")
            fresh = self.extractor.extract(media.permalink or media.id)
            refreshed = pick(fresh, args.quality, allow_watermark=args.watermark,
                             max_quality=args.max_quality, codec=args.codec).fmt
            refresh_box["fmt"] = refreshed
            return refreshed

        result = self.downloader.download(
            sel.fmt,
            dest,
            resume=not args.no_resume,
            overwrite=args.force,
            max_bytes=self.max_bytes,
            refresh=refresh,
            label=(dest.name if not self.quiet else ""),
        )
        final_fmt = refresh_box.get("fmt", sel.fmt)

        if not self.quiet:
            ok(f"saved {format_bytes(result.size)} in {result.seconds:.1f}s"
               + (" (resumed)" if result.resumed else "")
               + (f"  from {result.url_source}" if result.url_source else ""))

        path = result.path
        notes: list[str] = []

        if args.remux:
            if have_ffmpeg():
                target_path = path.with_name(path.stem + ".fs.mp4")
                try:
                    remuxed = remux(path, target_path, overwrite=True)
                    os.replace(remuxed, path)
                    notes.append("faststart")
                except TtdwnError as exc:
                    warn(f"remux skipped: {exc}")
            else:
                warn("--remux needs ffmpeg; skipped")

        if args.audio:
            if not have_ffmpeg():
                raise TtdwnError("--audio needs ffmpeg on PATH")
            try:
                audio_path = extract_audio(path, fmt=args.audio, overwrite=args.force)
                notes.append(f"audio {audio_path.suffix.lstrip('.')}")
                if not self.quiet:
                    ok(f"audio {audio_path}")
                if args.audio_only:
                    path.unlink(missing_ok=True)
                    path = audio_path
            except TtdwnError as exc:
                warn(f"audio extraction failed: {exc}")

        if args.thumbnail and media.cover_urls:
            try:
                cover = self.save_sidecar(media.cover_urls, dest.with_suffix(".jpg"), label="cover")
                notes.append("cover")
                self.verbose(f"cover {cover}")
            except TtdwnError as exc:
                warn(f"cover skipped: {exc}")

        if args.music and media.music.get("url"):
            try:
                music = self.save_sidecar([media.music["url"]], dest.with_name(dest.stem + ".sound.m4a"),
                                          label="sound")
                notes.append("sound")
                self.verbose(f"sound {music}")
            except TtdwnError as exc:
                warn(f"sound skipped: {exc}")

        if args.metadata:
            meta_path = dest.with_suffix(".json")
            meta_path.write_text(json.dumps(media.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            notes.append("metadata")
            self.verbose(f"metadata {meta_path}")

        if args.json:
            try:
                final_size = path.stat().st_size
            except OSError:
                final_size = result.size
            payload = {
                "ok": True,
                "input": target,
                "saved": str(path),
                "size": final_size,
                "seconds": round(result.seconds, 2),
                "quality": final_fmt.quality,
                "resolution": final_fmt.resolution,
                "bitrate": final_fmt.bitrate,
                "codec": final_fmt.codec,
                "watermark": final_fmt.watermarked,
                "extras": notes,
                "media": media.as_dict(),
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        else:
            print(str(path), flush=True)

    # -- listing -----------------------------------------------------
    def emit_list(self, media: Media, sel) -> None:
        cap = parse_quality(self.args.max_quality)[1] if self.args.max_quality else None
        formats = ranked(media, allow_watermark=self.args.watermark,
                         max_quality=cap, codec=self.args.codec)
        if self.args.json:
            selected = next((i for i, f in enumerate(formats, 1) if f is sel.fmt), None)
            payload = {
                "ok": True,
                "input": media.permalink or media.id,
                "selected": selected,
                "available": sel.available,
                "note": sel.note,
                "formats": [
                    {
                        "index": i,
                        "quality": f.quality,
                        "resolution": f.resolution,
                        "bitrate": f.bitrate,
                        "codec": f.codec,
                        "size": f.size,
                        "watermark": f.watermarked,
                        "urls": f.urls,
                    }
                    for i, f in enumerate(formats, 1)
                ],
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        self.say("", force=True)
        self.say(format_table(media, sel.fmt, allow_watermark=self.args.watermark,
                              max_quality=cap, codec=self.args.codec), force=True)
        self.say("", force=True)
        self.say("  " + C.paint("selected: ", C.dim) + selection_line(sel), force=True)
        scope = "including watermark" if self.args.watermark else "without watermark"
        self.say("  " + C.paint(f"{sel.available} rendition(s) {scope}", C.dim), force=True)

    def list_photos(self, media: Media) -> None:
        if self.args.json:
            payload = {
                "ok": True,
                "input": media.permalink or media.id,
                "kind": "photo",
                "count": len(media.image_urls),
                "images": media.image_urls,
                "media": media.as_dict(),
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        rows = [[f"{i:02d}", truncate(url, max(20, term_width() - 8))]
                for i, url in enumerate(media.image_urls, 1)]
        self.say("", force=True)
        self.say(table(rows, headers=["#", "photo"]), force=True)
        self.say("", force=True)

    # -- photo posts -------------------------------------------------
    def handle_photo(self, media: Media, out_dir: Path, index: int) -> None:
        args = self.args
        base = render_name(args.filename, media, None, index=index, ext="jpg")
        folder = Path(args.out_file).expanduser() if args.out_file else out_dir / base
        folder.mkdir(parents=True, exist_ok=True)
        if not self.quiet:
            self.say("  " + C.paint(f"{len(media.image_urls)} photo(s) → {folder}", C.dim))
        saved: list[str] = []
        for i, url in enumerate(media.image_urls, 1):
            dest = folder / f"{i:02d}.jpg"
            fmt = Format(width=None, height=None, gear="image", urls=[url])
            label = f"  {i:02d}/{len(media.image_urls):02d} " if not self.quiet else ""
            result = self.downloader.download(fmt, dest, overwrite=args.force, label=label,
                                              resume=not args.no_resume)
            saved.append(str(result.path))
        if args.metadata:
            (folder / "metadata.json").write_text(
                json.dumps(media.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json:
            payload = {"ok": True, "input": media.permalink or media.id, "kind": "photo",
                       "saved": saved, "count": len(saved), "media": media.as_dict()}
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        else:
            print(str(folder), flush=True)

    # -- sidecars ----------------------------------------------------
    def save_sidecar(self, urls: Sequence[str], dest: Path, *, label: str = "") -> Path:
        fmt = Format(width=None, height=None, gear=label or "asset", urls=list(urls))
        result = self.downloader.download(fmt, dest, overwrite=self.args.force,
                                          label=(dest.name if not self.quiet else ""),
                                          resume=not self.args.no_resume)
        return result.path

    def destination(self, media: Media, fmt: Format, out_dir: Path) -> Path:
        if self.args.out_file:
            return Path(self.args.out_file).expanduser()
        name = render_name(self.args.filename, media, fmt, ext="mp4")
        return out_dir / f"{name}.mp4"

    # -- metadata only ----------------------------------------------
    def emit_info(self, media: Media) -> None:
        if self.args.json:
            print(json.dumps({"ok": True, "media": media.as_dict()}, ensure_ascii=False), flush=True)
            return
        self.say("")
        self.say(format_table(media, None, allow_watermark=self.args.watermark))
        music = media.music or {}
        if music.get("title"):
            self.say("")
            self.say("  " + C.paint("sound  ", C.dim) + truncate(music.get("title", ""), 60)
                     + (C.paint(" — " + music["author"], C.dim) if music.get("author") else ""))
        if media.image_urls:
            self.say("  " + C.paint("photos ", C.dim) + f"{len(media.image_urls)} in this post (use --images)")
        self.say("")

    # -- diagnostics -------------------------------------------------
    def diagnostics(self) -> int:
        self.say(banner(__version__))
        self.say("")
        rows = [
            ["python", sys.version.split()[0]],
            ["platform", sys.platform],
            ["ffmpeg", ffmpeg_version() or "not found (optional extras disabled)"],
            ["cookies", self.args.cookies or "—"],
            ["proxy", self.args.proxy or "environment"],
        ]
        self.say(table(rows, headers=["check", "value"]))
        self.say("")
        healthy = 0
        results: list[list[str]] = []
        probes = [("page", "https://www.tiktok.com/")] + [
            (host.split(".")[0], f"https://{host}/aweme/v1/feed/?aweme_id=1&version_code=190500"
                                 "&app_name=musically_go&channel=googleplay&device_platform=android&aid=1340")
            for host in API_HOSTS[:3]
        ]
        for name, url in probes:
            started = time.monotonic()
            try:
                resp = self.session.get(url, headers={"User-Agent": UA_ANDROID}, retries=0,
                                        allow_status=(200, 204, 400, 403, 404))
                status = resp.status
                resp.close()
                good = status < 500
            except TtdwnError as exc:
                status, good = str(exc)[:40], False
            ms = (time.monotonic() - started) * 1000
            healthy += 1 if good else 0
            results.append([name, str(status), f"{ms:.0f} ms", "ok" if good else "fail"])
        self.say(table(results, headers=["endpoint", "status", "latency", ""]))
        self.say("")
        if healthy:
            ok = f"{healthy}/{len(results)} endpoints reachable"
            self.say(C.paint(ok, C.dim))
        else:
            error("nothing reachable — check your network or --proxy")
        return 0 if healthy else 1


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        set_color(False)
    elif args.json:
        set_color(supports_color(sys.stderr))
    if args.audio_only and not args.audio:
        args.audio = "mp3"
    if args.audio and not have_ffmpeg():
        error("--audio needs ffmpeg on PATH")
        return 6

    try:
        validate_template(args.filename)
        parse_quality(args.quality)
        parse_quality(args.max_quality)
        parse_size(args.max_size)
        if args.device.lower() not in DEVICES and not args.no_mobile:
            warn(f"unknown device '{args.device}', using pixel6a "
                 f"(known: {', '.join(DEVICES)})")
            args.device = "pixel6a"
        return App(args).run()
    except UsageError as exc:
        error(str(exc))
        return 2
    except TtdwnError as exc:
        error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        warn("interrupted")
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
