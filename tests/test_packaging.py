"""Packaging hygiene: the class of bug that breaks ``pip install .`` silently.

A module saved as UTF-16 imports as

    SyntaxError: source code string cannot contain null bytes

with a traceback that does not even name the file, so it is worth catching in
CI before a wheel is built.
"""

from __future__ import annotations

import pathlib
import py_compile

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "ttdwn"
EXPECTED = {
    "__init__.py", "__main__.py", "cli.py", "download.py", "errors.py",
    "extract.py", "ffmpeg.py", "formats.py", "http.py", "models.py",
    "resolve.py", "ui.py",
}


def modules() -> list[pathlib.Path]:
    return sorted(PACKAGE.glob("*.py"))


def test_every_module_is_present():
    assert EXPECTED <= {p.name for p in modules()}, "a module went missing"


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_module_is_utf8_without_nul_bytes(path: pathlib.Path):
    raw = path.read_bytes()
    assert b"\x00" not in raw, f"{path.name} contains NUL bytes (saved as UTF-16?)"
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} starts with a UTF-8 BOM"
    raw.decode("utf-8")  # raises UnicodeDecodeError if it is not UTF-8


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_module_compiles(path: pathlib.Path, tmp_path: pathlib.Path):
    py_compile.compile(str(path), cfile=str(tmp_path / (path.stem + ".pyc")), doraise=True)


def test_no_carriage_returns():
    for path in modules():
        assert b"\r\n" not in path.read_bytes(), f"{path.name} has CRLF line endings"
