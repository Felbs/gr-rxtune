# SPDX-License-Identifier: GPL-3.0-or-later
"""Command dialects, decoder adapters and content-based liveness. No GNU Radio."""
import subprocess
import sys
import time

import pytest

from rxtune import adapters, commands
from rxtune.attach import FileGrowth, PatternCounter, graceful_stop, popen_group


# ---------------------------------------------------------------- dialects
def test_soapy_dialect_per_element_gain_is_a_nested_dict():
    assert commands.command("soapy", "gain:IFGR", 40) == {"gain": {"name": "IFGR", "gain": 40.0}}
    assert commands.command("soapy", "gain", 30) == {"gain": 30.0}
    assert commands.command("soapy", "antenna", "Antenna C", chan=0) == {"antenna": "Antenna C", "chan": 0}
    assert commands.command("soapy", "agc", False) == {"gain_mode": False}


def test_soapy_settings_by_message_are_refused_unless_forced():
    """Measured on an RSPdx: the command stops the gr-soapy source block."""
    with pytest.raises(commands.Unsupported) as e:
        commands.command("soapy", "setting:biasT_ctrl", True)
    assert "STOPS THE SOURCE BLOCK" in str(e.value)
    forced = commands.command("soapy", "setting:biasT_ctrl", True, allow_unsafe=True)
    assert forced == {"setting": {"key": "biasT_ctrl", "value": "true"}}      # a STRING, always
    assert commands.command("soapy", "setting:agc_setpoint", -20, allow_unsafe=True)["setting"]["value"] == "-20"
    assert commands.check("soapy", ["gain:IFGR", "setting:x"])
    assert not commands.check("soapy", ["gain:IFGR", "setting:x"], allow_unsafe=True)


def test_uhd_dialect_cannot_name_a_gain_element():
    assert commands.command("uhd", "gain", 31.5) == {"gain": 31.5}
    with pytest.raises(commands.Unsupported):
        commands.command("uhd", "gain:PGA", 10)
    with pytest.raises(commands.Unsupported):
        commands.command("uhd", "setting:x", 1)


def test_only_what_changed_is_sent():
    out = commands.commands("generic", {"a": 1, "b": 2}, previous={"a": 1, "b": 0})
    assert out == [("b", {"knob": "b", "value": 2})]


# ---------------------------------------------------------------- adapters
def test_atsc_mer_formula_and_cliff():
    assert adapters.atsc_mer_from_fs_err(0.5) == pytest.approx(20.0)
    assert adapters.atsc_mer_from_fs_err(0.869) == pytest.approx(15.2, abs=0.01)   # the 8-VSB data cliff
    assert adapters.atsc_mer_from_fs_err(0) is None
    sc = adapters.atsc_scraper()
    sc.feed("[eq] batch=12 fs_err_rms=0.500 mean|x|=0.21\n")
    sc.feed("noise\n")
    assert sc.read() == pytest.approx(20.0) and sc.read() is None
    assert sc.spec.cliff == 15.2 and not sc.spec.comparable_across_configs


def test_nrsc5_dial_is_the_worse_sideband():
    sc = adapters.Nrsc5MerScraper()
    sc.feed("12:00:01 MER: 9.5 dB (lower), 11.2 dB (upper)\n")
    sc.feed("12:00:02 MER: 12.0 dB (lower), 3.1 dB (upper)\n")     # one sideband wrecked
    sc.feed("12:00:02 Title: something\n")                          # metadata is not audio
    sc.feed("12:00:03 Audio bit rate: 48.2 kbps\n")                 # printed from VALID audio packets
    assert [sc.read(), sc.read(), sc.read()] == [9.5, 3.1, None]
    assert sc.count() == 1
    ber = adapters.nrsc5_ber_scraper()
    ber.feed("BER: 0.000420, avg: 0.000500, min: 0.0, max: 0.01\n")
    assert ber.read() == pytest.approx(0.00042)
    assert ber.spec.score(1e-4) > ber.spec.score(1e-2)               # lower BER = better score


def test_adapt_modes():
    assert adapters.adapt("atsc_fs_err_rms", 0.5) == pytest.approx(20.0)
    assert adapters.adapt("atsc_fs_err_rms", "x fs_err_rms=0.5 y") == pytest.approx(20.0)
    assert adapters.adapt("nrsc5_mer", "MER: 9.5 dB (lower), 11.2 dB (upper)") == 9.5
    assert adapters.adapt("nrsc5_mer", "unrelated") is None
    assert adapters.adapt("identity", "17.5") == 17.5


# ---------------------------------------------------------------- liveness from content
def test_pattern_counter_counts_content_not_bytes(tmp_path):
    f = tmp_path / "live.ts"
    hdr = b"\x00\x00\x01\xb3"
    pc, fg = PatternCounter(str(f), hdr), FileGrowth(str(f), unit=188)
    assert pc.count() == 0 and fg.count() == 0
    f.write_bytes(b"\x47" + b"\xff" * 187)                  # a null packet: growth, no content
    assert fg.count() == 1 and pc.count() == 0              # THE mirage: bytes flowing, nothing in them
    with open(f, "ab") as fh:
        fh.write(b"abc" + hdr + b"def" + hdr[:2])           # one header + a header split across reads
    assert pc.count() == 1
    with open(f, "ab") as fh:
        fh.write(hdr[2:] + b"tail")
    assert pc.count() == 2                                  # the split one is found
    f.write_bytes(hdr)                                      # decoder restarted: file recreated
    assert pc.count() == 3                                  # still monotone


def test_graceful_stop_asks_first():
    code = ("import signal, sys, time\n"
            "def bye(*a):\n    print('closed the radio', flush=True); sys.exit(0)\n"
            "for s in ('SIGINT', 'SIGBREAK', 'SIGTERM'):\n"
            "    if hasattr(signal, s): signal.signal(getattr(signal, s), bye)\n"
            "print('streaming', flush=True)\n"
            "while True: time.sleep(0.1)\n")
    p = popen_group([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert p.stdout.readline().strip() == b"streaming"
    t0 = time.time()
    graceful_stop(p, grace_s=8.0)
    assert time.time() - t0 < 6.0
    assert p.stdout.read().strip() == b"closed the radio", "the decoder was killed, not asked"
    assert p.returncode == 0
