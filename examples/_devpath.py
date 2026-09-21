# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the examples and QA straight from the source tree, with nothing installed:
puts the pure-Python core on sys.path and grafts python/ onto the gnuradio
package so that `from gnuradio import rxtune` resolves here."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import gnuradio  # noqa: E402

_py = os.path.join(ROOT, "python")
if _py not in gnuradio.__path__:
    gnuradio.__path__.append(_py)
