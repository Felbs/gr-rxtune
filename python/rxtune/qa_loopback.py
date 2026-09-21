#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The loop closes over STOCK GNU Radio DSP, headless, no radio, no Qt.

Plant: QPSK mod -> weak signal + strong out-of-band neighbour -> Channel Model ->
gain (the knob) -> rail clip + converter noise -> RRC -> AGC -> Symbol Sync ->
M-PSK SNR probe. The dial peaks near +10 dB of gain: below it converter noise
wins, above it the neighbour hits the rail. See examples/headless_loopback.py."""
import sys
import threading

import pmt
from gnuradio import gr, gr_unittest

from qa_common import rxtune  # noqa: F401  (also puts examples/ on sys.path)
import headless_loopback as hl


class frame_counter(gr.basic_block):
    """TEST SCAFFOLD standing in for a decoder's frame counter: advances only
    while the dial is above the decode threshold."""

    def __init__(self, threshold, broken=False):
        gr.basic_block.__init__(self, name="frame_counter", in_sig=None, out_sig=None)
        self.threshold, self.broken, self.n = threshold, broken, 0
        self.message_port_register_in(pmt.intern("dial"))
        self.set_msg_handler(pmt.intern("dial"), self.on_dial)
        self.message_port_register_out(pmt.intern("liveness"))

    def on_dial(self, msg):
        if not self.broken and pmt.to_double(msg) >= self.threshold:
            self.n += 1
        self.message_port_pub(pmt.intern("liveness"), pmt.from_long(self.n))


def run(broken=False, timeout=240.0):
    tb = hl.loopback(cliff=20.0)
    fc = frame_counter(20.0, broken)
    tb.msg_connect(tb.probe, "dial", fc, "dial")
    tb.msg_connect(fc, "liveness", tb.ctl, "liveness")
    tb.start()
    ok = tb.catch.done.wait(timeout)
    tb.stop()
    tb.wait()
    return ok, tb


class qa_loopback(gr_unittest.TestCase):

    def test_001_closed_loop_finds_the_ridge(self):
        self.assertNotIn("PyQt5", sys.modules, "this test must be Qt-free")
        ok, tb = run()
        self.assertTrue(ok, "no verdict")
        v = tb.catch.verdict
        self.assertEqual(v["shape"], "HEALTHY", v)
        self.assertTrue(5 <= v["best"]["gain"] <= 11, v["best"])       # true optimum ~ +10 dB
        self.assertGreater(v["dial"], 25.0)
        self.assertTrue(any("overload ridge" in n for n in v["notes"]), v["notes"])
        # knob writes went out as generic commands and every one was read back
        self.assertTrue(all(set(c) == {"knob", "value"} for c in tb.catch.cmds))
        self.assertIn("closed-loop", v["confidence"])
        # the plant really is sitting on the winner
        self.assertAlmostEqual(abs(tb.gain.k()), 10 ** (v["best"]["gain"] / 20.0), places=3)
        self.assertNotIn("PyQt5", sys.modules)

    def test_002_same_rf_broken_decoder_is_refused(self):
        ok, tb = run(broken=True)
        self.assertTrue(ok)
        v = tb.catch.verdict
        self.assertEqual(v["shape"], "PLUMBING")
        self.assertEqual(v["best"], "none")


if __name__ == '__main__':
    gr_unittest.run(qa_loopback)
