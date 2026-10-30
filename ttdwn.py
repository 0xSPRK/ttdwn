#!/usr/bin/env python3
"""Launcher so the tool runs without installation: ./ttdwn.py <url>"""

import sys

from ttdwn.cli import main

if __name__ == "__main__":
    sys.exit(main())
