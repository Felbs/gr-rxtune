#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Radio-free closed loop, no Qt, stock DSP only.

  random bits -> QPSK mod -> weak "antenna" level --+
                       strong out-of-band neighbour-+-> channel model (noise)
    -> GAIN (the knob) -> converter: rail clip + converter noise
    -> RRC -> AGC -> symbol sync -> M-PSK SNR probe  ==dial==>  rxtune Controller
                                                                     |
        Multiply Const.set_k  <== rxtune Message Setter <==cmd=======+

Too little gain and the converter's own noise wins; too much and the neighbour
hits the rail and splatters across the wanted signal. The controller has to find
the ridge between them from the dial alone.

The only non-stock blocks are the controller and the Message Setter (GNU Radio's
Channel Model and Multiply Const have no message ports; see docs/DESIGN.md s.5).

Run:  python headless_loopback.py            (from the source tree, nothing installed)"""
import sys
import threading
import time

try:
    from gnuradio import rxtune
except ImportError:
    import _devpath  # noqa: F401
    from gnuradio import rxtune

import pmt
from gnuradio import analog, blocks, channels, digital, filter as grfilter, gr
from gnuradio.filter import firdes

SPS = 4
AXES = [{"name": "gain", "lo": -20, "hi": 40, "step": 1, "sense": "gain", "settle_s": 0.1}]


class verdict_catcher(gr.basic_block):
    """Collects the controller's outputs (stands in for Message Debug in tests)."""

    def __init__(self):
        gr.basic_block.__init__(self, name="verdict_catcher", in_sig=None, out_sig=None)
        self.verdict, self.status, self.cmds = None, [], []
        self.done = threading.Event()
        for port, fn in (("verdict", self._v), ("status", self._s), ("cmd", self._c)):
            self.message_port_register_in(pmt.intern(port))
            self.set_msg_handler(pmt.intern(port), fn)

    def _v(self, msg):
        self.verdict = pmt.to_python(msg)
        self.done.set()

    def _s(self, msg):
        self.status.append(pmt.to_python(msg))

    def _c(self, msg):
        self.cmds.append(pmt.to_python(msg))


class loopback(gr.top_block):
    def __init__(self, neighbour=0.3, signal=0.01, converter_noise=0.003, samp_rate=400e3,
                 settle_s=1.0, window_s=0.8, **controller_kw):
        gr.top_block.__init__(self, "rxtune headless loopback")
        const = digital.constellation_qpsk().base()
        self.bits = analog.random_uniform_source_b(0, 256, 1)
        self.mod = digital.generic_mod(constellation=const, differential=False,
                                       samples_per_symbol=SPS, pre_diff_code=True, excess_bw=0.35)
        self.throttle = blocks.throttle(gr.sizeof_gr_complex, samp_rate)
        self.antenna = blocks.multiply_const_cc(signal)
        self.neighbour = analog.sig_source_c(1.0, analog.GR_COS_WAVE, 0.37, neighbour)
        self.air = blocks.add_cc()
        self.chan = channels.channel_model(noise_voltage=0.001, frequency_offset=0.0,
                                           epsilon=1.0, taps=[1.0], noise_seed=1)
        self.gain = blocks.multiply_const_cc(1.0)                 # THE KNOB
        self.c2f = blocks.complex_to_float()
        self.rail_i, self.rail_q = analog.rail_ff(-1, 1), analog.rail_ff(-1, 1)
        self.f2c = blocks.float_to_complex()
        self.conv_noise = analog.noise_source_c(analog.GR_GAUSSIAN, converter_noise, 2)
        self.adc = blocks.add_cc()
        self.rrc = grfilter.fir_filter_ccf(1, firdes.root_raised_cosine(1.0, SPS, 1.0, 0.35, 11 * SPS))
        self.agc = analog.agc_cc(1e-3, 1.0, 1.0)
        # max deviation 0.01, NOT the GRC default of 1.5: with a wide clock range
        # the symbol sync wanders off during an overload cell and never re-locks,
        # so the dial reads ~9 dB at a setting that measured 30 dB a moment earlier.
        # A demodulator with hysteresis poisons every cell measured after the first
        # overload; bound its loops, or make settle_s cover its re-acquisition.
        self.sync = digital.symbol_sync_cc(digital.TED_SIGNAL_TIMES_SLOPE_ML, SPS, 0.045, 1.0, 1.0,
                                           0.01, 1, const, digital.IR_MMSE_8TAP, 128, [])
        # alpha: the estimator's own averaging is part of the dial's SETTLE time. GNU
        # Radio's default (0.001) remembers the previous gain setting for many seconds.
        self.probe = rxtune.dial_probe("m2m4", 4000, 0.2)

        self.ctl = rxtune.controller(axes=AXES, dial_name="SNR", units="dB", dialect="generic",
                                     settle_s=settle_s, window_s=window_s, **controller_kw)
        self.setter = rxtune.msg_setter(
            target=self.gain,
            setters={"gain": lambda blk, db: blk.set_k(10.0 ** (float(db) / 20.0))},
            getters={"gain": lambda blk: round(20.0 * __import__("math").log10(abs(blk.k())), 6)})
        self.catch = verdict_catcher()

        self.connect(self.bits, self.mod, self.throttle, self.antenna, (self.air, 0))
        self.connect(self.neighbour, (self.air, 1))
        self.connect(self.air, self.chan, self.gain, self.c2f)
        self.connect((self.c2f, 0), self.rail_i, (self.f2c, 0))
        self.connect((self.c2f, 1), self.rail_q, (self.f2c, 1))
        self.connect(self.f2c, (self.adc, 0))
        self.connect(self.conv_noise, (self.adc, 1))
        self.connect(self.adc, self.rrc, self.agc, self.sync, self.probe)

        self.msg_connect(self.probe, "dial", self.ctl, "dial")
        self.msg_connect(self.ctl, "cmd", self.setter, "cmd")
        self.msg_connect(self.setter, "readback", self.ctl, "readback")
        self.msg_connect(self.ctl, "verdict", self.catch, "verdict")
        self.msg_connect(self.ctl, "status", self.catch, "status")
        self.msg_connect(self.ctl, "cmd", self.catch, "cmd")


def main(timeout_s=180.0):
    tb = loopback()
    t0 = time.time()
    tb.start()
    ok = tb.catch.done.wait(timeout_s)
    tb.stop()
    tb.wait()
    if not ok:
        print("no verdict within", timeout_s, "s")
        return 1
    v = tb.catch.verdict
    print(f"{len(tb.catch.cmds)} knob writes, {v['cells']} cells, {time.time() - t0:.0f} s")
    print(f"VERDICT {v['shape']} - {v['headline']}")
    print("  why   :", v["rule"])
    print("  best  :", v["best"], "  candidate:", v["candidate"])
    for n in v["notes"]:
        print("  note  :", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
