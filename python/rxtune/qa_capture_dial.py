#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Capture Dial + Controller, no radio: record per cell, judge the FILE, close the
cell the moment the judgement arrives."""
import os
import tempfile
import time

import numpy as np
import pmt
from gnuradio import analog, blocks, gr, gr_unittest

from qa_common import rxtune, sink


def judges(best_level_db=-20.0):
    """A stand-in decoder: quality peaks when the recorded level is -20 dBFS, and
    it 'decodes' above 15."""
    def quality(path):
        iq = np.fromfile(path, np.int16).astype(np.float32) / 32767.0
        level = 20 * np.log10(max(np.sqrt(np.mean(iq * iq)), 1e-6))
        return 25.0 - abs(level - best_level_db)

    def score(path):
        q = quality(path)
        return {"value": q, "p10": q - 0.2, "p90": q + 0.2, "n": 12}

    def prove(path):
        q = quality(path)
        return {"alive": q >= 15.0, "count": 40 if q >= 15.0 else 0, "note": f"q={q:.1f}"}
    return score, prove


class qa_capture_dial(gr_unittest.TestCase):

    def test_001_record_judge_and_close_cells_early(self):
        score, prove = judges()
        path = os.path.join(tempfile.gettempdir(), "rxtune_qa_capture.cs16")
        tb = gr.top_block()
        src = analog.sig_source_c(200e3, analog.GR_COS_WAVE, 5e3, 1.0)
        thr = blocks.throttle(gr.sizeof_gr_complex, 200e3)
        gain = blocks.multiply_const_cc(1.0)
        cap = rxtune.capture_dial(samp_rate=200e3, secs=0.15, settle_s=0.05, path=path,
                                  score=score, prove=prove)
        ctl = rxtune.controller(axes=[{"name": "gain", "lo": -60, "hi": 0, "step": 2, "sense": "gain"}],
                                dialect="generic", dial_name="Q", cliff=15.0,
                                settle_s=0.05, window_s=30.0, min_samples=1)       # 30 s: never reached
        setter = rxtune.msg_setter(gain, {"gain": lambda b, db: b.set_k(10 ** (db / 20.0))},
                                   {"gain": lambda b: round(20 * np.log10(abs(b.k())), 6)})
        out = sink("verdict")
        tb.connect(src, thr, gain, cap)
        tb.msg_connect(ctl, "status", cap, "status")
        tb.msg_connect(cap, "dial", ctl, "dial")
        tb.msg_connect(cap, "liveness", ctl, "liveness")
        tb.msg_connect(ctl, "cmd", setter, "cmd")
        tb.msg_connect(setter, "readback", ctl, "readback")
        tb.msg_connect(ctl, "verdict", out, "verdict")
        t0 = time.time()
        tb.start()
        while not out.got["verdict"] and time.time() - t0 < 100:
            time.sleep(0.1)
        took = time.time() - t0
        tb.stop()
        tb.wait()
        self.assertTrue(out.got["verdict"], "no verdict")
        v = pmt.to_python(out.got["verdict"][0])
        self.assertEqual(v["shape"], "HEALTHY", v)
        # a unit-amplitude tone is -3 dBFS per rail... the judge wants -20 dBFS: gain ~ -17 dB
        self.assertTrue(-21 <= v["best"]["gain"] <= -13, v["best"])
        self.assertGreater(v["dial"], 23.0)
        self.assertGreaterEqual(cap.cells, 10)
        self.assertLess(took / cap.cells, 3.0, "cells did not close early (window is 30 s)")
        self.assertIn("closed-loop", v["confidence"])
        self.assertTrue(all(row["n"] in (0, 12) for row in v["curve"]))

    def test_002_a_superseded_cell_is_discarded(self):
        slow_done = []

        def score(path):
            time.sleep(0.6)
            slow_done.append(1)
            return {"value": 1.0, "n": 3}
        path = os.path.join(tempfile.gettempdir(), "rxtune_qa_capture2.cs16")
        tb = gr.top_block()
        src = analog.sig_source_c(100e3, analog.GR_COS_WAVE, 1e3, 0.5)
        cap = rxtune.capture_dial(samp_rate=100e3, secs=0.1, settle_s=0.0, path=path, score=score)
        out = sink("dial")
        tb.connect(src, blocks.throttle(gr.sizeof_gr_complex, 100e3), cap)
        tb.msg_connect(cap, "dial", out, "dial")
        tb.start()
        post = cap.to_basic_block()._post
        start = pmt.to_pmt({"reason": "cell start", "settle_s": 0.0, "window_scale": 1.0})
        post(pmt.intern("status"), start)
        time.sleep(0.4)                       # recorded, judging...
        post(pmt.intern("status"), start)     # ...and the controller moves on
        time.sleep(1.6)
        tb.stop()
        tb.wait()
        self.assertEqual(len(slow_done), 2)
        self.assertEqual(len(out.got["dial"]), 1, "the stale cell's result must not be published")


if __name__ == '__main__':
    gr_unittest.run(qa_capture_dial)
