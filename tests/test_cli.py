"""CLI plumbing: templates, sizes, exit codes."""

from __future__ import annotations

import pytest

from ttdwn.cli import (
    App,
    build_parser,
    format_table,
    main,
    parse_size,
    render_name,
    validate_template,
)
from ttdwn.errors import UsageError
from ttdwn.formats import pick
from ttdwn.models import Format, Media


def make_media() -> Media:
    return Media(id="7300000000000000001", author="frogfan", nickname="Frog Fan",
                 desc="frogs / but: make it *vertical*?", created=1699999999,
                 duration_ms=24564)


def test_filename_tokens():
    fmt = Format(width=1080, height=1920, bitrate=1094429, gear="g", sources=["web"])
    name = render_name("{author}_{id}_{quality}", make_media(), fmt)
    assert name == "frogfan_7300000000000000001_1080p"
    name = render_name("{nickname} {resolution} {duration} {date}", make_media(), fmt)
    assert name == "Frog Fan 1080x1920 00_24 2023-11-14"  # ":" is not legal in filenames
    assert "/" not in render_name("{desc}", make_media(), fmt)


def test_unknown_token_is_rejected_before_any_network_work():
    with pytest.raises(UsageError):
        validate_template("{author}_{nope}")


def test_malformed_template_is_a_usage_error():
    with pytest.raises(UsageError):
        validate_template("{author")
    assert main(["-f", "{author", "7300000000000000001"]) == 2


def test_watermark_rows_are_listed_once():
    media = make_media()
    media.formats = [
        Format(width=1080, height=1920, bitrate=1094429, urls=["https://cdn/clean.mp4"]),
        Format(width=720, height=720, urls=["https://cdn/wm1.mp4"], watermarked=True),
        Format(width=720, height=720, urls=["https://cdn/wm2.mp4"], watermarked=True),
    ]
    rendered = format_table(media, None, allow_watermark=True)
    assert rendered.count("watermark") == 2


def test_unwritable_output_directory_is_a_usage_error(monkeypatch, tmp_path):
    def deny(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.mkdir", deny)
    assert main(["-o", str(tmp_path / "out"), "7300000000000000001"]) == 2


def test_list_prints_even_with_quiet(capsys):
    args = build_parser().parse_args(["--list", "--quiet", "7300000000000000001"])
    app = App(args)
    media = make_media()
    media.formats.append(Format(width=1080, height=1920, bitrate=1094429,
                                gear="g", urls=["https://cdn/x.mp4"], sources=["web"]))
    app.emit_list(media, pick(media, "best"))
    err = capsys.readouterr().err
    assert "1080p" in err
    assert "selected" in err


def test_list_on_a_photo_post_does_not_download(capsys, tmp_path):
    args = build_parser().parse_args(["--list", "--quiet", "7300000000000000001"])
    app = App(args)
    media = make_media()
    media.image_urls = ["https://cdn/a.jpg", "https://cdn/b.jpg"]

    class Extractor:
        def extract(self, target):
            return media

    class Downloader:
        def download(self, *args, **kwargs):
            raise AssertionError("--list must not download photo slides")

    app.extractor = Extractor()
    app.downloader = Downloader()
    app.process("7300000000000000001", tmp_path, 1, 1)
    err = capsys.readouterr().err
    assert "a.jpg" in err and "b.jpg" in err


@pytest.mark.parametrize("raw,expected", [
    ("200MB", 200_000_000),
    ("1GiB", 1024 ** 3),
    ("512", 512),
    (None, None),
])
def test_parse_size(raw, expected):
    assert parse_size(raw) == expected


def test_parse_size_rejects_junk():
    with pytest.raises(UsageError):
        parse_size("big")


def test_bad_quality_exits_2_without_touching_the_network():
    assert main(["-q", "potato", "https://www.tiktok.com/@a/video/7300000000000000001"]) == 2


def test_foreign_link_exits_3():
    assert main(["https://vimeo.com/123456789"]) == 3


def test_no_arguments_prints_usage():
    assert main([]) == 2


def test_audio_without_ffmpeg_is_reported(monkeypatch):
    monkeypatch.setattr("ttdwn.cli.have_ffmpeg", lambda: False)
    assert main(["--audio", "mp3", "7300000000000000001"]) == 6
