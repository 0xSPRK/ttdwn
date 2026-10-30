"""Reference parsing and short-link following."""

from __future__ import annotations

import pytest

from ttdwn.errors import ResolveError
from ttdwn.resolve import parse_reference, resolve

VIDEO_ID = "7300000000000000001"


@pytest.mark.parametrize("text", [
    VIDEO_ID,
    f"https://www.tiktok.com/@user/video/{VIDEO_ID}",
    f"https://www.tiktok.com/@user/video/{VIDEO_ID}?is_from_webapp=1&sender_device=pc",
    f"https://m.tiktok.com/v/{VIDEO_ID}.html",
    f"https://www.tiktok.com/embed/v2/{VIDEO_ID}",
    f"www.tiktok.com/@user/video/{VIDEO_ID}",
    f"  https://www.tiktok.com/@user/video/{VIDEO_ID}  ",
])
def test_reference_parsing(text: str):
    ref = parse_reference(text)
    assert ref is not None and ref.aweme_id == VIDEO_ID


def test_author_and_kind_are_read_from_the_path():
    ref = parse_reference(f"https://www.tiktok.com/@frog.fan/photo/{VIDEO_ID}")
    assert ref is not None
    assert ref.author == "frog.fan"
    assert ref.kind == "photo"
    assert ref.permalink.endswith(f"/@{ref.author}/photo/{VIDEO_ID}")


def test_short_links_need_the_network():
    assert parse_reference("https://vm.tiktok.com/ZMabc123/") is None


def test_non_tiktok_hosts_are_rejected():
    with pytest.raises(ResolveError):
        parse_reference("https://vimeo.com/123456789")
    with pytest.raises(ResolveError):
        parse_reference("")


def test_short_ids_are_rejected():
    with pytest.raises(ResolveError):
        parse_reference("12345")


class _FakeRaw:
    def __init__(self, final_url: str) -> None:
        self._final = final_url

    def geturl(self) -> str:
        return self._final


class _FakeResponse:
    """What urllib hands back after following redirects."""

    def __init__(self, final_url: str) -> None:
        self.raw = _FakeRaw(final_url)
        self.status = 200
        self.headers = {}

    def close(self) -> None:
        pass


class _RedirectSession:
    def __init__(self, final_url: str) -> None:
        self.final_url = final_url
        self.requested: list[str] = []

    def get(self, url: str, **kwargs):
        self.requested.append(url)
        return _FakeResponse(self.final_url)


def test_short_link_redirect_is_followed():
    session = _RedirectSession(f"https://www.tiktok.com/@user/video/{VIDEO_ID}?is_from_webapp=1")
    ref = resolve(session, "https://vm.tiktok.com/ZMabc123/")
    assert ref.aweme_id == VIDEO_ID
    assert ref.author == "user"
    assert ref.hops and ref.hops[0].endswith("ZMabc123/")


def test_short_link_to_nowhere_reports_clearly():
    session = _RedirectSession("https://www.tiktok.com/404")
    with pytest.raises(ResolveError):
        resolve(session, "https://vm.tiktok.com/ZMdeadend/")
