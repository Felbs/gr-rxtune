#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
import time

import pmt
from gnuradio import gr, gr_unittest

from qa_common import rxtune, sink


def feed(mode, payloads):
    blk, out = rxtune.dial_adapter(mode), sink("dial")
    tb = gr.top_block()
    tb.msg_connect(blk, "dial", out, "dial")
    tb.start()
    for p in payloads:
        blk.to_basic_block()._post(pmt.intern("in"), p)
    time.sleep(0.4)
    tb.stop()
    tb.wait()
    return [(pmt.symbol_to_string(pmt.car(m)), pmt.to_double(pmt.cdr(m))) for m in out.got["dial"]]


class qa_dial_adapter(gr_unittest.TestCase):

    def test_001_atsc_training_error_to_mer(self):
        got = feed("atsc_fs_err_rms", [pmt.from_double(0.5),                           # a bare number
                                       pmt.intern("[eq] fs_err_rms=0.869 taps=64"),    # a log line
                                       pmt.intern("unrelated line"),
                                       pmt.cons(pmt.intern("x"), pmt.from_double(5.0))])
        self.assertEqual([n for n, _ in got], ["MER"] * 3)
        self.assertAlmostEqual(got[0][1], 20.0, places=3)          # 20*log10(5/0.5)
        self.assertAlmostEqual(got[1][1], 15.2, delta=0.02)        # the 8-VSB cliff
        self.assertAlmostEqual(got[2][1], 0.0, places=6)

    def test_002_nrsc5_lines_as_pdu_bytes(self):
        text = b"12:00:01 MER: 9.5 dB (lower), 11.2 dB (upper)\n12:00:02 BER: 0.000420, avg: 0.0005\n"
        pdu = pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(text), list(text)))
        self.assertEqual(feed("nrsc5_mer", [pdu]), [("MER", 9.5)])             # the WORSE sideband
        ber = feed("nrsc5_ber", [pdu])
        self.assertEqual(ber[0][0], "BER")
        self.assertAlmostEqual(ber[0][1], 0.00042, places=8)

    def test_003_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            rxtune.dial_adapter("telepathy")


if __name__ == '__main__':
    gr_unittest.run(qa_dial_adapter)
