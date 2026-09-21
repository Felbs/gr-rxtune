#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Controller QA: message-only flowgraphs against a synthetic plant. Headless."""
import os
import sys
import threading
import time

import pmt
from gnuradio import gr, gr_unittest

try:
    from gnuradio import rxtune
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "examples"))
    import _devpath  # noqa: F401
    from gnuradio import rxtune


class plant(gr.basic_block):
    """A receiver made of messages: knob writes in (any dialect), dial + liveness out.
    Dial = 25 dB plateau between gains 8 and 20, falling both ways; cliff 15."""

    def __init__(self, decode=True, silent_above=None, rate_hz=60.0):
        gr.basic_block.__init__(self, name="plant", in_sig=None, out_sig=None)
        self.g = {"a": 0.0, "b": 0.0}
        self.cmds = []
        self.decode, self.silent_above, self.period = decode, silent_above, 1.0 / rate_hz
        self.live = 0
        self.message_port_register_in(pmt.intern("cmd"))
        self.set_msg_handler(pmt.intern("cmd"), self._on_cmd)
        self.message_port_register_out(pmt.intern("dial"))
        self.message_port_register_out(pmt.intern("liveness"))
        self._run = threading.Event()

    def _on_cmd(self, msg):
        d = pmt.to_python(msg)
        self.cmds.append(d)
        if "knob" in d:                                  # generic
            self.g[d["knob"].split(":")[-1]] = float(d["value"])
        elif isinstance(d.get("gain"), dict):            # soapy per-element
            self.g[d["gain"]["name"]] = float(d["gain"]["gain"])
        elif "gain" in d:                                # overall (uhd / soapy)
            self.g["a"] = float(d["gain"])

    def value(self):
        total = self.g["a"] + self.g["b"]
        if total < 8:
            return 25.0 - 2.0 * (8 - total)
        if total > 20:
            return 25.0 - 3.0 * (total - 20)
        return 25.0

    def start(self):
        self._run.set()
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def stop(self):
        self._run.clear()
        return True

    def _loop(self):
        while self._run.is_set():
            v = self.value()
            total = self.g["a"] + self.g["b"]
            if not (self.silent_above is not None and total > self.silent_above):
                self.message_port_pub(pmt.intern("dial"), pmt.from_double(v))
            if self.decode and v >= 15.0:
                self.live += 3
            self.message_port_pub(pmt.intern("liveness"), pmt.from_long(self.live))
            time.sleep(self.period)


class catcher(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, name="catcher", in_sig=None, out_sig=None)
        self.verdicts, self.status = [], []
        self.done = threading.Event()
        for port, fn in (("verdict", self._v), ("status", self._s)):
            self.message_port_register_in(pmt.intern(port))
            self.set_msg_handler(pmt.intern(port), fn)

    def _v(self, msg):
        self.verdicts.append(pmt.to_python(msg))
        self.done.set()

    def _s(self, msg):
        self.status.append(pmt.to_python(msg))


AX2 = [{"name": "gain:a", "lo": 0, "hi": 20, "step": 1, "sense": "gain"},
       {"name": "gain:b", "lo": 0, "hi": 20, "step": 1, "sense": "gain"}]
FAST = dict(settle_s=0.05, window_s=0.15, cliff=15.0, dial_name="MER")


def run(tb, catch, timeout=90.0):
    tb.start()
    ok = catch.done.wait(timeout)
    tb.stop()
    tb.wait()
    return ok


class qa_controller(gr_unittest.TestCase):

    def wire(self, ctl, pl, liveness=True):
        tb, c = gr.top_block(), catcher()
        tb.msg_connect(pl, "dial", ctl, "dial")
        if liveness:
            tb.msg_connect(pl, "liveness", ctl, "liveness")
        tb.msg_connect(ctl, "cmd", pl, "cmd")
        tb.msg_connect(ctl, "verdict", c, "verdict")
        tb.msg_connect(ctl, "status", c, "status")
        return tb, c

    def test_001_soapy_dialect_converges_and_sends_per_element_dicts(self):
        pl = plant()
        ctl = rxtune.controller(axes=AX2, dialect="soapy", **FAST)
        tb, c = self.wire(ctl, pl)
        self.assertTrue(run(tb, c))
        v = c.verdicts[0]
        self.assertEqual(v["shape"], "HEALTHY")
        self.assertTrue(8 <= v["best"]["gain:a"] + v["best"]["gain:b"] <= 20, v["best"])
        self.assertAlmostEqual(v["dial"], 25.0, delta=0.5)
        # every command is a DICT of the exact form gr-soapy's handler parses
        for d in pl.cmds:
            self.assertEqual(set(d), {"gain"})
            self.assertEqual(set(d["gain"]), {"name", "gain"})
            self.assertIsInstance(d["gain"]["gain"], float)
        # and the radio was left on the winner
        self.assertEqual(pl.g, {"a": float(v["best"]["gain:a"]), "b": float(v["best"]["gain:b"])})

    def test_002_liveness_gate_refuses_a_mirage(self):
        pl = plant(decode=False)
        ctl = rxtune.controller(axes=AX2, dialect="generic", **FAST)
        tb, c = self.wire(ctl, pl)
        self.assertTrue(run(tb, c))
        v = c.verdicts[0]
        self.assertEqual(v["shape"], "PLUMBING")
        self.assertEqual(v["best"], "none")
        self.assertIn("NOTHING DECODED", v["headline"])

    def test_003_no_liveness_wire_is_unproven(self):
        pl = plant()
        ctl = rxtune.controller(axes=AX2, dialect="generic", **FAST)
        tb, c = self.wire(ctl, pl, liveness=False)
        self.assertTrue(run(tb, c))
        self.assertEqual(c.verdicts[0]["shape"], "UNPROVEN")
        self.assertEqual(c.verdicts[0]["best"], "none")
        self.assertNotEqual(c.verdicts[0]["candidate"], "none")

    def test_004_a_silent_dial_still_ends_its_cell(self):
        """Above total gain 24 the plant says NOTHING. Windows must close on the
        wall clock, or the search would hang on the first silent cell."""
        pl = plant(silent_above=24)
        ctl = rxtune.controller(axes=AX2, dialect="generic", **FAST)
        tb, c = self.wire(ctl, pl)
        self.assertTrue(run(tb, c), "controller hung on a silent cell")
        self.assertEqual(c.verdicts[0]["shape"], "HEALTHY")
        silent = [row for row in c.verdicts[0]["curve"] if row["value"] == "none"]
        self.assertTrue(silent, "expected at least one silent cell in the curve")

    def test_005_uhd_dialect_overall_gain_only(self):
        pl = plant()
        ctl = rxtune.controller(axes=[{"name": "gain", "lo": 0, "hi": 40, "step": 1, "sense": "gain"}],
                                dialect="uhd", **FAST)
        tb, c = self.wire(ctl, pl)
        self.assertTrue(run(tb, c))
        self.assertTrue(all(set(d) == {"gain"} and isinstance(d["gain"], float) for d in pl.cmds))
        self.assertTrue(8 <= c.verdicts[0]["best"]["gain"] <= 20)
        # a NAMED element cannot be set by message on gr-uhd: refuse at construction,
        # not as an exception inside somebody else's message handler
        with self.assertRaises(ValueError):
            rxtune.controller(axes=AX2, dialect="uhd")

    def test_006_ctrl_recal_runs_a_second_search(self):
        pl = plant()
        ctl = rxtune.controller(axes=[{"name": "gain:a", "lo": 0, "hi": 20, "step": 2, "sense": "gain"}],
                                dialect="generic", **FAST)
        tb, c = self.wire(ctl, pl)
        tb.start()
        self.assertTrue(c.done.wait(60))
        c.done.clear()
        ctl.to_basic_block()._post(pmt.intern("ctrl"), pmt.to_pmt({"recal": True}))
        self.assertTrue(c.done.wait(60))
        tb.stop()
        tb.wait()
        self.assertEqual(len(c.verdicts), 2)
        self.assertTrue(any("recal" in s["reason"] for s in c.status))

    def test_007_status_reports_open_loop_mode(self):
        pl = plant()
        ctl = rxtune.controller(axes=[{"name": "gain:a", "lo": 0, "hi": 20, "step": 4, "sense": "gain"}],
                                dialect="generic", **FAST)
        tb, c = self.wire(ctl, pl)
        self.assertTrue(run(tb, c))
        self.assertTrue(all("open-loop" in s["mode"] for s in c.status))
        self.assertIn("open-loop", c.verdicts[0]["confidence"])


if __name__ == '__main__':
    gr_unittest.run(qa_controller)
