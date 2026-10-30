"""Pydantic models for the web UI API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, HttpUrl, field_validator


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class DownloadRequest(BaseModel):
    url: HttpUrl = Field(..., description="TikTok video URL")
    quality: str | None = Field(default="best", description="Quality: best, worst, height (720, 1080p), or index")
    max_quality: str | None = Field(default=None, description="Never exceed this height")
    codec: str | None = Field(default=None, description="Preferred codec: h264 or h265")
    watermark: bool = Field(default=False, description="Allow watermarked renditions")
    audio: str | None = Field(default=None, description="Extract audio: mp3, m4a, opus, wav")
    audio_only: bool = Field(default=False, description="Keep only audio file")
    remux: bool = Field(default=False, description="Rewrite MP4 with +faststart")
    thumbnail: bool = Field(default=False, description="Save cover image")
    metadata: bool = Field(default=False, description="Save post metadata as JSON")
    music: bool = Field(default=False, description="Save original sound")
    images: bool | None = Field(default=None, description="Download photo carousel slides")

    @field_validator("url")
    @classmethod
    def validate_tiktok_url(cls, v: HttpUrl) -> HttpUrl:
        allowed_hosts = {
            "tiktok.com", "www.tiktok.com", "m.tiktok.com", "t.tiktok.com",
            "vt.tiktok.com", "vm.tiktok.com", "v.tiktok.com",
            "tiktokv.com", "www.tiktokv.com", "m.tiktokv.com",
            "tiktok.com.tr",
        }
        host = v.host.lower() if v.host else ""
        if host.startswith("www."):
            host = host[4:]
        if host not in allowed_hosts:
            raise ValueError("Only TikTok URLs are allowed")
        return v

    @field_validator("codec")
    @classmethod
    def validate_codec(cls, v: str | None) -> str | None:
        if v is not None and v not in ("h264", "h265"):
            raise ValueError("codec must be h264 or h265")
        return v

    @field_validator("audio")
    @classmethod
    def validate_audio(cls, v: str | None) -> str | None:
        if v is not None and v not in ("mp3", "m4a", "opus", "wav"):
            raise ValueError("audio must be mp3, m4a, opus, or wav")
        return v


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int = Field(default=0, ge=0, le=100)
    message: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class FileInfo(BaseModel):
    name: str
    size: int
    modified: datetime
    is_dir: bool = False


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"


class ErrorResponse(BaseModel):
    detail: str