#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writes examples/loopback_qtgui.grc and examples/loopback_stock.grc from one
description, so the Qt and the headless example can never drift apart.
Re-run after editing; then check both with grcc (see docs/TEST_REPORT.md)."""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AXES = '[{"name": "gain", "lo": -20, "hi": 40, "step": 1, "sense": "gain", "settle_s": 0.1}]'


def blk(name_, id_, x, y, **params):
    return {"name": name_, "id": id_, "parameters": {k: str(v) for k, v in params.items()},
            "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                       "coordinate": [x, y], "rotation": 0, "state": "enabled"}}


def build(qt):
    b = [
        blk("samp_rate", "variable", 200, 12, value=400000),
        blk("sps", "variable", 300, 12, value=4),
        blk("qpsk", "variable_constellation", 400, 12, type="qpsk"),
        blk("bits", "analog_random_uniform_source_x", 8, 120, type="byte", minimum=0, maximum=256, seed=1),
        blk("mod", "digital_constellation_modulator", 200, 120, constellation="qpsk", differential=False,
            samples_per_symbol="sps", excess_bw=0.35),
        blk("throttle", "blocks_throttle2", 420, 120, type="complex", samples_per_second="samp_rate"),
        blk("antenna", "blocks_multiply_const_vxx", 600, 120, type="complex", const=0.01,
            comment="weak wanted signal"),
        blk("neighbour", "analog_sig_source_x", 420, 220, type="complex", samp_rate=1, waveform="analog.GR_COS_WAVE",
            freq=0.37, amp="neighbour_level" if qt else 0.3, comment="strong out-of-band neighbour"),
        blk("air", "blocks_add_xx", 780, 150, type="complex", num_inputs=2),
        blk("chan", "channels_channel_model", 900, 130, noise_voltage=0.001, freq_offset=0.0, epsilon=1.0,
            taps=1.0, seed=1),
        blk("gain_blk", "blocks_multiply_const_vxx", 1100, 150, type="complex", const=1.0,
            comment="THE KNOB (set by rxtune)"),
        blk("c2f", "blocks_complex_to_float", 8, 360),
        blk("rail_i", "analog_rail_ff", 200, 340, lo=-1, hi=1),
        blk("rail_q", "analog_rail_ff", 200, 410, lo=-1, hi=1),
        blk("f2c", "blocks_float_to_complex", 380, 360),
        blk("conv_noise", "analog_noise_source_x", 380, 450, type="complex", noise_type="analog.GR_GAUSSIAN",
            amp=0.003, seed=2, comment="converter noise"),
        blk("adc", "blocks_add_xx", 560, 380, type="complex", num_inputs=2),
        blk("rrc", "root_raised_cosine_filter", 700, 360, type="fir_filter_ccf", decim=1, interp=1, gain=1,
            samp_rate="sps", sym_rate=1, alpha=0.35, ntaps="11*sps"),
        blk("agc", "analog_agc_xx", 900, 360, type="complex", rate="1e-3", reference=1.0, gain=1.0,
            max_gain=65536),
        blk("sync", "digital_symbol_sync_xx", 1080, 340, type="cc",
            ted_type="digital.TED_SIGNAL_TIMES_SLOPE_ML", constellation="qpsk.base()", sps="sps",
            ted_gain=1.0, loop_bw=0.045, damping=1.0, max_dev=0.01, osps=1,
            resamp_type="digital.IR_MMSE_8TAP", nfilters=128, pfb_mf_taps="[]",
            comment="max_dev 0.01: a wide clock range never re-locks after an overload cell"),
        blk("probe", "rxtune_dial_probe", 8, 560, estimator="m2m4", msg_nsamples=4000, alpha=0.2),
        blk("ctl", "rxtune_controller", 260, 540, axes=AXES, dialect="generic", dial_name="SNR",
            settle_s=1.0, window_s=0.8),
        blk("setter", "rxtune_msg_setter", 560, 560, target="gain_blk",
            setters='{"gain": lambda blk, db: blk.set_k(10.0 ** (float(db) / 20.0))}',
            getters='{"gain": lambda blk: round(20.0 * __import__("math").log10(abs(blk.k())), 6)}'),
        blk("debug", "blocks_message_debug", 860, 640, en_uvec=True),
    ]
    c = [["bits", "0", "mod", "0"], ["mod", "0", "throttle", "0"], ["throttle", "0", "antenna", "0"],
         ["antenna", "0", "air", "0"], ["neighbour", "0", "air", "1"], ["air", "0", "chan", "0"],
         ["chan", "0", "gain_blk", "0"], ["gain_blk", "0", "c2f", "0"], ["c2f", "0", "rail_i", "0"],
         ["c2f", "1", "rail_q", "0"], ["rail_i", "0", "f2c", "0"], ["rail_q", "0", "f2c", "1"],
         ["f2c", "0", "adc", "0"], ["conv_noise", "0", "adc", "1"], ["adc", "0", "rrc", "0"],
         ["rrc", "0", "agc", "0"], ["agc", "0", "sync", "0"], ["sync", "0", "probe", "0"],
         ["probe", "dial", "ctl", "dial"], ["ctl", "cmd", "setter", "cmd"],
         ["setter", "readback", "ctl", "readback"], ["ctl", "verdict", "debug", "print"]]
    if qt:
        b += [
            blk("neighbour_level", "variable_qtgui_range", 560, 12, label="Neighbour level", value=0.3, start=0,
                stop=1.0, step=0.01, widget="counter_slider", gui_hint="0,0,1,2"),
            blk("time_sink", "qtgui_time_sink_x", 1100, 470, type="complex", name='"after the converter"',
                size=1024, srate="samp_rate", gui_hint="1,0,1,1"),
            blk("freq_sink", "qtgui_freq_sink_x", 1100, 560, type="complex", name='"after the converter"',
                fftsize=1024, bw="samp_rate", gui_hint="1,1,1,1"),
            blk("to_mag", "blocks_complex_to_mag", 700, 470),
            blk("level_sink", "qtgui_number_sink", 900, 470, type="float", name='"converter level"',
                graph_type="qtgui.NUM_GRAPH_HORIZ", min=0, max=1.5, gui_hint="2,0,1,1"),
            blk("dash", "rxtune_dashboard", 560, 680, label="rxtune", units="dB", dial_max=35,
                gui_hint="2,1,2,1"),
        ]
        c += [["adc", "0", "time_sink", "0"], ["adc", "0", "freq_sink", "0"], ["adc", "0", "to_mag", "0"],
              ["to_mag", "0", "level_sink", "0"], ["probe", "dial", "dash", "dial"],
              ["ctl", "status", "dash", "status"], ["ctl", "verdict", "dash", "verdict"]]
    name = "loopback_qtgui" if qt else "loopback_stock"
    return {
        "options": {"parameters": {"id": name, "title": "rxtune radio-free loopback" + (" (Qt)" if qt else ""),
                                   "author": "gr-rxtune", "generate_options": "qt_gui" if qt else "no_gui",
                                   "output_language": "python", "category": "[GRC Hier Blocks]",
                                   "run": "True", "run_options": "prompt", "gen_cmake": "On",
                                   "description": "Stock DSP plant; rxtune finds the gain ridge from the "
                                                  "SNR dial alone."},
                    "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
                               "coordinate": [8, 8], "rotation": 0, "state": "enabled"}},
        "blocks": b, "connections": c,
        "metadata": {"file_format": 1, "grc_version": "3.10.12.0"},
    }, name


if __name__ == "__main__":
    for qt in (True, False):
        doc, name = build(qt)
        path = os.path.join(ROOT, "examples", name + ".grc")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=None, width=110)
        print("wrote", path)
