#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
import math
import time

import pmt
from gnuradio import blocks, channels, gr, gr_unittest

from qa_common import rxtune, sink


class qa_msg_setter(gr_unittest.TestCase):

    def test_001_sets_stock_blocks_that_have_no_message_ports(self):
        chan = channels.channel_model(noise_voltage=0.0)
        self.assertEqual(pmt.length(chan.message_ports_in()), 0)      # the reason this block exists
        mult = blocks.multiply_const_cc(1.0)
        s1 = rxtune.msg_setter(chan, {"noise": "set_noise_voltage"}, {"noise": "noise_voltage"})
        s2 = rxtune.msg_setter(mult, {"gain": lambda b, db: b.set_k(10 ** (db / 20))},
                               {"gain": lambda b: 20 * math.log10(abs(b.k()))})
        out = sink("rb")
        tb = gr.top_block()
        tb.msg_connect(s1, "readback", out, "rb")
        tb.msg_connect(s2, "readback", out, "rb")
        tb.start()
        s1.to_basic_block()._post(pmt.intern("cmd"), pmt.to_pmt({"knob": "noise", "value": 0.25}))     # generic dialect
        s2.to_basic_block()._post(pmt.intern("cmd"), pmt.to_pmt({"gain": 20.0}))                       # plain dict
        s2.to_basic_block()._post(pmt.intern("cmd"), pmt.to_pmt({"knob": "nonsense", "value": 1}))
        time.sleep(0.5)
        tb.stop()
        tb.wait()
        # GNU Radio quirk: channel_model.noise_voltage() returns the value / sqrt(2)
        self.assertAlmostEqual(chan.noise_voltage() * math.sqrt(2), 0.25, places=6)
        self.assertAlmostEqual(abs(mult.k()), 10.0, places=4)
        self.assertEqual(s2.unknown, 1)
        back = sorted((pmt.to_python(m) for m in out.got["rb"]), key=lambda d: d["knob"])
        self.assertEqual([d["knob"] for d in back], ["gain", "noise"])
        self.assertAlmostEqual(back[0]["readback"], 20.0, places=4)
        self.assertAlmostEqual(back[1]["readback"] * math.sqrt(2), 0.25, places=6)


if __name__ == '__main__':
    gr_unittest.run(qa_msg_setter)
