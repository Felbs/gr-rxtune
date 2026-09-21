#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs a grcc-generated Qt flowgraph UNATTENDED and checks it: the way GRC's own
main() would, but with a timer instead of a person.

  python run_grc_qt.py build/grc/loopback_qtgui.py [--seconds 70] [--png out.png]

Checks: the flowgraph starts; the rxtune dashboard is a child of the GRC top
window and sits in its grid layout beside the stock gr-qtgui widgets; the Qt
event loop keeps turning (timer ticks arrive on time) while the controller
searches; a verdict arrives and is shown; QT GUI Range still drives its block;
shutdown is clean."""
import argparse
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "examples"))
import _devpath  # noqa: E402,F401

from PyQt5 import Qt, QtCore  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("module")
    ap.add_argument("--seconds", type=float, default=70.0)
    ap.add_argument("--png")
    a = ap.parse_args()

    spec = importlib.util.spec_from_file_location("fg", a.module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, os.path.splitext(os.path.basename(a.module))[0])

    app = Qt.QApplication(sys.argv[:1])
    tb = cls()
    tb.resize(1300, 950)
    tb.start()
    tb.show()

    t0 = time.time()
    ticks, worst = [t0], [0.0]
    result = {"verdict": None}

    def tick():
        now = time.time()
        worst[0] = max(worst[0], now - ticks[-1])
        ticks.append(now)
        text = tb.dash.verdict.text()
        if "no verdict yet" not in text and result["verdict"] is None:
            result["verdict"] = text
            QtCore.QTimer.singleShot(1500, finish)          # let the last paint happen
        elif now - t0 > a.seconds:
            finish()

    def finish():
        timer.stop()
        if a.png:
            tb.grab().save(a.png)
        tb.stop()
        tb.wait()
        app.quit()

    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(100)
    QtCore.QTimer.singleShot(3000, lambda: tb.set_neighbour_level(0.31))   # what the Range slider calls
    app.exec_()

    dash = tb.dash
    layout_items = [tb.top_grid_layout.itemAt(i).widget() for i in range(tb.top_grid_layout.count())]
    checks = {
        "dashboard is a child of the GRC top window": dash.window() is tb.window(),
        "dashboard sits in top_grid_layout": dash in layout_items,
        "stock qtgui widgets share that layout": len(layout_items) >= 5,
        "Qt event loop never stalled (worst gap < 1.0 s)": worst[0] < 1.0,
        "QT GUI Range callback reached its block": abs(tb.neighbour.amplitude() - 0.31) < 1e-6,
        "a verdict arrived and is displayed": result["verdict"] is not None,
        "heatmap has cells": len(dash.heat.cells) >= 5,
        "live dial is displayed": dash.big.text() != "--",
    }
    for k, v in checks.items():
        print(("PASS  " if v else "FAIL  ") + k)
    print(f"ran {time.time() - t0:.0f} s, worst event-loop gap {worst[0] * 1000:.0f} ms, "
          f"{len(layout_items)} widgets in the grid, {len(dash.heat.cells)} heatmap cells")
    print("verdict:", (result["verdict"] or "").replace("<br>", " | "))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
