"""Quality selection."""

from __future__ import annotations

import pytest

from ttdwn.errors import UsageError
from ttdwn.formats import parse_quality, pick, ranked
from ttdwn.models import Format, Media, quality_label


def make_media(*, with_watermark: bool = False) -> Media:
    media = Media(id="1", author="a", duration_ms=1000)
    ladder = [
        (1080, 1920, 1094429, "h265", 3360445),
        (720, 1280, 640712, "h265", 1967308),
        (576, 1024, 1071120, "h264", 3288875),
        (576, 1024, 343612, "h264", 1000000),
        (360, 640, 200000, "h264", 400000),
    ]
    for w, h, br, codec, size in ladder:
        media.formats.append(Format(width=w, height=h, bitrate=br, codec=codec, size=size,
                                    gear=f"g{br}", urls=[f"https://cdn/{br}.mp4"]))
    if with_watermark:
        media.formats.append(Format(width=720, height=720, gear="download_addr",
                                    urls=["https://cdn/wm.mp4"], watermarked=True, size=10))
    return media


def test_quality_labels_use_the_short_side():
    assert quality_label(576, 1024) == "540p"
    assert quality_label(720, 1280) == "720p"
    assert quality_label(1080, 1920) == "1080p"
    assert quality_label(720, 720) == "720p"


def test_best_is_the_largest_frame():
    assert pick(make_media(), "best").fmt.quality == "1080p"


def test_worst_is_the_smallest():
    assert pick(make_media(), "worst").fmt.quality == "360p"


def test_exact_height():
    assert pick(make_media(), "720").fmt.quality == "720p"


def test_height_falls_back_downwards_with_a_note():
    sel = pick(make_media(), "480")
    assert sel.fmt.quality == "360p"
    assert "480p" in sel.note


def test_height_above_the_ladder_is_capped():
    sel = pick(make_media(), "2160")
    assert sel.fmt.quality == "1080p"
    assert "2160p" in sel.note


def test_index_selection():
    assert pick(make_media(), "2").fmt.quality == "720p"
    with pytest.raises(UsageError):
        pick(make_media(), "9")


def test_same_height_prefers_h264():
    media = make_media()
    sel = pick(media, "540")
    assert sel.fmt.codec == "h264"
    # and the higher bitrate wins among equals
    assert sel.fmt.bitrate == 1071120


def test_codec_filter_with_fallback():
    sel = pick(make_media(), "best", codec="h264")
    # no h264 above 540p: falls back to the best h264 stream
    assert sel.fmt.quality == "540p"
    assert sel.fmt.codec == "h264"


def test_max_quality_caps_the_ladder():
    sel = pick(make_media(), "best", max_quality="720")
    assert sel.fmt.quality == "720p"


def test_watermark_is_opt_in():
    media = make_media(with_watermark=True)
    assert all(not f.watermarked for f in ranked(media))
    assert any(f.watermarked for f in ranked(media, allow_watermark=True))


def test_parse_quality_accepts_common_spellings():
    assert parse_quality("best") == ("best", None)
    assert parse_quality(None) == ("best", None)
    assert parse_quality("1080P") == ("height", 1080)
    assert parse_quality("#2") == ("index", 2)
    assert parse_quality("worst") == ("worst", None)
    with pytest.raises(UsageError):
        parse_quality("potato")
    with pytest.raises(UsageError):
        parse_quality("99999")


def test_empty_media_is_an_error():
    media = Media(id="1", author="a")
    with pytest.raises(UsageError):
        pick(media, "best")
