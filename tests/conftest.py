"""Shared test helpers: a fake session and payload fixtures."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

MOBILE_PAYLOAD: dict[str, Any] = {
    "status_code": 0,
    "aweme_list": [
        {
            "aweme_id": "7300000000000000001",
            "desc": "frogs, but make it vertical",
            "create_time": 1699999999,
            "author": {"unique_id": "frogfan", "nickname": "Frog Fan", "uid": "107955"},
            "statistics": {"digg_count": 1234, "play_count": 56789, "comment_count": 7},
            "music": {"title": "original sound", "author": "Frog Fan",
                      "play_url": {"url_list": ["https://cdn.example/sound.mp3"]}},
            "video": {
                "duration": 24564,
                "width": 1080,
                "height": 1920,
                "ratio": "540p",
                "is_bytevc1": 0,
                "bit_rate": [
                    {"gear_name": "adapt_lower_720_1", "bit_rate": 640712, "is_bytevc1": 1,
                     "play_addr": {"width": 720, "height": 1280, "data_size": 1967308,
                                   "url_list": ["https://cdn.example/720-hevc.mp4"]}},
                    {"gear_name": "adapt_540_1", "bit_rate": 555965, "is_bytevc1": 0,
                     "play_addr": {"width": 576, "height": 1024, "data_size": 1707091,
                                   "url_list": ["https://cdn.example/540.mp4"]}},
                ],
                "play_addr": {"width": 576, "height": 1024, "data_size": 3288875,
                              "url_list": ["https://cdn.example/play-540.mp4"]},
                "play_addr_h264": {"width": 576, "height": 1024, "data_size": 3288875,
                                   "url_list": ["https://cdn.example/play-540.mp4"]},
                "play_addr_265": {"width": 576, "height": 1024, "data_size": 1500000,
                                  "url_list": ["https://cdn.example/play-540-hevc.mp4"]},
                "download_addr": {"width": 720, "height": 720, "data_size": 4049946,
                                  "url_list": ["https://cdn.example/watermarked.mp4"]},
                "cover": {"url_list": ["https://cdn.example/cover.jpg"]},
            },
        }
    ],
}

PHOTO_PAYLOAD: dict[str, Any] = {
    "status_code": 0,
    "aweme_list": [
        {
            "aweme_id": "7300000000000000002",
            "desc": "three photos and a sound",
            "create_time": 1700000001,
            "author": {"unique_id": "slides", "nickname": "Slides", "uid": "42"},
            "statistics": {"digg_count": 10, "play_count": 20},
            "music": {"title": "sneaky snare", "author": "someone",
                      "play_url": {"url_list": ["https://cdn.example/snare.mp3"]}},
            "image_post_info": {
                "images": [
                    {"display_image": {"url_list": ["https://cdn.example/a.jpeg", "https://cdn.example/a2.jpeg"]}},
                    {"display_image": {"url_list": ["https://cdn.example/b.jpeg"]}},
                    {"display_image": {"url_list": ["https://cdn.example/c.jpeg"]}},
                ]
            },
            "video": {"duration": 12000, "width": 576, "height": 1024},
        }
    ],
}


def web_payload(item_id: str = "7300000000000000001") -> dict[str, Any]:
    return {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "statusCode": 0,
                "itemInfo": {
                    "itemStruct": {
                        "id": item_id,
                        "desc": "frogs, but make it vertical",
                        "createTime": "1699999999",
                        "author": {"uniqueId": "frogfan", "nickname": "Frog Fan", "id": "107955"},
                        "statsV2": {"diggCount": "1234", "playCount": "56789", "commentCount": "7"},
                        "music": {"title": "original sound", "authorName": "Frog Fan",
                                  "playUrl": "https://cdn.example/sound.mp3"},
                        "video": {
                            "duration": 24,
                            "width": 576,
                            "height": 1024,
                            "codecType": "h264",
                            "originCover": "https://cdn.example/cover.jpg",
                            "bitrateInfo": [
                                {"GearName": "adapt_lowest_1080_1", "Bitrate": 1094429,
                                 "CodecType": "h265_hvc1",
                                 "PlayAddr": {"Width": 1080, "Height": 1920, "DataSize": "3360445",
                                              "UrlList": ["https://cdn.example/1080-hevc.mp4"]}},
                                {"GearName": "normal_540_0", "Bitrate": 1071120,
                                 "CodecType": "h264",
                                 "PlayAddr": {"Width": 576, "Height": 1024, "DataSize": "3288875",
                                              "UrlList": ["https://cdn.example/play-540.mp4"]}},
                            ],
                            "downloadAddr": "https://cdn.example/watermarked.mp4",
                        },
                    }
                },
            }
        }
    }


def web_html(payload: dict[str, Any] | None = None) -> bytes:
    island = json.dumps(payload or web_payload())
    return (
        "<!doctype html><html><head><title>t</title></head><body>"
        f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{island}</script>'
        "</body></html>"
    ).encode()


def embed_html(item_id: str = "7300000000000000001") -> bytes:
    payload = {
        "source": {
            "data": {
                f"/embed/v2/{item_id}": {
                    "videoData": {
                        "itemInfos": {
                            "id": item_id,
                            "text": "embed text",
                            "createTime": 1699999999,
                            "covers": ["https://cdn.example/cover.jpg"],
                            "video": {
                                "urls": ["https://cdn.example/embed.mp4"],
                                "videoMeta": {"width": 576, "height": 1024, "duration": 24.4},
                            },
                        },
                        "authorInfos": {"uniqueId": "frogfan", "nickname": "Frog Fan", "userId": "107955"},
                    }
                }
            }
        }
    }
    return (
        '<html><body><script id="__FRONTITY_CONNECT_STATE__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    ).encode()


class FakeSession:
    """Stands in for ttdwn.http.Session: routes URLs to canned responses."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.user_agent = "test"
        self.calls: list[str] = []

    def _resolve(self, url: str):
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        raise AssertionError(f"unexpected URL in test: {url}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(url)
        payload = self._resolve(url)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, (bytes, str)):
            return json.loads(payload)
        return payload

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        self.calls.append(url)
        payload = self._resolve(url)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode()
        return json.dumps(payload).encode()

    def get(self, url: str, **kwargs: Any):  # pragma: no cover - not used in tests
        raise AssertionError("not implemented in fake session")

    def stream(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise AssertionError("not implemented in fake session")


# ---------------------------------------------------------------------------
# a real local HTTP server for download tests
# ---------------------------------------------------------------------------

class MediaServer:
    """Serves a blob with Range support, optional failure injection."""

    def __init__(self, body: bytes, *, fail_first: int = 0, ignore_range: bool = False,
                 deny_after: int | None = None, wrong_range_start: bool = False) -> None:
        self.body = body
        self.fail_first = fail_first
        self.ignore_range = ignore_range
        self.deny_after = deny_after
        self.wrong_range_start = wrong_range_start
        self.hits = 0
        self.requests: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_GET(self) -> None:
                server.hits += 1
                headers = {k.lower(): v for k, v in self.headers.items()}
                server.requests.append(headers)
                if server.fail_first and server.hits <= server.fail_first:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if server.deny_after is not None and server.hits > server.deny_after:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                rng = headers.get("range")
                partial = False
                if rng and not server.ignore_range and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(server.body):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(server.body)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True
                chunk = server.body[start:]
                self.send_response(206 if partial else 200)
                if partial:
                    reported = 0 if server.wrong_range_start else start
                    self.send_header("Content-Range",
                                     f"bytes {reported}-{len(server.body) - 1}/{len(server.body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(chunk)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/media.mp4"

    def __enter__(self) -> "MediaServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


@pytest.fixture()
def media_server():
    servers: list[MediaServer] = []

    def factory(body: bytes, **kwargs: Any) -> MediaServer:
        server = MediaServer(body, **kwargs)
        server.__enter__()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.__exit__()


def make_session() -> Callable[..., Any]:
    from ttdwn.http import Session

    return Session(timeout=10, retries=1)
