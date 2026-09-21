# SPDX-License-Identifier: GPL-3.0-or-later
"""Renders GRC's block-tree panel, filtered to rxtune, to a PNG (offscreen).
  python grc_block_tree_shot.py docs/img/grc_block_tree.png"""
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
from gnuradio import gr
from gnuradio.grc.gui.Platform import Platform
from gnuradio.grc.gui.BlockTreeWindow import BlockTreeWindow
platform = Platform(version=gr.version(), version_parts=(gr.major_version(), gr.api_version(), gr.minor_version()), prefs=gr.prefs(), install_prefix=gr.prefix())
platform.build_library()
app = Gtk.Application(application_id="org.gnuradio.rxtune.tree"); app.register(None); Gtk.Application.set_default(app)
tree = BlockTreeWindow(platform)
tree.repopulate()
tree.search_entry.set_text("rxtune")
try:
    tree._handle_search(tree.search_entry)
except Exception as e:
    print("search handler:", e)
tree.treeview.expand_all()
win = Gtk.OffscreenWindow(); win.set_default_size(330, 230); win.add(tree); win.show_all()
def grab():
    pb = win.get_pixbuf(); pb.savev(sys.argv[1], "png", [], []); print("wrote", sys.argv[1], pb.get_width(), pb.get_height()); Gtk.main_quit(); return False
GLib.timeout_add(1200, grab); Gtk.main()
