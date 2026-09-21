#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Renders a .grc flowgraph to a PNG exactly as GNU Radio Companion draws it
(it IS GRC's own canvas and its own File > Screen Capture code), with no window.

  python grc_screenshot.py examples/loopback_qtgui.grc docs/img/grc_loopback_qtgui.png

GRC_BLOCKS_PATH must include this repo's grc/ directory so the rxtune blocks resolve."""
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk  # noqa: E402,F401

from gnuradio import gr  # noqa: E402
from gnuradio.grc.gui.Platform import Platform  # noqa: E402  (first: GRC's gui modules import in a circle)
from gnuradio.grc.gui import Utils  # noqa: E402


def main(grc_file, png):
    platform = Platform(version=gr.version(), version_parts=(gr.major_version(), gr.api_version(),
                                                             gr.minor_version()),
                        prefs=gr.prefs(), install_prefix=gr.prefix())
    platform.build_library()
    # GRC's canvas looks up the running Gtk.Application for its context menu;
    # give it one (never shown, never run).
    app = Gtk.Application(application_id="org.gnuradio.rxtune.screenshot")
    app.register(None)
    Gtk.Application.set_default(app)
    # ...and its right-click menu wants a main window to attach to. There is none.
    from gnuradio.grc.gui.canvas import flowgraph as canvas_flowgraph
    canvas_flowgraph._ContextMenu = lambda main_window: None
    fg = platform.make_flow_graph(grc_file)
    fg.rewrite()
    fg.validate()
    errors = fg.get_error_messages()
    for e in errors:
        print("flowgraph error:", e)
    fg.update()                      # lay out the canvas elements
    Utils.make_screenshot(fg, png, transparent_bg=False)
    print("wrote", png, "- valid" if not errors else "- INVALID")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
