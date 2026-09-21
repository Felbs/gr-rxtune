#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
import time

import pmt
from gnuradio import blocks, gr, gr_unittest

from qa_common import rxtune, sink
from qa_dial_from_tag import qpsk


class qa_dial_probe(gr_unittest.TestCase):

    def test_001_bare_double_in_db_tracks_the_true_snr(self):
        for true_db in (10.0, 25.0):
            src = blocks.vector_source_c(qpsk(80000, true_db, 3), True)
            blk, out = rxtune.dial_probe("m2m4", 8000, 0.05), sink("dial")
            tb = gr.top_block()
            tb.connect(src, blocks.throttle(gr.sizeof_gr_complex, 200e3), blk)
            tb.msg_connect(blk, "dial", out, "dial")
            tb.start()
            time.sleep(1.0)
            tb.stop()
            tb.wait()
            self.assertTrue(len(out.got["dial"]) >= 5)
            last = out.got["dial"][-1]
            self.assertTrue(pmt.is_real(last), "the stock probe publishes a BARE double")
            self.assertAlmostEqual(pmt.to_double(last), true_db, delta=1.5)


if __name__ == '__main__':
    gr_unittest.run(qa_dial_probe)
