#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Dashboard QA. Runs on the 'offscreen' Qt platform so it needs no display.

Proves: messages that arrive on SCHEDULER threads reach the widgets (through
signals, on the GUI thread); the dashboard lives in one layout with stock
gr-qtgui sinks in a flowgraph that is actually running; nothing deadlocks the
Qt event loop; shutdown is clean."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pmt
from gnuradio import analog, blocks, gr, gr_unittest, qtgui
from PyQt5 import Qt, QtCore, QtWidgets

try:
    import sip
except ImportError:
    from PyQt5 import sip

from qa_common import rxtune

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.01)


class qa_dashboard(gr_unittest.TestCase):

    def test_001_messages_cross_threads_into_the_widgets(self):
        dash = rxtune.dashboard(label="QA", units="dB", cliff=15.2, dial_max=30)
        tb = gr.top_block()
        src = blocks.message_strobe(pmt.cons(pmt.intern("MER"), pmt.from_double(17.25)), 50)
        tb.msg_connect(src, "strobe", dash, "dial")
        tb.start()
        seen_threads = set()
        orig = dash._show_dial

        def spy(v):
            seen_threads.add(threading.current_thread() is threading.main_thread())
            orig(v)
        dash.sig_dial.disconnect()
        dash.sig_dial.connect(spy)
        post = dash.to_basic_block()._post
        post(pmt.intern("status"), pmt.to_pmt({"state": "searching", "cell": 1, "reason": "cell done",
                                                "mode": "message (open-loop)", "value": 12.5,
                                                "setting": {"gain:RFGR": 3, "gain:IFGR": 40}}))
        post(pmt.intern("status"), pmt.to_pmt({"state": "searching", "cell": 2, "reason": "cell done",
                                                "mode": "message (open-loop)", "value": "none",
                                                "setting": {"gain:RFGR": 0, "gain:IFGR": 20}}))
        post(pmt.intern("verdict"), pmt.to_pmt({"shape": "HEALTHY", "headline": "MER 17.2 dB",
                                                 "rule": "peak above the cliff", "advice": "Use it.",
                                                 "notes": ["overload ridge: x"],
                                                 "best": {"gain:RFGR": 3, "gain:IFGR": 40}}))
        pump(1.0)
        tb.stop()
        tb.wait()
        self.assertEqual(seen_threads, {True}, "widget updates must run on the GUI thread")
        self.assertEqual(dash.big.text(), "17.2 dB")
        self.assertIn("2ecc71", dash.big.styleSheet())              # above the cliff = green
        self.assertEqual(dash.heat.cells, {(40.0, 3.0): 12.5, (20.0, 0.0): None})   # axes sorted by name
        self.assertEqual(dash.heat.best, (40.0, 3.0))
        self.assertEqual(dash.heat.axes, ("gain:IFGR", "gain:RFGR"))
        self.assertIn("HEALTHY", dash.verdict.text())
        self.assertIn("overload ridge", dash.verdict.text())
        dash.resize(520, 300)
        img = dash.grab().toImage()                                  # forces the heatmap paintEvent
        self.assertFalse(img.isNull())

    def test_002_docks_beside_stock_qtgui_sinks_in_a_running_flowgraph(self):
        win = QtWidgets.QWidget()
        grid = Qt.QGridLayout(win)                                   # what GRC's top_grid_layout is
        tb = gr.top_block()
        src = analog.sig_source_c(32000, analog.GR_COS_WAVE, 1000, 1.0)
        thr = blocks.throttle(gr.sizeof_gr_complex, 32000)
        tsink = qtgui.time_sink_c(1024, 32000, "time", 1, None)
        fsink = qtgui.freq_sink_c(1024, 5, 0, 32000, "freq", 1, None)
        nsink = qtgui.number_sink(gr.sizeof_float, 0, qtgui.NUM_GRAPH_HORIZ, 1, None)
        tb.connect(src, thr, tsink)
        tb.connect(thr, fsink)
        tb.connect(thr, blocks.complex_to_mag(), nsink)
        for i, s in enumerate((tsink, fsink, nsink)):
            grid.addWidget(sip.wrapinstance(s.qwidget(), Qt.QWidget), i, 0)
        dash = rxtune.dashboard(parent=win)
        grid.addWidget(dash, 0, 1, 3, 1)                             # ${gui_hint() % win} expands to this
        self.assertIs(dash.parent(), win)
        strobe = blocks.message_strobe(pmt.from_double(21.0), 20)
        tb.msg_connect(strobe, "strobe", dash, "dial")
        win.show()
        tb.start()
        t0 = time.time()
        pump(2.0)
        lag = time.time() - t0 - 2.0
        tb.stop()
        tb.wait()
        self.assertEqual(dash.big.text(), "21.0 dB")
        self.assertLess(lag, 1.0, "the Qt event loop stalled")
        win.close()


if __name__ == '__main__':
    gr_unittest.run(qa_dashboard)
