"""Extraction: mobile ladder, web island, embed fallback, merging, dedupe."""

from __future__ import annotations

import pytest

from conftest import (
    MOBILE_PAYLOAD,
    PHOTO_PAYLOAD,
    FakeSession,
    embed_html,
    web_html,
)
from ttdwn.errors import ExtractionError
from ttdwn.extract import (
    Extractor,
    _drop_duplicate_streams,
    _from_mobile,
    _from_web,
    _merge,
    _parse_web_item,
)
from ttdwn.formats import pick
from ttdwn.models import Format, Media


def test_mobile_ladder_keeps_watermark_apart():
    media = _from_mobile(MOBILE_PAYLOAD["aweme_list"][0])
    assert media is not None
    assert media.id == "7300000000000000001"
    assert media.author == "frogfan"
    assert media.duration_ms == 24564
    clean = media.video_formats
    assert clean and all(not f.watermarked for f in clean)
    assert {f.quality for f in clean} == {"540p", "720p"}
    watermarked = media.watermarked_formats
    assert any("download_addr" == f.gear for f in watermarked)
    assert all(f.watermarked for f in watermarked)


def test_mobile_codec_flags_are_trusted():
    media = _from_mobile(MOBILE_PAYLOAD["aweme_list"][0])
    by_gear = {f.gear: f for f in media.formats}
    assert by_gear["adapt_lower_720_1"].codec == "h265"  # is_bytevc1 == 1
    assert by_gear["adapt_540_1"].codec == "h264"
    assert by_gear["play_addr_265"].codec == "h265"


def test_photo_post_parsing():
    media = _from_mobile(PHOTO_PAYLOAD["aweme_list"][0])
    assert media is not None
    assert media.is_photo
    assert len(media.image_urls) == 3
    assert media.image_urls[0].endswith("a.jpeg")
    assert media.music["title"] == "sneaky snare"
    assert media.sources == ["mobile"]


def test_web_island_parsing():
    item = _parse_web_item(web_html().decode())
    assert item is not None and item["id"] == "7300000000000000001"
    media = _from_web(item)
    assert media is not None
    assert media.author == "frogfan"
    qualities = {(f.quality, f.codec) for f in media.video_formats}
    assert ("1080p", "h265") in qualities
    assert ("540p", "h264") in qualities
    assert media.watermarked_formats, "downloadAddr must be flagged as watermarked"
    assert media.music["url"] == "https://cdn.example/sound.mp3"


def test_web_island_survives_html_noise():
    html = web_html().decode().replace("</script>", "</script><script>var x=1</script>", 1)
    assert _parse_web_item(html) is not None


def test_full_extract_prefers_web_then_merges_mobile():
    session = FakeSession({
        "/@frogfan/video/": web_html(),
        "api-h2.tiktokv.com": MOBILE_PAYLOAD,
    })
    media = Extractor(session).extract("https://www.tiktok.com/@frogfan/video/7300000000000000001")
    assert set(media.sources) == {"web", "mobile"}
    sel = pick(media, "best")
    assert sel.fmt.quality == "1080p"
    # the same 540p file reached through both sources must appear once
    assert len([f for f in media.formats if f.quality == "540p" and f.codec == "h264"
                and "play-540" in f.urls[0]]) == 1


def test_extract_falls_back_to_embed_and_learns_the_author():
    session = FakeSession({
        "/embed/v2/": embed_html(),
        "api-h2.tiktokv.com": {"aweme_list": []},
    })
    media = Extractor(session).extract("7300000000000000001")
    assert media.formats and media.formats[0].quality == "540p"
    assert media.author == "frogfan"
    assert media.duration_ms == 24400


def test_extract_raises_when_everything_is_dead():
    session = FakeSession({
        "api-h2.tiktokv.com": {"aweme_list": []},
        "/embed/v2/": b"<html>nothing</html>",
    })
    with pytest.raises(ExtractionError):
        Extractor(session).extract("7300000000000000001")


def test_duplicate_url_sets_collapse():
    media = _from_mobile(MOBILE_PAYLOAD["aweme_list"][0])
    before = len(media.formats)
    _drop_duplicate_streams(media)
    assert len(media.formats) < before
    urls = [u for f in media.formats for u in f.urls]
    assert len(urls) == len(set(urls)), "no URL should be listed twice"


def test_merge_keeps_different_codecs_apart():
    base = Media(id="1", author="a")
    base.formats = [Format(width=576, height=1024, bitrate=500000, gear="g", codec="h264",
                           urls=["https://cdn/a.mp4"])]
    extra = Media(id="1", author="a")
    extra.formats = [Format(width=576, height=1024, bitrate=500000, gear="g", codec="h265",
                            urls=["https://cdn/b.mp4"])]
    merged = _merge(base, extra)
    assert merged is not None
    assert len(merged.formats) == 2
    assert {f.codec for f in merged.formats} == {"h264", "h265"}


def test_bare_id_uses_mobile_only():
    session = FakeSession({"api-h2.tiktokv.com": MOBILE_PAYLOAD})
    media = Extractor(session).extract("7300000000000000001")
    assert media.sources == ["mobile"]
    assert media.url == "https://www.tiktok.com/@frogfan/video/7300000000000000001"
