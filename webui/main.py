"""FastAPI web UI for ttdwn."""

from __future__ import annotations

import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from ttdwn.cli import download_tiktok
from .models import (
    DownloadRequest,
    JobResponse,
    JobStatus,
    FileInfo,
    HealthResponse,
    ErrorResponse,
)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads")).expanduser().resolve()
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
WEBUI_USER = os.getenv("WEBUI_USER")
WEBUI_PASSWORD = os.getenv("WEBUI_PASSWORD")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory job store (sufficient for single-container use)
# ---------------------------------------------------------------------------
_jobs: dict[str, JobResponse] = {}
_jobs_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
    return _executor


def _create_job(request: DownloadRequest) -> JobResponse:
    job_id = uuid4().hex[:12]
    job = JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress=0,
        message="Queued",
    )
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _update_job(job_id: str, **kwargs: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            job = _jobs[job_id]
            for k, v in kwargs.items():
                setattr(job, k, v)
            job.updated_at = time.time()


def _run_download(job_id: str, request: DownloadRequest) -> None:
    _update_job(job_id, status=JobStatus.RUNNING, progress=10, message="Starting download...")

    def progress_callback(msg: str, pct: int | None = None) -> None:
        if pct is not None:
            _update_job(job_id, progress=min(10 + pct * 80 // 100, 90), message=msg)
        else:
            _update_job(job_id, message=msg)

    try:
        # Convert Pydantic model to kwargs for download_tiktok
        kwargs = request.model_dump(exclude_none=True)
        kwargs["output_dir"] = str(DOWNLOAD_DIR)
        kwargs["json_output"] = True
        kwargs["verbose"] = False

        result = download_tiktok(str(request.url), **kwargs)

        if result.get("ok"):
            _update_job(
                job_id,
                status=JobStatus.DONE,
                progress=100,
                message="Download complete",
                result=result,
            )
        else:
            _update_job(
                job_id,
                status=JobStatus.ERROR,
                progress=100,
                message="Download failed",
                error=result.get("error", "Unknown error"),
            )
    except Exception as exc:  # pragma: no cover
        _update_job(
            job_id,
            status=JobStatus.ERROR,
            progress=100,
            message="Download failed",
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Authentication middleware (HTTP Basic)
# ---------------------------------------------------------------------------
class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not WEBUI_USER or not WEBUI_PASSWORD:
            return await call_next(request)

        # Skip auth for health check and static files
        if request.url.path in ("/healthz", "/favicon.ico") or request.url.path.startswith("/static/"):
            return await call_next(request)

        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Basic "):
            return Response(
                content="Authentication required",
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="ttdwn Web UI"'},
            )

        import base64
        try:
            credentials = base64.b64decode(auth[6:]).decode("utf-8")
            user, password = credentials.split(":", 1)
            if not secrets.compare_digest(user, WEBUI_USER) or not secrets.compare_digest(password, WEBUI_PASSWORD):
                raise ValueError
        except Exception:
            return Response(
                content="Invalid credentials",
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="ttdwn Web UI"'},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _get_executor()
    yield
    # Shutdown
    if _executor:
        _executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ttdwn Web UI",
    description="Web interface for downloading TikTok videos",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(BasicAuthMiddleware)

# Static files
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/healthz", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>ttdwn Web UI</h1><p>Frontend not built.</p>")


@app.post("/api/download", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_download(request: DownloadRequest) -> JobResponse:
    job = _create_job(request)
    _get_executor().submit(_run_download, job.job_id, request)
    return job


@app.get("/api/jobs", response_model=list[JobResponse])
async def list_jobs() -> list[JobResponse]:
    with _jobs_lock:
        return list(_jobs.values())


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/files", response_model=list[FileInfo])
async def list_files() -> list[FileInfo]:
    files: list[FileInfo] = []
    try:
        for entry in sorted(DOWNLOAD_DIR.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
            stat = entry.stat()
            files.append(FileInfo(
                name=entry.name,
                size=stat.st_size,
                modified=time.ctime(stat.st_mtime),
                is_dir=entry.is_dir(),
            ))
    except Exception:
        pass
    return files


@app.get("/api/files/{name}")
async def download_file(name: str) -> FileResponse:
    # Prevent path traversal
    safe_name = Path(name).name
    file_path = (DOWNLOAD_DIR / safe_name).resolve()
    try:
        file_path.relative_to(DOWNLOAD_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path, filename=safe_name)


@app.delete("/api/files/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(name: str) -> Response:
    # Prevent path traversal
    safe_name = Path(name).name
    file_path = (DOWNLOAD_DIR / safe_name).resolve()
    try:
        file_path.relative_to(DOWNLOAD_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if file_path.is_dir():
        import shutil
        shutil.rmtree(file_path)
    else:
        file_path.unlink()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )