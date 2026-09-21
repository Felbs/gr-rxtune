# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared by the QA files: import gnuradio.rxtune from an install OR the source tree."""
import os
import sys
import threading

import pmt
from gnuradio import gr

EXAMPLES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "examples"))
sys.path.insert(0, EXAMPLES)
try:
    from gnuradio import rxtune  # noqa: F401
except ImportError:
    import _devpath  # noqa: F401
    from gnuradio import rxtune  # noqa: F401


class sink(gr.basic_block):
    """Collects messages from any number of named ports."""

    def __init__(self, *ports):
        gr.basic_block.__init__(self, name="qa_sink", in_sig=None, out_sig=None)
        self.got = {p: [] for p in ports}
        self.event = threading.Event()
        for p in ports:
            self.message_port_register_in(pmt.intern(p))
            # the Python gateway finds a handler again BY NAME, so a lambda will not do
            def handler(msg, p=p):
                self._on(p, msg)
            handler.__name__ = "_h_" + p
            setattr(self, handler.__name__, handler)
            self.set_msg_handler(pmt.intern(p), handler)

    def _on(self, port, msg):
        self.got[port].append(msg)
        self.event.set()
