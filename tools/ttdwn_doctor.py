#!/usr/bin/env python3
"""ttdwn doctor - find and fix source files that Python refuses to import.

A .py file saved as UTF-16 (or containing stray NUL bytes) fails with

    SyntaxError: source code string cannot contain null bytes

This script reports the encoding of every module in a ttdwn checkout (or in an
already-installed package) and, with --fix, rewrites the broken ones as clean
UTF-8.

    python ttdwn_doctor.py D:\\Projects\\ttdwn
    python ttdwn_doctor.py D:\\Projects\\ttdwn --fix
    python ttdwn_doctor.py --installed
    python ttdwn_doctor.py --installed --fix

Output is pure ASCII so it prints correctly in a Windows console with any
code page. MIT licensed, standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

MODULES = [
    "__init__.py",
    "__main__.py",
    "cli.py",
    "download.py",
    "errors.py",
    "extract.py",
    "ffmpeg.py",
    "formats.py",
    "http.py",
    "models.py",
    "resolve.py",
    "ui.py",
]

OK, BOM, UTF16, NULS, EMPTY, OTHER = "ok", "utf-8+bom", "utf-16", "nul-bytes", "empty", "unknown"
BAD = {UTF16, NULS, OTHER}


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def sniff(data: bytes) -> str:
    """Classify raw source bytes."""
    if not data:
        return EMPTY
    if data.startswith(b"\xef\xbb\xbf"):
        return BOM
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return UTF16
    if b"\x00" in data:
        # no BOM but NULs everywhere: UTF-16 written by a careless editor,
        # or a binary blob saved with a .py extension
        sample = data[:4096]
        even_nuls = sum(1 for i, b in enumerate(sample) if i % 2 == 0 and b == 0)
        odd_nuls = sum(1 for i, b in enumerate(sample) if i % 2 == 1 and b == 0)
        half = max(1, len(sample) // 2)
        if odd_nuls > half * 0.35 or even_nuls > half * 0.35:
            return UTF16
        return NULS
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return OTHER
    # a file that decodes but has no Python in it is suspicious, not broken
    if not any(ch.isprintable() for ch in text[:200]):
        return OTHER
    return OK


def convert(data: bytes, verdict: str) -> bytes:
    """Return clean UTF-8 bytes for a broken file."""
    if verdict == UTF16:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            # guess the endianness: which half contains the zero bytes?
            sample = data[:4096]
            even_nuls = sum(1 for i, b in enumerate(sample) if i % 2 == 0 and b == 0)
            odd_nuls = sum(1 for i, b in enumerate(sample) if i % 2 == 1 and b == 0)
            text = data.decode("utf-16-be" if even_nuls > odd_nuls else "utf-16-le", errors="replace")
        text = text.replace("\x00", "")
    elif verdict == NULS:
        text = data.replace(b"\x00", b"").decode("utf-8", errors="replace")
    elif verdict == BOM:
        text = data.decode("utf-8-sig")
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------

def package_dirs(path: str) -> list[str]:
    """Accept a checkout root, a package dir, or a single file's directory."""
    path = os.path.abspath(os.path.expanduser(path))
    candidates = [path, os.path.join(path, "ttdwn")]
    found: list[str] = []
    for candidate in candidates:
        if os.path.isdir(candidate) and any(
            os.path.isfile(os.path.join(candidate, m)) for m in MODULES
        ):
            found.append(candidate)
    if not found and os.path.isdir(path):
        found.append(path)
    return found


def installed_dirs() -> list[str]:
    """Locate the ttdwn package Python actually imports."""
    try:
        import ttdwn  # noqa: F401
    except SyntaxError as exc:
        print("[!] the installed package cannot even be imported: %s" % exc)
        print("    scanning sys.path for it instead")
    except ImportError:
        print("[!] ttdwn is not installed for this interpreter (%s)" % sys.executable)
        return []
    try:
        import ttdwn as pkg

        location = os.path.dirname(os.path.abspath(pkg.__file__))
        return [location]
    except SyntaxError:
        pass
    except Exception:
        pass
    # manual hunt: walk site-packages
    roots = [p for p in sys.path if p and os.path.isdir(p)]
    found: list[str] = []
    for root in roots:
        candidate = os.path.join(root, "ttdwn")
        if os.path.isdir(candidate) and candidate not in found:
            found.append(candidate)
    return found


def scan(directory: str, *, fix: bool, backup: bool) -> tuple[int, int]:
    names = sorted(n for n in os.listdir(directory) if n.endswith(".py"))
    if not names:
        print("[!] no .py files in %s" % directory)
        return 0, 0
    print("")
    print("  %-16s %9s  %-11s %s" % ("file", "bytes", "encoding", "verdict"))
    print("  " + "-" * 56)
    bad = 0
    fixed = 0
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        verdict = sniff(data)
        status = "clean"
        if verdict in BAD:
            bad += 1
            status = "BROKEN -> cannot be imported"
        elif verdict == BOM:
            status = "works, but the BOM is unnecessary"
        elif verdict == EMPTY:
            bad += 1
            status = "BROKEN -> empty file"
        print("  %-16s %9d  %-11s %s" % (name, len(data), verdict, status))
        if fix and verdict in BAD and verdict != EMPTY:
            clean = convert(data, verdict)
            if backup:
                shutil.copy2(path, path + ".bak")
            with open(path, "wb") as fh:
                fh.write(clean)
            fixed += 1
            print("  %-16s %9d  %-11s rewritten as UTF-8%s"
                  % ("  ->", len(clean), "utf-8", " (.bak kept)" if backup else ""))
    missing = [m for m in MODULES if not os.path.isfile(os.path.join(directory, m))]
    if missing:
        print("")
        print("  [!] missing modules: %s" % ", ".join(missing))
        print("      a partial copy explains a wheel that is far smaller than expected")
    return bad, fixed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect (and fix) ttdwn sources that Python cannot import.")
    parser.add_argument("path", nargs="?", help="project root, e.g. D:\\Projects\\ttdwn")
    parser.add_argument("--installed", action="store_true",
                        help="scan the installed package instead of a checkout")
    parser.add_argument("--fix", action="store_true", help="rewrite broken files as UTF-8")
    parser.add_argument("--backup", action="store_true", help="keep .bak copies when fixing")
    args = parser.parse_args(argv)

    if not args.installed and not args.path:
        parser.print_help()
        return 2

    dirs = installed_dirs() if args.installed else package_dirs(args.path)
    if not dirs:
        print("[!] nothing to scan")
        return 2

    total_bad = 0
    total_fixed = 0
    for directory in dirs:
        print("")
        print("scanning %s" % directory)
        bad, fixed = scan(directory, fix=args.fix, backup=args.backup)
        total_bad += bad
        total_fixed += fixed

    print("")
    if total_bad == 0:
        print("[ok] every file is valid UTF-8 and importable")
        return 0

    if total_fixed:
        print("[ok] %d file(s) rewritten as UTF-8" % total_fixed)
        if not args.installed:
            print("")
            print("now reinstall so the fixed sources are the ones Python runs:")
            print("    pip uninstall -y ttdwn")
            print("    pip install --force-reinstall --no-cache-dir .")
        return 0

    print("[!] %d broken file(s) found" % total_bad)
    print("")
    print("fix them, then reinstall:")
    print("    python ttdwn_doctor.py %s --fix" % (args.path or "<project dir>"))
    print("    pip uninstall -y ttdwn")
    print("    pip install --force-reinstall --no-cache-dir .")
    return 1


if __name__ == "__main__":
    sys.exit(main())
