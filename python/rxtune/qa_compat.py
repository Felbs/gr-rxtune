#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Compatibility with the stock SDR source blocks, as far as it can be tested
with no hardware attached.

  gr-osmosdr  REAL block (file source): it has no message port, so the route is
              the Message Setter; and its no-op setters show why readback matters.
  gr-uhd      cannot be constructed without a USRP (no null device): the dialect
              is checked against the command table in gr-uhd's own documentation.
  gr-soapy    SoapySDR's null device reports 0 channels, so soapy.source cannot be
              constructed on it. The REAL test runs on hardware: util/hw_soapy_cmd.py."""
import os
import tempfile
import time
import unittest

import numpy as np
import pmt
from gnuradio import gr, gr_unittest

from qa_common import rxtune, sink
from rxtune import commands

try:
    import osmosdr
except ImportError:
    osmosdr = None


class qa_compat(gr_unittest.TestCase):

    @unittest.skipIf(osmosdr is None, "gr-osmosdr is not installed")
    def test_001_osmosdr_source_has_no_message_port_so_use_the_setter(self):
        path = os.path.join(tempfile.gettempdir(), "rxtune_qa_iq.cf32")
        np.zeros(200000, np.complex64).tofile(path)
        src = osmosdr.source(args="numchan=1 file=%s,rate=1e6,freq=100e6,repeat=true,throttle=true"
                             % path.replace("\\", "/"))
        # GRC draws a 'command' port on this block. The block does not have one.
        self.assertEqual(pmt.length(src.message_ports_in()), 0)

        setter = rxtune.msg_setter(src, {"freq": lambda b, v: b.set_center_freq(v, 0),
                                         "gain": lambda b, v: b.set_gain(v, 0)},
                                   {"freq": lambda b: b.get_center_freq(0),
                                    "gain": lambda b: b.get_gain(0)})
        ctl = rxtune.controller(axes=[{"name": "gain", "lo": 0, "hi": 40, "step": 10, "sense": "gain"}],
                                dialect="generic", settle_s=0.05, window_s=0.1, fixed={"freq": 101e6})
        out = sink("verdict", "cmd")
        tb = gr.top_block()
        from gnuradio import blocks
        tb.connect(src, blocks.null_sink(gr.sizeof_gr_complex))
        tb.msg_connect(ctl, "cmd", setter, "cmd")
        tb.msg_connect(setter, "readback", ctl, "readback")
        tb.msg_connect(ctl, "verdict", out, "verdict")
        tb.msg_connect(ctl, "cmd", out, "cmd")
        tb.start()
        deadline = time.time() + 30
        while not out.got["verdict"] and time.time() < deadline:
            time.sleep(0.1)
        tb.stop()
        tb.wait()
        self.assertTrue(out.got["verdict"], "no verdict")
        v = pmt.to_python(out.got["verdict"][0])
        # The FILE source accepts set_center_freq(101e6) and stays at 100e6. Open-loop
        # that is invisible; with readback the controller says so.
        self.assertTrue(any("freq" in n and "read back" in n for n in v["notes"]), v["notes"])
        self.assertNotIn("closed-loop", v["confidence"].split("(")[0])
        self.assertEqual(v["shape"], "NO_SIGNAL")           # nothing ever reported a dial: said plainly

    def test_002_uhd_dialect_matches_the_documented_command_table(self):
        # gr-uhd docs/uhd.dox "Command Syntax": freq/gain/lo_offset/rate/bandwidth = double,
        # antenna = string, chan = int. Named gain elements are NOT settable by message.
        self.assertEqual(commands.command("uhd", "gain", 30), {"gain": 30.0})
        self.assertEqual(commands.command("uhd", "freq", 915e6, chan=1), {"freq": 915e6, "chan": 1})
        self.assertEqual(commands.command("uhd", "antenna", "RX2"), {"antenna": "RX2"})
        for body in (commands.command("uhd", "gain", 30), commands.command("uhd", "antenna", "RX2")):
            msg = pmt.to_pmt(body)
            self.assertTrue(pmt.is_dict(msg))              # usrp_block_impl accepts dict or pair
        self.assertTrue(pmt.is_real(pmt.dict_ref(pmt.to_pmt({"gain": 30.0}), pmt.intern("gain"), pmt.PMT_NIL)))
        with self.assertRaises(commands.Unsupported):
            commands.command("uhd", "gain:PGA", 10)

    def test_003_soapy_dialect_is_exactly_what_block_impl_cc_parses(self):
        # gr-soapy lib/block_impl.cc: dict ONLY; "gain" = number OR {name, gain};
        # "setting" = {key, value} where value MUST be a symbol (handler type-test bug).
        g = pmt.to_pmt(commands.command("soapy", "gain:IFGR", 40))
        inner = pmt.dict_ref(g, pmt.intern("gain"), pmt.PMT_NIL)
        self.assertTrue(pmt.is_dict(inner))
        self.assertEqual(pmt.symbol_to_string(pmt.dict_ref(inner, pmt.intern("name"), pmt.PMT_NIL)), "IFGR")
        self.assertTrue(pmt.is_real(pmt.dict_ref(inner, pmt.intern("gain"), pmt.PMT_NIL)))
        # by default this is REFUSED: on device-level-settings drivers it stops the source block
        with self.assertRaises(commands.Unsupported):
            commands.command("soapy", "setting:biasT_ctrl", True)
        with self.assertRaises(ValueError):
            rxtune.controller(axes=[{"name": "gain:IFGR", "lo": 20, "hi": 59, "step": 1}],
                              dialect="soapy", fixed={"setting:biasT_ctrl": "true"})
        s = pmt.to_pmt(commands.command("soapy", "setting:biasT_ctrl", True, allow_unsafe=True))
        val = pmt.dict_ref(pmt.dict_ref(s, pmt.intern("setting"), pmt.PMT_NIL), pmt.intern("value"), pmt.PMT_NIL)
        self.assertTrue(pmt.is_symbol(val))
        self.assertEqual(pmt.symbol_to_string(val), "true")
        a = pmt.to_pmt(commands.command("soapy", "antenna", "Antenna C"))
        self.assertTrue(pmt.is_symbol(pmt.dict_ref(a, pmt.intern("antenna"), pmt.PMT_NIL)))


if __name__ == '__main__':
    gr_unittest.run(qa_compat)
