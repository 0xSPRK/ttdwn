"""Local web interface for ttdwn: ``ttdwn --webui``.

A single-file server (standard library only) that puts the same extraction and
download pipeline behind a browser. Nothing is exposed beyond what the CLI
does: paste a link, pick a rendition, get a watermark-free file.

Endpoints
---------
``GET  /``                  the page (inline CSS/JS, no external assets)
``POST /api/info``          {"url"} -> post metadata + rendition ladder
``POST /api/jobs``          {"url","index"} -> start a download, returns a job
``GET  /api/jobs/<id>``     job state: status, bytes, speed, error
``GET  /api/jobs/<id>/file`` the finished file, as an attachment
``GET  /api/image?u=``      cover proxy (TikTok CDN hosts only)
``GET  /api/preview?…``     range-capable stream proxy for the <video> element

The server binds to 127.0.0.1 by default. Files land in ``--output`` so a web
download and a CLI download are indistinguishable afterwards.
"""

from __future__ import annotations

import functools
import json
import mimetypes
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __app__, __version__
from .download import Downloader
from .errors import TtdwnError, UsageError
from .extract import Extractor
from .formats import ranked
from .http import Session
from .models import Format, Media

__all__ = ["WebUI", "serve", "main"]

MAX_BODY = 64 * 1024
CDN_SUFFIXES = (
    "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokcdn-va.com",
    "tiktokv.com", "tiktokv.us", "tiktok.com", "muscdn.com", "byteoversea.com",
    "ibytedtos.com",
)
CDN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Referer": "https://www.tiktok.com/",
}


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    url: str
    index: int
    status: str = "queued"          # queued | working | done | error
    done: int = 0
    total: int | None = None
    speed: float = 0.0
    path: Path | None = None
    filename: str = ""
    size: int = 0
    quality: str = ""
    resolution: str = ""
    codec: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        fraction = (self.done / self.total) if self.total else None
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "fraction": round(fraction, 4) if fraction is not None else None,
            "speed": round(self.speed, 1),
            "filename": self.filename,
            "size": self.size,
            "quality": self.quality,
            "resolution": self.resolution,
            "codec": self.codec,
            "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.created, 2),
            "file_url": f"/api/jobs/{self.id}/file" if self.status == "done" else None,
        }


class _JobProgress:
    """Progress sink passed to Downloader; writes nothing to the terminal."""

    def __init__(self, total: int | None = None, *, label: str = "", enabled: bool = True,
                 job: Job | None = None) -> None:
        self.job = job
        self.total = total if total and total > 0 else None
        self._t = time.monotonic()
        self._bytes = 0
        if job is not None:
            job.total = self.total
            job.status = "working"

    def start(self) -> "_JobProgress":
        return self

    def update(self, n: int) -> None:
        job = self.job
        if job is None:
            return
        job.done += n
        now = time.monotonic()
        dt = now - self._t
        if dt >= 0.3:
            instant = (job.done - self._bytes) / dt
            job.speed = instant if not job.speed else (job.speed * 0.6 + instant * 0.4)
            self._t, self._bytes = now, job.done

    def advance_to(self, n: int) -> None:
        if self.job is not None:
            self.job.done = n

    def finish(self, label: str | None = None) -> None:
        return


class JobManager:
    def __init__(self, options: "Options") -> None:
        self.options = options
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self._semaphore = threading.Semaphore(2)  # at most two transfers at once
        self._cache: dict[str, tuple[float, Media, Session]] = {}
        self._cache_lock = threading.Lock()

    # -- helpers used by the handler ---------------------------------
    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def new_session(self) -> Session:
        o = self.options
        return Session(timeout=o.timeout, retries=o.retries, proxy=o.proxy,
                       cookie_file=o.cookies)

    def extractor(self, session: Session) -> Extractor:
        o = self.options
        return Extractor(session, device=o.device)

    def resolve(self, url: str) -> tuple[Media, Session]:
        """Extract and keep the session that produced the signed links.

        The web ladder's CDN URLs are signed for the cookies of the page load
        that returned them, so the session travels with the cached media.
        """
        key = url.strip().lower()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < 600:
                return hit[1], hit[2]
        session = self.new_session()
        media = self.extractor(session).extract(url)
        with self._cache_lock:
            self._cache[key] = (time.time(), media, session)
        return media, session

    def inspect(self, url: str) -> Media:
        return self.resolve(url)[0]

    def formats(self, media: Media) -> list[Format]:
        return ranked(media, allow_watermark=self.options.watermark)

    def format_at(self, media: Media, index: int) -> Format:
        items = self.formats(media)
        if not items:
            raise UsageError("this post has no watermark-free video stream")
        if 1 <= index <= len(items):
            return items[index - 1]
        raise UsageError(f"rendition {index} does not exist — choose 1..{len(items)}")

    # -- lifecycle ----------------------------------------------------
    def create(self, url: str, index: int) -> Job:
        job = Job(id=secrets.token_hex(6), url=url, index=index)
        with self.lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        o = self.options
        with self._semaphore:
            session = self.new_session()
            try:
                job.status = "working"
                media = self.extractor(session).extract(job.url)
                fmt = self.format_at(media, job.index)
                job.quality = fmt.quality
                job.resolution = fmt.resolution
                job.codec = fmt.codec
                path = o.out_dir / (self._name(media, fmt) + ".mp4")

                downloader = Downloader(
                    session,
                    retries=max(2, o.retries),
                    quiet=False,
                    progress_factory=functools.partial(_JobProgress, job=job),
                )

                def refresh() -> Format:
                    fresh = self.extractor(session).extract(media.permalink or media.id)
                    return self.format_at(fresh, 1)

                counter = {"n": 0}

                def target() -> Path:
                    """Never overwrite: add a suffix when the name is taken."""
                    counter["n"] += 1
                    if counter["n"] == 1:
                        return path
                    return path.with_name(f"{path.stem}-{counter['n']}{path.suffix}")

                while True:
                    dest = target()
                    try:
                        result = downloader.download(fmt, dest, overwrite=False, refresh=refresh)
                        break
                    except TtdwnError as exc:
                        if "already exists" in str(exc) and counter["n"] < 50:
                            continue
                        raise

                job.path = result.path
                job.filename = result.path.name
                job.size = result.size
                job.total = job.total or result.size
                job.done = job.size
                job.status = "done"
                job.finished = time.time()
            except TtdwnError as exc:
                job.status = "error"
                job.error = str(exc)
                job.finished = time.time()
            except Exception as exc:  # pragma: no cover - unexpected
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished = time.time()

    def _name(self, media: Media, fmt: Format) -> str:
        from .cli import render_name  # lazy: cli imports this module on demand

        return render_name(self.options.template, media, fmt, ext="mp4")


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------

@dataclass
class Options:
    host: str = "127.0.0.1"
    port: int = 8765
    out_dir: Path = field(default_factory=lambda: Path(".").resolve())
    device: str = "pixel6a"
    template: str = "{author}_{id}_{quality}"
    cookies: str | None = None
    proxy: str | None = None
    timeout: float = 20.0
    retries: int = 3
    watermark: bool = False
    initial: str = ""


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

class WebUI:
    """Ties an Options set to a ThreadingHTTPServer."""

    def __init__(self, options: Options) -> None:
        self.options = options
        self.manager = JobManager(options)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------
    def make_server(self) -> ThreadingHTTPServer:
        manager = self.manager
        options = self.options

        class Handler(ApiHandler):
            jobs = manager
            opts = options

        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True
        self.httpd = ThreadingHTTPServer((options.host, options.port), Handler)
        return self.httpd

    @property
    def url(self) -> str:
        if self.httpd is None:
            return f"http://{self.options.host}:{self.options.port}/"
        host, port = self.httpd.server_address[:2]
        display_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        return f"http://{display_host}:{port}/"

    def serve_forever(self) -> None:
        httpd = self.httpd or self.make_server()
        httpd.serve_forever()

    def start_background(self) -> str:
        httpd = self.httpd or self.make_server()
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class ApiHandler(BaseHTTPRequestHandler):
    jobs: JobManager
    opts: Options
    server_version = f"{__app__}/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
        return

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise UsageError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise UsageError("body must be JSON") from None
        return data if isinstance(data, dict) else {}

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    # -- routing ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        try:
            if path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                self._send(200, FAVICON, "image/svg+xml")
            elif path == "/api/image":
                self.handle_image()
            elif path == "/api/preview":
                self.handle_preview()
            elif re.fullmatch(r"/api/jobs/[A-Za-z0-9]+", path):
                self.handle_job(path.rsplit("/", 1)[-1])
            elif re.fullmatch(r"/api/jobs/[A-Za-z0-9]+/file", path):
                self.handle_file(path.split("/")[3])
            elif path == "/api/health":
                self._json({"ok": True, "version": __version__, "out_dir": str(self.opts.out_dir)})
            else:
                self._fail("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - safety net
            self._fail(f"{type(exc).__name__}: {exc}", 500)

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        try:
            if path == "/api/info":
                self.handle_info()
            elif path == "/api/jobs":
                self.handle_create()
            else:
                self._fail("not found", 404)
        except UsageError as exc:
            self._fail(str(exc), 400)
        except TtdwnError as exc:
            self._fail(str(exc), 422)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            self._fail(f"{type(exc).__name__}: {exc}", 500)

    # -- api ----------------------------------------------------------
    def handle_info(self) -> None:
        data = self._body()
        url = str(data.get("url") or "").strip()
        if not url:
            raise UsageError("paste a TikTok link first")
        if len(url) > 2048:
            raise UsageError("that link is suspiciously long")
        media = self.jobs.inspect(url)
        formats = self.jobs.formats(media)
        payload = {
            "ok": True,
            "media": {
                "id": media.id,
                "url": media.permalink,
                "author": media.author,
                "nickname": media.nickname,
                "desc": media.desc,
                "duration": round(media.duration, 3),
                "created": media.created_iso,
                "stats": media.stats,
                "sources": media.sources,
                "music": (media.music or {}).get("title") or "",
                "photos": len(media.image_urls),
            },
            "thumb": ("/api/image?u=" + urllib.parse.quote(media.cover_urls[0], safe=""))
            if media.cover_urls else "",
            "formats": [self.format_dict(i, fmt, media)
                        for i, fmt in enumerate(formats, 1)],
            "note": "" if formats else
                    "this post is a photo carousel — the CLI fetches the slides (ttdwn --images)",
        }
        self._json(payload)

    def format_dict(self, index: int, fmt: Format, media: Media) -> dict[str, Any]:
        return {
            "index": index,
            "quality": fmt.quality,
            "resolution": fmt.resolution,
            "width": fmt.width,
            "height": fmt.height,
            "bitrate": fmt.bitrate,
            "codec": fmt.codec,
            "size": fmt.size,
            "watermark": fmt.watermarked,
            "preview": "/api/preview?" + urllib.parse.urlencode(
                {"u": media.permalink or media.id, "i": index}),
        }

    def handle_create(self) -> None:
        data = self._body()
        url = str(data.get("url") or "").strip()
        if not url:
            raise UsageError("paste a TikTok link first")
        try:
            index = int(data.get("index") or 1)
        except (TypeError, ValueError):
            raise UsageError("index must be a number") from None
        if not 1 <= index <= 50:
            raise UsageError("index out of range")
        job = self.jobs.create(url, index)
        self._json({"ok": True, "job": job.as_dict()})

    def handle_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            self._fail("unknown job", 404)
            return
        self._json({"ok": True, "job": job.as_dict()})

    def handle_file(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.path is None or not job.path.exists():
            self._fail("file is not ready", 404)
            return
        path = job.path
        size = path.stat().st_size
        ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", job.filename) or "tiktok.mp4"
        disposition = ("attachment; filename=\"%s\"; filename*=UTF-8''%s"
                       % (ascii_name, urllib.parse.quote(job.filename)))
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- proxies ------------------------------------------------------
    @staticmethod
    def _allowed(url: str) -> bool:
        try:
            host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
        except ValueError:
            return False
        return any(host == s or host.endswith("." + s) for s in CDN_SUFFIXES)

    def handle_image(self) -> None:
        url = self._query().get("u", "")
        if not url or not self._allowed(url):
            self._fail("image host not allowed", 403)
            return
        session = self.jobs.new_session()
        with session.get(url, headers=CDN_HEADERS, retries=0) as resp:
            data = resp.read()
            ctype = resp.header("Content-Type") or "image/jpeg"
        self._send(200, data, ctype, {"Cache-Control": "max-age=3600"})

    def handle_preview(self) -> None:
        query = self._query()
        url = query.get("u", "")
        if not url:
            self._fail("missing u", 400)
            return
        try:
            index = int(query.get("i") or 1)
        except ValueError:
            index = 1
        media, session = self.jobs.resolve(url)
        fmt = self.jobs.format_at(media, index)
        if not fmt.urls:
            self._fail("no stream for that rendition", 404)
            return
        headers = dict(CDN_HEADERS)
        if self.headers.get("Range"):
            headers["Range"] = str(self.headers["Range"])
        resp = None
        errors: list[str] = []
        for target in fmt.urls:  # mirrors: first one that answers wins
            candidate = session.request("GET", target, headers=headers, retries=0,
                                        allow_status=(200, 206, 403, 404, 410))
            if candidate.status in (403, 404, 410):
                candidate.close()
                errors.append(f"HTTP {candidate.status}")
                continue
            resp = candidate
            break
        if resp is None:
            self._fail("stream unavailable: " + ", ".join(dict.fromkeys(errors)), 502)
            return
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", "video/mp4")
            for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
                value = resp.header(header)
                if value:
                    self.send_header(header, value)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            for chunk in resp.iter_chunks(128 * 1024):
                self.wfile.write(chunk)
        finally:
            resp.close()


# ---------------------------------------------------------------------------
# serving
# ---------------------------------------------------------------------------

def serve(options: Options, *, open_browser: bool = False) -> int:
    web = WebUI(options)
    httpd = web.make_server()
    from .ui import C, set_color

    url = web.url
    if options.initial:
        url += "?u=" + urllib.parse.quote(options.initial, safe="")
    line = f"{C.paint('ttdwn', C.bold)} {C.paint(__version__, C.dim)}"
    print(f"{line}  {C.paint('web interface', C.dim)}", flush=True)
    print("", flush=True)
    print(f"  {C.paint('listening', C.dim)}  {url}", flush=True)
    print(f"  {C.paint('saving to', C.dim)}  {options.out_dir}", flush=True)
    if options.host not in ("127.0.0.1", "localhost"):
        print(f"  {C.paint('!', C.dim)} reachable from other machines on this network", flush=True)
    print(f"  {C.paint('stop with', C.dim)} Ctrl+C", flush=True)
    print("", flush=True)
    del set_color

    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("", flush=True)
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ttdwn-web", description="ttdwn web interface")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765, 0 = any free)")
    parser.add_argument("-o", "--output", default=".", help="where files are saved")
    parser.add_argument("--device", default="pixel6a", help="Android profile for the mobile API")
    parser.add_argument("-f", "--filename", default="{author}_{id}_{quality}", help="filename template")
    parser.add_argument("--cookies", help="Netscape cookie file")
    parser.add_argument("--proxy", help="proxy URL")
    parser.add_argument("--open", action="store_true", help="open the browser")
    args = parser.parse_args(argv)
    options = Options(
        host=args.host, port=args.port, out_dir=Path(args.output).expanduser().resolve(),
        device=args.device, template=args.filename, cookies=args.cookies, proxy=args.proxy,
    )
    options.out_dir.mkdir(parents=True, exist_ok=True)
    return serve(options, open_browser=args.open)


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" fill="#111"/>'
    b'<path d="M4 5h8M8 5v7" stroke="#fff" stroke-width="1.6" fill="none"/></svg>'
)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ttdwn</title>
<style>
  :root {
    --bg: #fbfbfa; --panel: #ffffff; --ink: #111111; --muted: #6f6f6f;
    --rule: #e3e1dc; --rule-strong: #c9c6bf; --accent: #111111; --on-accent: #ffffff;
    --bad: #a1271f; --good: #1f6b3a; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0e0e0e; --panel: #141414; --ink: #ededed; --muted: #8d8d8d;
      --rule: #262626; --rule-strong: #3a3a3a; --accent: #ededed; --on-accent: #0e0e0e;
      --bad: #e2756b; --good: #7fc79b;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); }
  body {
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    padding: 40px 24px 80px;
  }
  main { max-width: 720px; margin: 0 auto; }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
  .brand { font-size: 17px; letter-spacing: -0.01em; }
  .brand b { font-weight: 650; }
  .brand span { color: var(--muted); }
  .tag { font-size: 12px; color: var(--muted); }
  hr { border: 0; border-top: 1px solid var(--rule); margin: 20px 0; }
  form.search { display: flex; gap: 8px; }
  input[type=text] {
    flex: 1; min-width: 0; padding: 11px 12px; font: inherit; color: var(--ink);
    background: var(--panel); border: 1px solid var(--rule-strong); border-radius: 2px;
    font-family: var(--mono); font-size: 13.5px;
  }
  input[type=text]:focus { outline: 2px solid var(--ink); outline-offset: -1px; }
  input[type=text]::placeholder { color: var(--muted); }
  button {
    font: inherit; cursor: pointer; border-radius: 2px; border: 1px solid var(--accent);
    background: var(--accent); color: var(--on-accent); padding: 11px 18px; font-weight: 550;
    transition: opacity .12s ease;
  }
  button:hover { opacity: .85; }
  button:disabled { opacity: .45; cursor: default; }
  button.ghost { background: transparent; color: var(--ink); border-color: var(--rule-strong); }
  .hint { color: var(--muted); font-size: 12.5px; margin: 10px 0 0; }
  section { margin-top: 28px; }
  .post { display: flex; gap: 14px; align-items: flex-start; }
  .cover {
    width: 56px; height: 74px; object-fit: cover; border: 1px solid var(--rule);
    border-radius: 2px; background: var(--panel); flex: 0 0 auto;
  }
  .cover.empty { display: grid; place-items: center; color: var(--muted); font-size: 11px; }
  .who { font-weight: 600; }
  .who .nick { color: var(--muted); font-weight: 400; margin-left: 8px; font-size: 13.5px; }
  .desc { margin: 3px 0 0; }
  .meta { color: var(--muted); font-size: 12.5px; margin-top: 5px; font-variant-numeric: tabular-nums; }
  table.ladder { width: 100%; border-collapse: collapse; margin-top: 18px; font-variant-numeric: tabular-nums; }
  table.ladder th {
    text-align: left; font-weight: 500; font-size: 11.5px; letter-spacing: .06em;
    text-transform: uppercase; color: var(--muted); padding: 0 8px 7px; border-bottom: 1px solid var(--rule-strong);
  }
  table.ladder th.num, table.ladder td.num { text-align: right; }
  table.ladder td { padding: 9px 8px; border-bottom: 1px solid var(--rule); font-size: 14px; }
  table.ladder tr.row { cursor: pointer; }
  table.ladder tr.row:hover td { background: color-mix(in srgb, var(--ink) 4%, transparent); }
  table.ladder tr.sel td { font-weight: 600; }
  table.ladder tr.sel td:first-child::before { content: "→ "; color: var(--muted); }
  table.ladder td.dim { color: var(--muted); font-weight: 400; }
  .actions { display: flex; gap: 10px; align-items: center; margin-top: 18px; flex-wrap: wrap; }
  .bar { height: 6px; background: var(--rule); border-radius: 3px; overflow: hidden; margin-bottom: 10px; }
  .bar > i { display: block; height: 100%; width: 0; background: var(--ink); transition: width .18s linear; }
  .stats { display: flex; justify-content: space-between; gap: 12px; color: var(--muted);
           font-size: 12.5px; font-variant-numeric: tabular-nums; font-family: var(--mono); }
  .stats b { color: var(--ink); font-weight: 600; }
  .note { color: var(--muted); font-size: 13px; }
  .error { color: var(--bad); }
  .error::before { content: "× "; }
  .done-title { font-weight: 600; }
  .file { font-family: var(--mono); font-size: 13px; color: var(--muted); word-break: break-all; }
  video { width: 100%; margin-top: 16px; border: 1px solid var(--rule); border-radius: 2px; background: #000; }
  footer { margin-top: 48px; color: var(--muted); font-size: 12px; }
  footer .badge { font-family: var(--mono); }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <header>
    <div class="brand"><b>ttdwn</b> <span>web interface</span></div>
    <div class="tag">no watermark · best quality</div>
  </header>

  <hr>

  <form class="search" id="form" autocomplete="off">
    <input type="text" id="url" placeholder="https://www.tiktok.com/@user/video/…" spellcheck="false" autofocus>
    <button type="submit" id="fetch">Fetch</button>
  </form>
  <p class="hint">full link, short link or bare 19-digit id. quote links that contain <span class="badge">&amp;</span> when using cmd.exe.</p>

  <section id="error" hidden><span class="error" id="error-text"></span></section>

  <section id="post" hidden>
    <div class="post">
      <img id="cover" class="cover" alt="">
      <div>
        <div class="who"><span id="author">@</span><span class="nick" id="nick"></span></div>
        <p class="desc" id="desc"></p>
        <div class="meta" id="meta"></div>
      </div>
    </div>

    <table class="ladder">
      <thead>
        <tr>
          <th style="width:38px">#</th>
          <th>quality</th>
          <th>resolution</th>
          <th>codec</th>
          <th class="num">bitrate</th>
          <th class="num">size</th>
        </tr>
      </thead>
      <tbody id="ladder"></tbody>
    </table>

    <p class="note" id="note" hidden></p>

    <div class="actions">
      <button type="button" id="download">Download</button>
      <button type="button" class="ghost" id="preview">Preview</button>
      <span class="note" id="picked"></span>
    </div>

    <video id="player" controls playsinline hidden></video>
  </section>

  <section id="transfer" hidden>
    <div class="bar"><i id="bar"></i></div>
    <div class="stats">
      <span><b id="pct">0%</b> <span id="phase">starting</span></span>
      <span id="bytes"></span>
      <span id="speed"></span>
    </div>
    <div class="actions" id="result" hidden>
      <a id="save" href="#"><button type="button">Save file</button></a>
      <button type="button" class="ghost" id="again">Download again</button>
      <span class="file" id="saved-name"></span>
    </div>
  </section>

  <footer>
    files are written to the server's output directory · <span class="badge">ttdwn</span>
  </footer>
</main>

<script>
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { url: "", formats: [], selected: 1, job: null, timer: null };

  const human = (n) => {
    if (n === null || n === undefined) return "—";
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB"];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + " " + units[i];
  };
  const dur = (s) => {
    if (!s && s !== 0) return "";
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60), r = s % 60;
    return m + ":" + String(r).padStart(2, "0");
  };
  const count = (n) => {
    if (!n) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return String(n);
  };
  const show = (el, on) => { el.hidden = !on; };

  function fail(message) {
    $("error-text").textContent = message;
    show($("error"), true);
  }
  function clearError() { show($("error"), false); }

  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || ("request failed (" + res.status + ")"));
    }
    return data;
  }

  function renderPost(media, formats, thumb, note) {
    $("author").textContent = media.author ? "@" + media.author : "@unknown";
    $("nick").textContent = media.nickname && media.nickname !== media.author ? media.nickname : "";
    $("desc").textContent = media.desc || "";
    show($("desc"), !!media.desc);

    const bits = [];
    if (media.duration) bits.push(dur(media.duration));
    if (media.created) bits.push(media.created.replace(" UTC", ""));
    if (media.stats && media.stats.play_count) bits.push(count(media.stats.play_count) + " plays");
    if (media.stats && media.stats.like_count) bits.push(count(media.stats.like_count) + " likes");
    if (media.sources && media.sources.length) bits.push("via " + media.sources.join("+"));
    $("meta").textContent = bits.join(" · ");

    const cover = $("cover");
    if (thumb) { cover.src = thumb; cover.classList.remove("empty"); }
    else { cover.removeAttribute("src"); cover.classList.add("empty"); }

    const tbody = $("ladder");
    tbody.innerHTML = "";
    formats.forEach((f) => {
      const tr = document.createElement("tr");
      tr.className = "row" + (f.index === state.selected ? " sel" : "");
      tr.dataset.index = f.index;
      tr.innerHTML =
        "<td>" + f.index + "</td>" +
        "<td>" + f.quality + (f.watermark ? " *" : "") + "</td>" +
        "<td class='dim'>" + f.resolution + "</td>" +
        "<td class='dim'>" + f.codec + "</td>" +
        "<td class='num dim'>" + (f.bitrate ? Math.round(f.bitrate / 1000) + " kbps" : "—") + "</td>" +
        "<td class='num'>" + (f.size ? human(f.size) : "—") + "</td>";
      tr.addEventListener("click", () => select(f.index));
      tbody.appendChild(tr);
    });

    $("note").textContent = note || "";
    show($("note"), !!note);
    updatePicked();
    show($("post"), true);
  }

  function select(index) {
    state.selected = index;
    [...document.querySelectorAll("#ladder tr")].forEach((tr) => {
      tr.classList.toggle("sel", Number(tr.dataset.index) === index);
    });
    updatePicked();
    stopPreview();
  }

  function currentFormat() {
    return state.formats.find((f) => f.index === state.selected) || state.formats[0];
  }

  function updatePicked() {
    const f = currentFormat();
    if (!f) { $("picked").textContent = ""; return; }
    const bits = [f.quality, f.resolution, f.codec, f.size ? human(f.size) : ""].filter(Boolean);
    $("picked").textContent = bits.join(" · ");
    $("download").disabled = false;
  }

  function stopPreview() {
    const p = $("player");
    p.pause();
    p.removeAttribute("src");
    p.load();
    show(p, false);
    $("preview").textContent = "Preview";
  }

  // -- actions ------------------------------------------------------
  $("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("url").value.trim();
    if (!url) { fail("paste a link first"); return; }
    clearError();
    stopPreview();
    show($("transfer"), false);
    $("fetch").disabled = true;
    $("fetch").textContent = "Fetching…";
    try {
      const data = await api("/api/info", { url });
      state.url = url;
      state.formats = data.formats || [];
      state.selected = 1;
      renderPost(data.media, state.formats, data.thumb, data.note);
      if (!state.formats.length) {
        $("download").disabled = true;
        fail(data.note || "no downloadable rendition in this post");
      }
    } catch (error) {
      show($("post"), false);
      fail(error.message);
    } finally {
      $("fetch").disabled = false;
      $("fetch").textContent = "Fetch";
    }
  });

  $("download").addEventListener("click", async () => {
    clearError();
    stopPreview();
    $("download").disabled = true;
    show($("result"), false);
    $("bar").style.width = "0%";
    $("pct").textContent = "0%";
    $("phase").textContent = "queued";
    $("bytes").textContent = "";
    $("speed").textContent = "";
    show($("transfer"), true);
    try {
      const data = await api("/api/jobs", { url: state.url, index: state.selected });
      state.job = data.job.id;
      poll();
    } catch (error) {
      $("download").disabled = false;
      show($("transfer"), false);
      fail(error.message);
    }
  });

  $("preview").addEventListener("click", () => {
    const f = currentFormat();
    if (!f) return;
    const p = $("player");
    if (!p.hidden) { stopPreview(); return; }
    p.src = f.preview;
    show(p, true);
    $("preview").textContent = "Hide preview";
    p.play().catch(() => {});
  });

  $("again").addEventListener("click", () => {
    show($("result"), false);
    $("download").disabled = false;
    $("download").click();
  });

  function poll() {
    clearTimeout(state.timer);
    const tick = async () => {
      let data;
      try {
        data = await api("/api/jobs/" + state.job);
      } catch (error) {
        $("download").disabled = false;
        fail(error.message);
        return;
      }
      const job = data.job;
      const pct = job.fraction === null ? 0 : Math.round(job.fraction * 100);
      $("bar").style.width = pct + "%";
      $("pct").textContent = job.status === "done" ? "100%" : pct + "%";
      $("phase").textContent =
        job.status === "queued" ? "queued" :
        job.status === "working" ? (job.total ? "downloading" : "resolving") :
        job.status === "done" ? "saved" : "failed";
      $("bytes").textContent = job.total
        ? human(job.done) + " / " + human(job.total)
        : human(job.done);
      $("speed").textContent = job.speed > 0 && job.status === "working"
        ? human(job.speed) + "/s"
        : (job.status === "done" ? job.elapsed.toFixed(1) + "s" : "");

      if (job.status === "done") {
        $("save").href = job.file_url;
        $("saved-name").textContent = job.filename + " · " + human(job.size);
        show($("result"), true);
        $("download").disabled = false;
        $("bar").style.width = "100%";
        state.job = null;
        return;
      }
      if (job.status === "error") {
        $("download").disabled = false;
        show($("transfer"), false);
        fail(job.error || "download failed");
        state.job = null;
        return;
      }
      state.timer = setTimeout(tick, 350);
    };
    tick();
  }

  // ?u=<link> pre-fills and submits, so "ttdwn --webui <url>" lands ready to go
  (function boot() {
    const preset = new URLSearchParams(location.search).get("u");
    if (preset) {
      $("url").value = preset;
      $("form").dispatchEvent(new Event("submit", { cancelable: true }));
    }
  })();

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { stopPreview(); clearError(); }
  });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
