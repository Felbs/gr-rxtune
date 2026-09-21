#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs a grcc-generated Qt flowgraph that drives a REAL radio, unattended, until the
rxtune dashboard shows a verdict; saves window screenshots on the way.

  python run_hw_qt.py build/grc/atsc3_live.py --set freq=<Hz> --set antenna="<port>" \
      --set receiver_dir=/path/to/receiver --set py=/path/to/python \
      --png final.png --png-mid during.png

`--set` values become the flowgraph's GRC Parameters. Set RXTUNE_LOCK to a site lock
module if the radio is shared: the lock is held for the whole run and released after."""
import argparse
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "examples"))
import _devpath  # noqa: E402,F401

from PyQt5 import Qt, QtCore  # noqa: E402

from rxtune import lock  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("module")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--seconds", type=float, default=3600.0)
    ap.add_argument("--png")
    ap.add_argument("--png-mid", help="also save a shot once this many cells are in")
    ap.add_argument("--mid-cells", type=int, default=8)
    ap.add_argument("--size", default="1500x1000")
    a = ap.parse_args()
    kw = {}
    for item in a.set:
        k, v = item.split("=", 1)
        try:
            kw[k] = float(v)
        except ValueError:
            kw[k] = v

    spec = importlib.util.spec_from_file_location("fg", a.module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, os.path.splitext(os.path.basename(a.module))[0])

    with lock.from_env(owner="rxtune-grc", priority=60) as lk:
        app = Qt.QApplication(sys.argv[:1])
        tb = cls(**kw)
        w, h = (int(x) for x in a.size.split("x"))
        tb.resize(w, h)
        tb.start()
        tb.show()
        t0 = time.time()
        state = {"verdict": None, "mid": False}

        def finish():
            timer.stop()
            if a.png:
                tb.grab().save(a.png)
            tb.stop()
            tb.wait()
            app.quit()

        def tick():
            lk.heartbeat()
            cells = len(tb.dash.heat.cells)
            if a.png_mid and not state["mid"] and cells >= a.mid_cells:
                state["mid"] = True
                tb.grab().save(a.png_mid)
            text = tb.dash.verdict.text()
            if "no verdict yet" not in text and state["verdict"] is None:
                state["verdict"] = text
                QtCore.QTimer.singleShot(2500, finish)
            elif time.time() - t0 > a.seconds:
                finish()

        timer = QtCore.QTimer()
        timer.timeout.connect(tick)
        timer.start(500)
        app.exec_()
        cells = len(tb.dash.heat.cells)
    print(f"ran {time.time() - t0:.0f} s, {cells} cells")
    print("verdict:", (state["verdict"] or "NONE").replace("<br>", " | "))
    return 0 if state["verdict"] else 1


if __name__ == "__main__":
    sys.exit(main())
