# SPDX-License-Identifier: GPL-3.0-or-later
"""Profiles, the store, the stage pipeline, decoder attachment, and the rule
that the core imports with neither GNU Radio nor SoapySDR."""
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from rxtune import profile
from rxtune.attach import LineScraper, PipeDecoder
from rxtune.cli import main
from rxtune.dial import DialSpec
from rxtune.knob import KnobSpec
from rxtune.loop import tune
from rxtune.sim import SimReceiver
from rxtune.stages import survey
from rxtune.store import Store, fingerprint

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_core_imports_without_gnuradio_or_soapy():
    code = textwrap.dedent("""
        import sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split('.')[0] in ('gnuradio', 'SoapySDR', 'pmt', 'PyQt5'):
                    raise ImportError('blocked: ' + name)
        sys.meta_path.insert(0, Block())
        import rxtune, rxtune.cli, rxtune.loop, rxtune.stages, rxtune.soapy, rxtune.attach
        import rxtune.profile, rxtune.store, rxtune.lock, rxtune.sim
        print('ok')
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": SRC})
    assert out.stdout.strip() == "ok", out.stderr


def test_selftest_command_exits_zero(capsys):
    assert main(["selftest"]) == 0
    assert "SELFTEST PASS" in capsys.readouterr().out


# ---------------------------------------------------------------- profiles
def test_shipped_rspdx_profile_loads_and_matches():
    p = profile.find({"driver": "SDRplay", "hardware": "RSPdx"})
    assert p.name == "sdrplay_rspdx"
    assert profile.find({"driver": "rtlsdr", "hardware": "R820T"}).name == "blind"
    specs = {"gain:IFGR": KnobSpec("gain:IFGR", "range", 20, 59, 1),
             "gain:RFGR": KnobSpec("gain:RFGR", "range", 0, 27, 1),
             "setting:rfgain_sel": KnobSpec("setting:rfgain_sel", "choice", choices=("0", "1")),
             "setting:iqcorr_ctrl": KnobSpec("setting:iqcorr_ctrl", "choice", choices=("false", "true"))}
    out = p.overlay(specs)
    assert out["gain:IFGR"].sense == "reduction" and out["gain:RFGR"].role == "regime"
    assert "setting:rfgain_sel" not in out and "setting:iqcorr_ctrl" not in out


def test_known_bad_is_a_mask_with_ranges():
    bad = profile.find({"driver": "sdrplay", "hardware": "rspdx"}).is_known_bad()
    assert bad({"setting:dabnotch_ctrl": "true", "freq": 183e6})          # the VHF-high lesson
    assert not bad({"setting:dabnotch_ctrl": "true", "freq": 605e6})
    assert not bad({"setting:dabnotch_ctrl": "false", "freq": 183e6})
    assert not bad({"freq": 183e6})


def test_profile_rejects_unknown_keys(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("name: x\ncode: 'import os'\n")
    with pytest.raises(ValueError):
        profile.load(f)
    f.write_text("name: x\nknobs:\n  'gain:A':\n    setter: evil\n")
    with pytest.raises(ValueError):
        profile.load(f)


# ---------------------------------------------------------------- store
def test_store_staleness_and_path_change(tmp_path):
    rx = SimReceiver("healthy")
    rep = tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"})
    st = Store(tmp_path / "runs")
    dev = {"driver": "sim"}
    fp_a = fingerprint(dev, {"antenna": "A"}, "loft")
    assert st.check(fp_a, "ch1").stale                              # never calibrated
    st.save(fp_a, "ch1", rep.verdict, rep.result.curve())
    assert not st.check(fp_a, "ch1").stale
    assert st.check(fp_a, "ch1", max_age_s=-1).stale                # age
    assert st.check(fp_a, "ch1", dial_now=rep.verdict.dial - 3).stale   # sag
    # any change to the RF path is a different fingerprint: nothing is reused
    assert fingerprint(dev, {"antenna": "B"}, "loft") != fp_a
    assert fingerprint(dev, {"antenna": "A"}, "loft + preamp") != fp_a
    assert st.check(fingerprint(dev, {"antenna": "B"}, "loft"), "ch1").stale


def test_store_never_recommends_from_a_failed_calibration(tmp_path):
    rx = SimReceiver("plumbing")
    rep = tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"})
    st = Store(tmp_path)
    st.save("fp", "t", rep.verdict, [])
    assert st.check("fp", "t").stale


def test_hour_is_a_recorded_knob(tmp_path):
    st = Store(tmp_path)
    st.log("fp", "t", {"g": 1}, {"value": 17.0})
    st.log("fp", "t", {"g": 1}, {"value": 19.0})
    st.log("fp", "other", {"g": 1}, {"value": 3.0})
    hour = time.localtime().tm_hour
    assert st.hour_curve("fp", "t") == {hour: 19.0}


# ---------------------------------------------------------------- stages
def test_survey_ranks_ports_and_judges_configs_by_content():
    rx = SimReceiver("fading")            # near the cliff: where configs matter
    rep = survey(rx, rx, rx, paths={"antenna": ["A", "B"]}, config_knob="config", clock=rx.clock)
    assert rep.winner.fixed == {"antenna": "A"}
    assert [r["path"]["antenna"] for r in rep.scans] == ["A", "B"]
    # "slow_avg" reads 2 dB BETTER on the dial and decodes half as much
    assert rep.shootout_metric == "liveness rate" and rep.config == "erasure"
    assert rep.shootout["slow_avg"].mean < rep.shootout["plain"].mean
    assert rx.state["config"] == "erasure"


def test_shootout_by_dial_would_have_picked_the_flatterer():
    """Negative control for the rule above."""
    rx = SimReceiver("fading")
    rep = survey(rx, rx, None, config_knob="config", clock=rx.clock, fixed={"antenna": "A"})
    assert rep.shootout_metric.endswith("score") and rep.config == "slow_avg"


# ---------------------------------------------------------------- attach
FAKE_DECODER = textwrap.dedent("""
    import sys
    n = 0
    while True:
        b = sys.stdin.buffer.read(4096)
        if not b:
            break
        n += 1
        sys.stderr.write(f"telem mer={10 + n % 3}.5 pkts={n * 7}\\n")
        sys.stderr.flush()
""")


def test_pipe_decoder_mode_a_scrapes_dial_and_liveness():
    sc = LineScraper(DialSpec("MER"), r"mer=([\d.]+)", r"pkts=(\d+)")
    dec = PipeDecoder([sys.executable, "-c", FAKE_DECODER], sc)
    stdin = dec.start()
    for _ in range(5):
        stdin.write(b"\0" * 4096)
        stdin.flush()
    deadline = time.time() + 5
    while sc.count() < 35 and time.time() < deadline:
        time.sleep(0.05)
    dec.stop()
    assert sc.count() == 35
    vals = [sc.read() for _ in range(5)]
    assert vals == [11.5, 12.5, 10.5, 11.5, 12.5] and sc.read() is None
    assert dec.proc is None
