#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Dial Probe (M-PSK SNR): stream in, dial message out, stock DSP inside.

Wraps digital.probe_mpsk_snr_est_c so an example flowgraph needs no custom
decoder. Valid on symbol-rate M-PSK samples only (not OFDM, not 8-VSB).

alpha is the estimator's averaging constant, applied once per scheduler buffer.
It is part of the dial's settle time: at GNU Radio's default of 0.001 the
estimate still describes the PREVIOUS gain setting many seconds after a change,
and a tuning loop that does not wait that long chases its own tail."""
from gnuradio import digital, gr

ESTIMATORS = {"simple": digital.SNR_EST_SIMPLE, "skew": digital.SNR_EST_SKEW,
              "m2m4": digital.SNR_EST_M2M4, "svr": digital.SNR_EST_SVR}


class dial_probe(gr.hier_block2):
    """complex in -> message out 'dial' (bare double, dB)."""

    def __init__(self, estimator="m2m4", msg_nsamples=10000, alpha=0.1):
        gr.hier_block2.__init__(self, "rxtune_dial_probe",
                                gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(0, 0, 0))
        self.message_port_register_hier_out("dial")
        self.probe = digital.probe_mpsk_snr_est_c(ESTIMATORS[str(estimator).lower()],
                                                  int(msg_nsamples), float(alpha))
        self.connect(self, self.probe)
        self.msg_connect(self.probe, "snr", self, "dial")

    def snr(self):
        return self.probe.snr()

    def set_msg_nsample(self, n):
        self.probe.set_msg_nsample(int(n))
