#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Dial From Tag: many stock and third-party blocks report quality as a stream
TAG (digital.mpsk_snr_est_cc tags "snr"). This turns a tag into a dial message."""
import numpy as np
import pmt
from gnuradio import gr

TYPES = {"complex": np.complex64, "float": np.float32, "byte": np.uint8, "short": np.int16}


class dial_from_tag(gr.sync_block):
    """stream in (sink) -> message out 'dial' = (key . value) for every Nth tag."""

    def __init__(self, type="complex", key="snr", every=1):
        gr.sync_block.__init__(self, name="rxtune_dial_from_tag", in_sig=[TYPES[type]], out_sig=None)
        self.key = pmt.intern(key)
        self.every = max(1, int(every))
        self._n = 0
        self.message_port_register_out(pmt.intern("dial"))

    def work(self, input_items, output_items):
        n = len(input_items[0])
        for tag in self.get_tags_in_window(0, 0, n, self.key):
            self._n += 1
            if self._n % self.every == 0 and pmt.is_number(tag.value):
                self.message_port_pub(pmt.intern("dial"), pmt.cons(self.key, tag.value))
        return n
