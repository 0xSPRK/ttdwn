"""Web interface: JSON api, job lifecycle, file serving, guards.

Everything runs offline: the extractor and downloader are stubbed, so these
tests describe the HTTP contract rather than TikTok's behaviour.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from conftest import MOBILE_PAYLOAD
from ttdwn.extract import _from_mobile
from ttdwn import webui

PAYLOAD = bytes(range(256)) * 512  # 128 KiB


def fixture_media():
    return _from_mobile(MOBILE_PAYLOAD["aweme_list"][0])


class FakeExtractor:
    def __init__(self, session=None, **kwargs):
        self.session = session

    def extract(self, url):
        media = fixture_media()
        media.url = "https://www.tiktok.com/@frogfan/video/7300000000000000001"
        return media


class FakeDownloader:
    calls: list[dict] = []

    def __init__(self, session=None, **kwargs):
        self.session = session
        self.options = kwargs

    def download(self, fmt, dest, *, overwrite=False, refresh=None, resume=True, **kwargs):
        dest = Path(dest)
        if dest.exists() and not overwrite:
            from ttdwn.errors import DownloadError

            raise DownloadError(f"{dest} already exists — pass --force to overwrite")
        dest.parent.mkdir(parents=True, exist_ok=True)
        factory = kwargs.get("progress_factory")
        progress = factory(len(PAYLOAD)) if factory else None
        if progress:
            progress.start()
        with open(dest, "wb") as fh:
            for i in range(0, len(PAYLOAD), 16384):
                block = PAYLOAD[i:i + 16384]
                fh.write(block)
                if progress:
                    progress.update(len(block))
        if progress:
            progress.finish()

        class Result:
            path = dest
            size = len(PAYLOAD)
            seconds = 0.1
            resumed = False
            url_source = "test"

        FakeDownloader.calls.append({"name": dest.name, "fmt": fmt.quality})
        return Result()


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "Extractor", FakeExtractor)
    monkeypatch.setattr(webui, "Downloader", FakeDownloader)
    FakeDownloader.calls = []
    options = webui.Options(host="127.0.0.1", port=0, out_dir=tmp_path)
    web = webui.WebUI(options)
    url = web.start_background()
    yield web, url.rstrip("/"), tmp_path
    web.stop()


def get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def post(url: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:80]}


# ---------------------------------------------------------------------------

def test_page_is_served_without_external_assets(server):
    _, base, _ = server
    status, body, headers = get(base + "/")
    assert status == 200
    page = body.decode()
    assert "ttdwn" in page
    assert "http://" not in page.replace("http://www.w3.org", ""), "no external references"
    assert "https://" not in page.replace("https://www.tiktok.com/@user/video/", "")
    assert headers["Content-Type"].startswith("text/html")


def test_health(server):
    _, base, tmp_path = server
    status, body, _ = get(base + "/api/health")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] and data["version"]
    assert data["out_dir"] == str(tmp_path.resolve())


def test_info_returns_ladder_best_first(server):
    _, base, _ = server
    status, data = post(base + "/api/info", {"url": "7300000000000000001"})
    assert status == 200 and data["ok"]
    media = data["media"]
    assert media["author"] == "frogfan"
    assert media["duration"] == pytest.approx(24.564)
    formats = data["formats"]
    assert [f["index"] for f in formats] == list(range(1, len(formats) + 1))
    assert formats[0]["quality"] == "720p"           # best available in the fixture
    assert all(f["watermark"] is False for f in formats)
    assert data["thumb"].startswith("/api/image?u=")
    assert formats[0]["preview"].startswith("/api/preview?")


def test_info_rejects_empty_url(server):
    _, base, _ = server
    status, data = post(base + "/api/info", {})
    assert status == 400
    assert "paste a tiktok link" in data["error"].lower()


def test_job_lifecycle_and_file(server):
    _, base, tmp_path = server
    status, data = post(base + "/api/jobs", {"url": "7300000000000000001", "index": 1})
    assert status == 200 and data["ok"]
    job_id = data["job"]["id"]

    job = {}
    for _ in range(100):
        _, body, _ = get(f"{base}/api/jobs/{job_id}")
        job = json.loads(body)["job"]
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)

    assert job["status"] == "done", job
    assert job["size"] == len(PAYLOAD)
    assert job["quality"] == "720p"
    assert job["filename"].endswith(".mp4") and job["filename"].startswith("frogfan_")
    assert job["file_url"] == f"/api/jobs/{job_id}/file"

    status, blob, headers = get(base + job["file_url"])
    assert status == 200 and blob == PAYLOAD
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["Content-Type"] == "video/mp4"

    saved = list(tmp_path.glob("*.mp4"))
    assert saved and saved[0].read_bytes() == PAYLOAD


def test_two_downloads_of_one_post_do_not_overwrite(server):
    _, base, tmp_path = server
    ids = []
    for _ in range(2):
        _, data = post(base + "/api/jobs", {"url": "7300000000000000001", "index": 1})
        ids.append(data["job"]["id"])
    for job_id in ids:
        for _ in range(100):
            _, body, _ = get(f"{base}/api/jobs/{job_id}")
            if json.loads(body)["job"]["status"] in ("done", "error"):
                break
            time.sleep(0.05)
    names = sorted(p.name for p in tmp_path.glob("*.mp4"))
    assert len(names) == 2 and names[0] != names[1]


def test_job_index_out_of_range(server):
    _, base, _ = server
    status, data = post(base + "/api/jobs", {"url": "7300000000000000001", "index": 99})
    assert status == 400
    assert "index" in data["error"]


def test_unknown_job_is_404(server):
    _, base, _ = server
    assert get(base + "/api/jobs/deadbeef")[0] == 404
    assert get(base + "/api/jobs/deadbeef/file")[0] == 404


def test_path_traversal_is_refused(server):
    _, base, tmp_path = server
    (tmp_path.parent / "secret.txt").write_text("nope")
    for path in ("/api/jobs/../secret.txt", "/api/jobs/%2e%2e/secret.txt", "/../../etc/passwd"):
        status, _, _ = get(base + path)
        assert status == 404, path


def test_image_proxy_blocks_foreign_hosts(server):
    _, base, _ = server
    evil = urllib.parse.quote("http://169.254.169.254/latest/meta-data", safe="")
    assert get(f"{base}/api/image?u={evil}")[0] == 403
    assert get(f"{base}/api/image?u=") [0] == 403
    assert webui.ApiHandler._allowed("https://v19e.tiktokcdn.com/x.jpg")
    assert webui.ApiHandler._allowed("https://p16-common-sign.tiktokcdn-us.com/x")
    assert not webui.ApiHandler._allowed("https://tiktokcdn.com.evil.test/x.jpg")


def test_preview_forwards_range_and_headers(server, monkeypatch):
    web, base, _ = server
    media = fixture_media()

    class FakeResp:
        status = 206
        headers = {"Content-Range": "bytes 0-9/100", "Content-Length": "10",
                   "Content-Type": "video/mp4"}

        def header(self, name, default=None):
            return self.headers.get(name, default)

        def iter_chunks(self, size=1024):
            yield b"0123456789"

        def close(self):
            pass

    class FakeSession:
        def __init__(self):
            self.seen = None

        def request(self, method, url, **kwargs):
            self.seen = kwargs.get("headers")
            return FakeResp()

    session = FakeSession()
    monkeypatch.setattr(web.manager, "resolve", lambda url: (media, session))
    encoded = urllib.parse.quote("https://www.tiktok.com/@frogfan/video/7300000000000000001", safe="")
    status, body, headers = get(f"{base}/api/preview?u={encoded}&i=1",
                                headers={"Range": "bytes=0-9"})
    assert status == 206
    assert body == b"0123456789"
    assert headers["Content-Range"] == "bytes 0-9/100"
    assert session.seen["Range"] == "bytes=0-9"


def test_preview_reports_when_every_mirror_refuses(server, monkeypatch):
    web, base, _ = server
    media = fixture_media()

    class DeadResp:
        status = 403

        def header(self, name, default=None):
            return default

        def close(self):
            pass

    class FakeSession:
        def request(self, *a, **kw):
            return DeadResp()

    monkeypatch.setattr(web.manager, "resolve", lambda url: (media, FakeSession()))
    encoded = urllib.parse.quote("7300000000000000001", safe="")
    status, body, _ = get(f"{base}/api/preview?u={encoded}&i=1")
    assert status == 502
    assert "unavailable" in json.loads(body)["error"]


def test_bad_flag_with_webui_exits_before_binding_a_port():
    from ttdwn.cli import main

    # flags are validated before the server starts, so a typo never leaves a
    # socket bound (and never blocks the process in serve_forever)
    assert main(["--webui", "-f", "{nope}", "7300000000000000001"]) == 2
    assert main(["--webui", "-q", "potato"]) == 2
