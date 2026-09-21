#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Dial Adapter: a decoder's own telemetry (a number, or a line of its log)
in; a dial message out. Two real-world adapters ship:
  atsc_fs_err_rms  8-VSB equaliser training error -> MER dB = 20*log10(5/rms)
  nrsc5_mer / nrsc5_ber   HD Radio (nrsc5 and forks) log lines"""
import pmt
from gnuradio import gr

from rxtune import adapters

from ._pmt import unpack


class dial_adapter(gr.basic_block):
    """message in 'in' (number | string | pair | u8vector PDU) -> message out 'dial'."""

    def __init__(self, mode="identity"):
        gr.basic_block.__init__(self, name="rxtune_dial_adapter", in_sig=None, out_sig=None)
        if mode not in adapters.MODES:
            raise ValueError(f"mode must be one of {sorted(adapters.MODES)}")
        self.mode = mode
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self._on_msg)
        self.message_port_register_out(pmt.intern("dial"))

    def _on_msg(self, msg):
        kind, payload = unpack(msg)
        if kind == "dict":
            payload = payload.get("value")
        for line in (payload.splitlines() if isinstance(payload, str) else [payload]):
            value = adapters.adapt(self.mode, line)
            if value is not None:
                self.message_port_pub(pmt.intern("dial"),
                                      pmt.cons(pmt.intern(adapters.MODES[self.mode][2].name),
                                               pmt.from_double(float(value))))
