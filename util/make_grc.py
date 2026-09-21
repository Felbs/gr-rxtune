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
    R1, R2, R3, R4 = 250, 560, 880, 1170          # row baselines (canvas pixels)
    b = [
        blk("samp_rate", "variable", 230, 12, value=400000),
        blk("sps", "variable", 340, 12, value=4),
        blk("qpsk", "variable_constellation", 450, 12, type="qpsk"),
        blk("note_air", "note", 8, R1 - 75, note="1. THE AIR"),
        # ---- row 1: the air ---------------------------------------------------
        blk("bits", "analog_random_uniform_source_x", 8, R1, type="byte", minimum=0, maximum=256, seed=1),
        blk("mod", "digital_constellation_modulator", 230, R1, constellation="qpsk", differential=False,
            samples_per_symbol="sps", excess_bw=0.35),
        blk("throttle", "blocks_throttle2", 490, R1, type="complex", samples_per_second="samp_rate"),
        blk("antenna", "blocks_multiply_const_vxx", 700, R1 + 12, type="complex", const=0.01,
            comment="weak wanted signal"),
        blk("neighbour", "analog_sig_source_x", 640, R1 + 110, type="complex", samp_rate=1,
            waveform="analog.GR_COS_WAVE", freq=0.37, amp="neighbour_level" if qt else 0.3,
            comment="strong out-of-band neighbour"),
        blk("air", "blocks_add_xx", 900, R1 + 40, type="complex", num_inputs=2),
        blk("chan", "channels_channel_model", 1010, R1, noise_voltage=0.001, freq_offset=0.0, epsilon=1.0,
            taps=1.0, seed=1),
        blk("to_radio", "virtual_sink", 1260, R1 + 40, stream_id="antenna_port"),
        # ---- row 2: the radio: gain knob, then a converter that clips -------------
        blk("note_radio", "note", 8, R2 - 75, note="2. THE RADIO"),
        blk("from_air", "virtual_source", 8, R2 + 30, stream_id="antenna_port"),
        blk("gain_blk", "blocks_multiply_const_vxx", 200, R2 + 18, type="complex", const=1.0,
            comment="THE KNOB (set by rxtune)"),
        blk("c2f", "blocks_complex_to_float", 400, R2 + 14),
        blk("rail_i", "analog_rail_ff", 590, R2 - 30, lo=-1, hi=1),
        blk("rail_q", "analog_rail_ff", 590, R2 + 70, lo=-1, hi=1),
        blk("f2c", "blocks_float_to_complex", 780, R2 + 14),
        blk("conv_noise", "analog_noise_source_x", 740, R2 + 130, type="complex",
            noise_type="analog.GR_GAUSSIAN", amp=0.003, seed=2, comment="converter noise"),
        blk("adc", "blocks_add_xx", 980, R2 + 40, type="complex", num_inputs=2),
        blk("to_demod", "virtual_sink", 1100, R2 + 44, stream_id="converter_out"),
        # ---- row 3: the demodulator and the dial -----------------------------------
        blk("note_demod", "note", 8, R3 - 75, note="3. THE DECODER"),
        blk("from_conv", "virtual_source", 8, R3 + 40, stream_id="converter_out"),
        blk("rrc", "root_raised_cosine_filter", 200, R3, type="fir_filter_ccf", decim=1, interp=1, gain=1,
            samp_rate="sps", sym_rate=1, alpha=0.35, ntaps="11*sps"),
        blk("agc", "analog_agc_xx", 450, R3, type="complex", rate="1e-3", reference=1.0, gain=1.0,
            max_gain=65536),
        blk("sync", "digital_symbol_sync_xx", 650, R3 - 20, type="cc",
            ted_type="digital.TED_SIGNAL_TIMES_SLOPE_ML", constellation="qpsk.base()", sps="sps",
            ted_gain=1.0, loop_bw=0.045, damping=1.0, max_dev=0.01, osps=1,
            resamp_type="digital.IR_MMSE_8TAP", nfilters=128, pfb_mf_taps="[]",
            comment="max deviation 0.01: with a wide clock range it never re-locks after an overload cell"),
        blk("to_dial", "virtual_sink", 1010, R3 + 30, stream_id="symbols"),
        # ---- row 4: the loop --------------------------------------------------------
        blk("note_loop", "note", 8, R4 - 75, note="4. DIAL + LOOP"),
        blk("from_sync", "virtual_source", 8, R4 + 20, stream_id="symbols"),
        blk("probe", "rxtune_dial_probe", 190, R4, estimator="m2m4", msg_nsamples=4000, alpha=0.2,
            comment="alpha is part of the dial's settle time"),
        blk("ctl", "rxtune_controller", 480, R4, axes=AXES, dialect="generic", dial_name="SNR",
            settle_s=1.0, window_s=0.8),
        blk("setter", "rxtune_msg_setter", 830, R4, target="gain_blk",
            setters='{"gain": lambda blk, db: blk.set_k(10.0 ** (float(db) / 20.0))}',
            getters='{"gain": lambda blk: round(20.0 * __import__("math").log10(abs(blk.k())), 6)}',
            comment="Multiply Const has no message port: call set_k(), read k() back"),
        blk("debug", "blocks_message_debug", 1180, R4 + 140, en_uvec=True),
    ]
    c = [["bits", "0", "mod", "0"], ["mod", "0", "throttle", "0"], ["throttle", "0", "antenna", "0"],
         ["antenna", "0", "air", "0"], ["neighbour", "0", "air", "1"], ["air", "0", "chan", "0"],
         ["chan", "0", "to_radio", "0"], ["from_air", "0", "gain_blk", "0"],
         ["gain_blk", "0", "c2f", "0"], ["c2f", "0", "rail_i", "0"],
         ["c2f", "1", "rail_q", "0"], ["rail_i", "0", "f2c", "0"], ["rail_q", "0", "f2c", "1"],
         ["f2c", "0", "adc", "0"], ["conv_noise", "0", "adc", "1"], ["adc", "0", "to_demod", "0"],
         ["from_conv", "0", "rrc", "0"],
         ["rrc", "0", "agc", "0"], ["agc", "0", "sync", "0"], ["sync", "0", "to_dial", "0"], ["from_sync", "0", "probe", "0"],
         ["probe", "dial", "ctl", "dial"], ["ctl", "cmd", "setter", "cmd"],
         ["setter", "readback", "ctl", "readback"], ["ctl", "verdict", "debug", "print"]]
    if qt:
        b += [
            blk("neighbour_level", "variable_qtgui_range", 700, 12, label="Neighbour level", value=0.3, start=0,
                stop=1.0, step=0.01, widget="counter_slider", gui_hint="0,0,1,2"),
            blk("from_conv_gui", "virtual_source", 1230, R2 + 150, stream_id="converter_out"),
            blk("time_sink", "qtgui_time_sink_x", 1450, R2 - 30, type="complex", name='"after the converter"',
                size=1024, srate="samp_rate", gui_hint="1,0,1,1"),
            blk("freq_sink", "qtgui_freq_sink_x", 1450, R2 + 110, type="complex", name='"after the converter"',
                fftsize=1024, bw="samp_rate", gui_hint="1,1,1,1"),
            blk("to_mag", "blocks_complex_to_mag", 1450, R2 + 290),
            blk("level_sink", "qtgui_number_sink", 1640, R2 + 260, type="float", name='"converter level"',
                graph_type="qtgui.NUM_GRAPH_HORIZ", min=0, max=1.5, gui_hint="2,0,1,1"),
            blk("dash", "rxtune_dashboard", 830, R4 + 140, label="rxtune", units="dB", dial_max=35,
                gui_hint="2,1,2,1"),
        ]
        c += [["from_conv_gui", "0", "time_sink", "0"], ["from_conv_gui", "0", "freq_sink", "0"],
              ["from_conv_gui", "0", "to_mag", "0"],
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
