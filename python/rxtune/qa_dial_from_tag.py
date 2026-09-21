#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
import time

import numpy as np
import pmt
from gnuradio import blocks, digital, gr, gr_unittest

from qa_common import rxtune, sink


def qpsk(n, snr_db, seed):
    rng = np.random.default_rng(seed)
    sym = ((rng.integers(0, 2, n) * 2 - 1) + 1j * (rng.integers(0, 2, n) * 2 - 1)) / np.sqrt(2)
    noise = (rng.normal(size=n) + 1j * rng.normal(size=n)) * 10 ** (-snr_db / 20) / np.sqrt(2)
    return (sym + noise).astype(np.complex64).tolist()


class qa_dial_from_tag(gr_unittest.TestCase):

    def test_001_stock_mpsk_snr_est_tags_become_a_dial(self):
        src = blocks.vector_source_c(qpsk(60000, 20.0, 7), True)
        est = digital.mpsk_snr_est_cc(digital.SNR_EST_M2M4, 5000, 0.01)     # STOCK: emits "snr" TAGS
        blk, out = rxtune.dial_from_tag("complex", "snr", 2), sink("dial")
        tb = gr.top_block()
        tb.connect(src, blocks.throttle(gr.sizeof_gr_complex, 200e3), est, blk)
        tb.msg_connect(blk, "dial", out, "dial")
        tb.start()
        time.sleep(1.0)
        tb.stop()
        tb.wait()
        vals = [pmt.to_double(pmt.cdr(m)) for m in out.got["dial"]]
        self.assertTrue(len(vals) >= 3, len(vals))
        self.assertAlmostEqual(vals[-1], 20.0, delta=1.5)
        self.assertEqual(pmt.symbol_to_string(pmt.car(out.got["dial"][0])), "snr")


if __name__ == '__main__':
    gr_unittest.run(qa_dial_from_tag)
