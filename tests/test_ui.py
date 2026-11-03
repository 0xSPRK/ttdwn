"""Terminal output state."""

from __future__ import annotations

from ttdwn import ui
from ttdwn.cli import C as cli_color


def test_set_color_updates_the_object_every_module_imported():
    previous = ui.C.enabled
    try:
        ui.set_color(False)
        assert ui.C is cli_color
        assert cli_color.enabled is False
        assert cli_color.bold == ""

        ui.set_color(True)
        assert cli_color.enabled is True
        assert cli_color.bold == "\x1b[1m"
    finally:
        ui.set_color(previous)
