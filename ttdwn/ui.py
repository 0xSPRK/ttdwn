"""Terminal output. Monochrome on purpose: weight, dim and a single red.

Nothing here writes to stdout unless the caller asks for it, so ``--json``
stays machine readable.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Iterable, Sequence

__all__ = [
    "Color",
    "supports_color",
    "hrule",
    "banner",
    "info",
    "warn",
    "error",
    "ok",
    "format_bytes",
    "format_duration",
    "format_eta",
    "Progress",
    "table",
]

_RULE = "─"


class Color:
    """ANSI codes, degraded to empty strings when colour is off."""

    def __init__(self, enabled: bool) -> None:
        self.set(enabled)

    def set(self, enabled: bool) -> None:
        """Reconfigure in place so every module's ``C`` reference stays valid."""
        self.enabled = enabled
        c = self._c
        self.reset = c("0")
        self.bold = c("1")
        self.dim = c("2")
        self.italic = c("3")
        self.red = c("31")
        self.green = c("32")
        self.yellow = c("33")
        self.inverse = c("7")
        self.strike = c("9")

    def _c(self, code: str) -> str:
        return f"\x1b[{code}m" if self.enabled else ""

    def paint(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return "".join(styles) + text + self.reset


def supports_color(stream=None) -> bool:
    stream = stream or sys.stderr
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


C = Color(supports_color())


def set_color(enabled: bool) -> None:
    C.set(enabled)


def term_width(default: int = 78, maximum: int = 100) -> int:
    try:
        width = shutil.get_terminal_size((default, 24)).columns
    except Exception:
        width = default
    return max(40, min(width, maximum))


def hrule(width: int | None = None) -> str:
    return C.paint(_RULE * (width or term_width()), C.dim)


def banner(version: str) -> str:
    tag = "no watermark · high quality"
    return (
        f"{C.paint('ttdwn', C.bold)} {C.paint(version, C.dim)}"
        f"{C.paint('  ·  ', C.dim)}{C.paint(tag, C.dim)}"
    )


def _emit(stream, text: str) -> None:
    try:
        print(text, file=stream, flush=True)
    except (BrokenPipeError, UnicodeEncodeError):
        pass


def info(text: str, *, prefix: str = "·") -> None:
    _emit(sys.stderr, f"{C.paint(prefix, C.dim)} {text}")


def warn(text: str) -> None:
    _emit(sys.stderr, f"{C.paint('!', C.yellow)} {text}")


def error(text: str) -> None:
    _emit(sys.stderr, f"{C.paint('×', C.red)} {text}")


def ok(text: str) -> None:
    _emit(sys.stderr, f"{C.paint('✓', C.dim)} {text}")


def format_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m:d}:{s:02d}" if m < 60 else format_duration(seconds)


class Progress:
    """One line, rewritten in place. Silent when not a tty."""

    def __init__(self, total: int | None, *, label: str = "", stream=None,
                 enabled: bool | None = None, width: int | None = None) -> None:
        self.total = total if total and total > 0 else None
        self.done = 0
        self.label = label
        self.stream = stream or sys.stderr
        if enabled is None:
            enabled = supports_color(self.stream) and not os.environ.get("TTDWN_PLAIN")
        self.enabled = enabled
        self.width = width or term_width()
        self._t0 = time.monotonic()
        self._last_render = 0.0
        self._last_bytes = 0
        self._last_t = self._t0
        self.speed = 0.0
        self._finished = False

    # -- lifecycle ---------------------------------------------------
    def start(self) -> "Progress":
        self.render(force=True)
        return self

    def update(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last_render >= 0.08:
            self.render()

    def advance_to(self, n: int) -> None:
        self.done = n
        now = time.monotonic()
        if now - self._last_render >= 0.08:
            self.render()

    def finish(self, label: str | None = None) -> None:
        if label is not None:
            self.label = label
        self.render(force=True, final=True)
        self._finished = True
        if self.enabled:
            _emit(self.stream, "")

    # -- rendering ---------------------------------------------------
    def _speed(self) -> float:
        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 0.35:
            inst = (self.done - self._last_bytes) / dt
            self.speed = inst if self.speed == 0 else (self.speed * 0.65 + inst * 0.35)
            self._last_bytes = self.done
            self._last_t = now
        if self.speed <= 0:
            elapsed = now - self._t0
            if elapsed > 0.05:
                return self.done / elapsed
        return self.speed

    def render(self, force: bool = False, final: bool = False) -> None:
        if not self.enabled:
            if not final:
                return
            self._render_plain(final=True)
            return
        self._last_render = time.monotonic()
        speed = self._speed()
        pct = (self.done / self.total) if self.total else None
        eta = None
        if pct is not None and speed > 0:
            eta = (self.total - self.done) / speed

        left = f"{int(pct * 100):3d}% " if pct is not None else "     "
        bar_w = max(10, self.width - 46)
        if pct is None:
            bar = _RULE * bar_w
        else:
            filled = int(bar_w * min(pct, 1.0))
            bar = "█" * filled + C.paint(_RULE * (bar_w - filled), C.dim)

        size = f"{format_bytes(self.done)}"
        if self.total:
            size += f"/{format_bytes(self.total)}"
        tail = f"{size}  {format_bytes(speed)}/s  ETA {format_eta(eta)}"
        label = f"{self.label} " if self.label else ""
        line = f"{C.paint(left, C.bold)}{bar}  {C.paint(tail, C.dim)}"
        try:
            self.stream.write("\r\x1b[2K" + label + line)
            self.stream.flush()
        except Exception:
            self.enabled = False

    def _render_plain(self, final: bool = False) -> None:
        if not final:
            return
        _emit(self.stream, f"{self.label}{format_bytes(self.done)}")


def table(rows: Sequence[Sequence[str]], *, headers: Sequence[str] | None = None,
          align: Sequence[str] | None = None, indent: str = "  ") -> str:
    """Minimal fixed-width table. Dim headers, no vertical rules."""
    cols = len(headers) if headers else len(rows[0])
    align = align or ["<"] * cols
    widths = [0] * cols
    for i, h in enumerate(headers or []):
        widths[i] = max(widths[i], len(h))
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    out: list[str] = []
    if headers:
        head = "  ".join(f"{h:{align[i]}{widths[i]}}" for i, h in enumerate(headers))
        out.append(indent + C.paint(head, C.dim))
        out.append(indent + C.paint(_RULE * (sum(widths) + 2 * (cols - 1)), C.dim))
    for row in rows:
        out.append(indent + "  ".join(f"{c:{align[i]}{widths[i]}}" for i, c in enumerate(row)))
    return "\n".join(out)


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bullet_list(items: Iterable[str], indent: str = "  ") -> str:
    return "\n".join(f"{indent}{C.paint('·', C.dim)} {i}" for i in items)
