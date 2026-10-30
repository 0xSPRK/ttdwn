"""Exception hierarchy. Every failure the user can act on is a TtdwnError."""

from __future__ import annotations


class TtdwnError(Exception):
    """Base class for all expected failures."""

    exit_code = 1


class UsageError(TtdwnError):
    """Bad arguments / bad input from the user."""

    exit_code = 2


class ResolveError(TtdwnError):
    """The URL or bare id could not be turned into an aweme id."""

    exit_code = 3


class ExtractionError(TtdwnError):
    """TikTok answered, but we could not get media information out of it."""

    exit_code = 4


class DownloadError(TtdwnError):
    """Bytes did not make it to disk."""

    exit_code = 5


class URLExpired(DownloadError):
    """Signed CDN link is stale — the caller should re-extract and retry."""


class PostProcessError(TtdwnError):
    """ffmpeg is missing or refused the job."""

    exit_code = 6
