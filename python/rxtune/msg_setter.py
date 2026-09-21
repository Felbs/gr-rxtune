#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Message Setter: command message in, setter METHOD call on another block.

For everything that cannot be commanded by message: gr-osmosdr's source (no
message port at all), gr-uhd named gain elements, channels.channel_model, a
multiply_const standing in for a gain stage. With a getter it also reads the
value back and reports it - the only readback available inside a flowgraph."""
import pmt
from gnuradio import gr

from ._pmt import is_real_dict


class msg_setter(gr.basic_block):
    """
    target : the block object (in GRC: its id)
    setters: {knob: "method_name" | callable(target, value)}
    getters: {knob: "method_name" | callable(target)}           (optional)
    Accepts the controller's 'generic' dialect {knob, value}; any other dict is
    treated as {knob: value, ...}.
    """

    def __init__(self, target=None, setters=None, getters=None):
        gr.basic_block.__init__(self, name="rxtune_msg_setter", in_sig=None, out_sig=None)
        self.target, self.setters, self.getters = target, dict(setters or {}), dict(getters or {})
        self.message_port_register_in(pmt.intern("cmd"))
        self.set_msg_handler(pmt.intern("cmd"), self._on_cmd)
        self.message_port_register_out(pmt.intern("readback"))
        self.unknown = 0

    @staticmethod
    def _call(target, how, *args):
        return how(target, *args) if callable(how) else getattr(target, how)(*args)

    def _on_cmd(self, msg):
        if not is_real_dict(msg):
            return
        d = pmt.to_python(msg)
        items = [(d["knob"], d["value"])] if set(d) >= {"knob", "value"} else list(d.items())
        for knob, value in items:
            how = self.setters.get(knob)
            if how is None:
                self.unknown += 1
                continue
            self._call(self.target, how, value)
            if knob in self.getters:
                back = self._call(self.target, self.getters[knob])
                self.message_port_pub(pmt.intern("readback"), pmt.to_pmt(
                    {"knob": knob, "value": value, "readback": back}))
