# SPDX-License-Identifier: GPL-3.0-or-later
"""Layer-1 proof with no radio: the optimizer and EVERY verdict, each with a
positive and a negative control, against the simulated receiver."""
import dataclasses
import math

import pytest

from rxtune.dial import VITERBI_BER, DialSpec, LatchDial
from rxtune.knob import FunctionKnob, KnobSpec, KnobWriteError, coarse_indices
from rxtune.lock import NullLock, Yielded
from rxtune.loop import tune
from rxtune.measure import Accumulator, Measurer
from rxtune.optimize import Axis, Tuner
from rxtune.shape import Shape
from rxtune.sim import SCENARIOS, SIM_MER, Scenario, SimReceiver


def run(name, **kw):
    rx = SimReceiver(name) if isinstance(name, str) else name
    kw.setdefault("fixed", {"antenna": "A"})
    return rx, tune(rx, rx, kw.pop("liveness", rx), clock=rx.clock, **kw)


# ---------------------------------------------------------------- verdicts
EXPECT = {
    "healthy": (Shape.HEALTHY, True), "island": (Shape.ISLAND, True),
    "fading": (Shape.FADING, True), "impulse": (Shape.IMPULSE, True),
    "plumbing": (Shape.PLUMBING, False), "aperture": (Shape.APERTURE_LIMITED, False),
    "gain_limited": (Shape.GAIN_LIMITED, False), "overload": (Shape.OVERLOAD, False),
    "no_signal": (Shape.NO_SIGNAL, False), "starved": (Shape.STARVED, False),
    "crc_dial": (Shape.HEALTHY, True), "crc_dial_dead": (Shape.BLIND_DIAL, False),
}


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_every_shape_positive(name):
    shape, recommended = EXPECT[name]
    _, rep = run(name)
    assert rep.verdict.shape is shape, str(rep)
    assert rep.verdict.ok is recommended, str(rep)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("name", sorted(EXPECT))
def test_verdicts_are_stable_across_noise_seeds(name, seed):
    rx = SimReceiver(name, seed=seed)
    rep = tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"})
    assert rep.verdict.shape is EXPECT[name][0], f"seed {seed}: {rep}"


def test_physical_verdicts_say_so_in_db():
    _, rep = run("aperture")
    v = rep.verdict
    assert v.physical and v.best is None
    assert v.margin_db == pytest.approx(-9.0, abs=0.7)
    assert "short of the cliff" in v.headline and "physical" in v.headline


def test_negative_controls():
    # a healthy plateau is wide: it must not be called an island
    assert run("healthy")[1].verdict.evidence["good_region_width_db"] > 9
    # the island really is narrow, and the healthy rule alone would have passed it
    assert run("island")[1].verdict.evidence["good_region_width_db"] < 9
    # impulse rails must not be mistaken for overload, nor overload for impulse
    assert run("impulse")[1].verdict.shape is not Shape.OVERLOAD
    assert run("overload")[1].verdict.shape is not Shape.IMPULSE
    # flat-below-cliff needs NO clipping; a rising tail is not a plateau
    assert run("gain_limited")[1].verdict.shape is not Shape.APERTURE_LIMITED
    assert run("aperture")[1].verdict.shape is not Shape.GAIN_LIMITED
    # fading is a property of time at one setting, not of the gain curve
    assert run("healthy")[1].verdict.evidence["best_spread"] < 1.0


# ---------------------------------------------------------------- liveness
def test_liveness_gate_refuses_a_mirage():
    _, rep = run("plumbing")
    assert rep.verdict.dial > 25 and rep.verdict.best is None
    assert "NOTHING DECODED" in rep.verdict.headline


def test_no_liveness_source_is_unproven_not_good():
    rx = SimReceiver("healthy")
    rep = tune(rx, rx, None, clock=rx.clock, fixed={"antenna": "A"})
    assert rep.verdict.shape is Shape.UNPROVEN and rep.verdict.best is None
    assert rep.verdict.candidate is not None


# ---------------------------------------------------------------- optimizer
def test_island_is_found_from_a_full_span_grid():
    rx, rep = run("island")
    assert rep.verdict.best["gain:RF"] == 8
    assert 28 <= rep.verdict.best["gain:IF"] <= 36
    assert rx.state["gain:RF"] == 8, "the radio must be left ON the winner"


def test_staircase_extends_a_restricted_span():
    """The hot-LNA lesson: a starting grid at sensible gains sees only a
    staircase; the answer is far out in the attenuation."""
    rx, rep = run("island", start={"gain:RF": [2, 3, 4], "gain:IF": [32, 40, 48]})
    assert rep.result.extended
    assert any("full range" in line for line in rep.result.log)
    assert rep.verdict.best and rep.verdict.best["gain:RF"] == 8


def test_monotone_staircase_extends_toward_the_edge_it_climbs_into():
    rx, rep = run("island", start={"gain:RF": [5, 6, 7], "gain:IF": [36, 40, 44]})
    assert rep.result.extended
    assert any("climbs monotonically" in line for line in rep.result.log)
    assert rep.verdict.best and rep.verdict.best["gain:RF"] == 8


def test_staircase_negative_control_no_extension_when_peak_is_interior():
    _, rep = run("healthy", start={"gain:RF": [2, 5, 8], "gain:IF": [30, 40, 50]})
    assert not rep.result.extended


def test_best_is_within_a_db_of_the_true_optimum():
    for name in ("healthy", "island", "aperture", "fading"):
        rx, rep = run(name)
        truth = -1e9
        for rf in range(10):
            for ifr in range(20, 60):
                rx.state.update({"gain:RF": rf, "gain:IF": ifr})
                truth = max(truth, min(rx._model()["snr"], rx.sc.mer_cap))
        tol = 1.5 if rx.sc.fade_db else 1.0          # a fading dial is a noisy ruler
        assert rep.verdict.dial >= truth - tol, (name, rep.verdict.dial, truth)


def test_budget_and_known_bad_mask():
    rx = SimReceiver("healthy")
    rep = tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"}, max_cells=10)
    assert len([p for p in rep.result.points if p.phase != "confirm"]) <= 10
    assert any("budget" in n for n in rep.verdict.notes)
    rx = SimReceiver("healthy")
    rep = tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"},
               known_bad=lambda s: s["gain:RF"] == 0)
    assert all(p.setting["gain:RF"] != 0 for p in rep.result.points)


def test_stepwise_api_matches_blocking_run():
    def go(stepwise):
        rx = SimReceiver("island")
        axes = [Axis.of(rx.knobs()[n].spec) for n in ("gain:RF", "gain:IF")]
        t = Tuner(axes, rx.spec)
        m = Measurer(rx, rx, rx, clock=rx.clock)

        def measure(s):
            s = dict(s)
            scale = s.pop("__window_scale__", 1.0)
            for k, v in s.items():
                rx.knobs()[k].set(v)
            return m.measure(window_scale=scale)
        if not stepwise:
            return t.run(measure).best.setting
        s = t.propose()
        while s is not None:
            s = t.report(measure(s))
        return t.result.best.setting
    assert go(True) == go(False)


def test_pick_policies():
    rx, rep = run("healthy", pick="knee")
    knee = rep.verdict
    rx2, rep2 = run("healthy", pick="max")
    assert knee.ok and rep2.verdict.ok
    lvl = {p.cell: p.reading.level_db for p in rep.result.points}
    best_lvl = [p for p in rep.result.points if p.phase == "confirm"][0].reading.level_db
    assert best_lvl <= sorted(lvl.values())[len(lvl) // 2], "knee sits in the low-gain half"


# ---------------------------------------------------------------- knobs
def test_senses_are_learned_not_assumed():
    _, rep = run("healthy")
    assert rep.senses == {"gain:RF": "reduction", "gain:IF": "reduction"}


def test_stuck_knob_is_caught_by_readback_and_by_level():
    _, rep = run("stuck_knob")
    assert rep.senses["gain:IF"] == "no-effect"
    assert any("did not read back" in w for w in rep.warnings)
    assert "open-loop" in rep.verdict.confidence or "closed-loop" not in rep.verdict.confidence


def test_readback_or_it_did_not_happen():
    box = {"v": 5}
    k = FunctionKnob(KnobSpec("x", "range", 0, 10, 1), lambda v: None, lambda: box["v"])
    with pytest.raises(KnobWriteError):
        k.set(7)
    k2 = FunctionKnob(KnobSpec("ant", "choice", choices=("Antenna A", "Antenna C")),
                      lambda v: box.update(v=v), lambda: box["v"])
    k2.set("Antenna C")
    assert not FunctionKnob(KnobSpec("y"), lambda v: None).closed_loop


def test_awkward_ranges():
    att = KnobSpec("gain:ATT", "range", -48, 0, 6, sense="gain")       # an attenuator, 0 = most signal
    assert att.values() == [-48, -42, -36, -30, -24, -18, -12, -6, 0]
    rtl = KnobSpec("gain:TUNER", "choice", choices=(0.0, 0.9, 1.4, 2.7, 49.6))
    assert rtl.values()[-1] == 49.6 and rtl.ordered
    swapped = KnobSpec("g", "range", 10, 0, 5)
    assert swapped.values() == [0, 5, 10]
    nostep = KnobSpec("g", "range", 0.0, 1.0, None)
    assert len(nostep.values()) == 41
    assert coarse_indices(40, 5) == [0, 10, 20, 29, 39]
    assert coarse_indices(3, 5) == [0, 1, 2]


# ---------------------------------------------------------------- measuring
def test_settle_discards_the_lying_samples():
    """Right after a change the sim's dial reads up to 6 dB low, as a real
    decoder's does while its loops reconverge. No settle = a biased number."""
    def reading(settle):
        rx = SimReceiver("healthy")
        spec = dataclasses.replace(SIM_MER, settle_s=settle, window_s=1.0)
        rx.knobs()["gain:IF"].set(45)
        acc = Accumulator(spec, rx.clock.now())
        while not acc.done(rx.clock.now()):
            acc.add(rx.read(), rx.clock.now())
            rx.clock.sleep(0.1)
        return acc.result(rx.clock.now()).value
    assert reading(2.0) == pytest.approx(30.0, abs=0.5)
    assert reading(0.0) < 28.5


def test_minimum_evidence_guard():
    spec = DialSpec("x", min_samples=5, settle_s=0, window_s=1)
    acc = Accumulator(spec, 0.0)
    for i in range(3):
        acc.add(10.0, 0.1 * i)
    r = acc.result(1.0)
    assert r.value is None and "not enough evidence" in r.note


def test_lower_is_better_and_log_dials():
    assert VITERBI_BER.score(1e-3) == pytest.approx(30.0)
    assert VITERBI_BER.score(1e-5) > VITERBI_BER.score(1e-3)
    d = DialSpec("err", higher_is_better=False)
    assert d.score(2.0) > d.score(3.0)
    assert d.score(None) is None and d.score(math.nan) is None


def test_latch_dial_hands_each_value_out_once():
    d = LatchDial(SIM_MER)
    d.push(17.0)
    assert d.read() == 17.0 and d.read() is None


# ---------------------------------------------------------------- sharing
def test_yield_stops_the_run_and_releases_the_lock():
    events = []

    class Lock(NullLock):
        n = 0

        def __enter__(self):
            events.append("acquire")
            return self

        def __exit__(self, *exc):
            events.append("release")
            return False

        def should_yield(self):
            self.n += 1
            return "a recorder needs the radio" if self.n > 3 else None

    rx = SimReceiver("healthy")
    with pytest.raises(Yielded):
        tune(rx, rx, rx, clock=rx.clock, lock=Lock(), fixed={"antenna": "A"})
    assert events == ["acquire", "release"]


def test_agc_is_forced_off():
    rx = SimReceiver("healthy")
    assert rx.state["agc"] is True
    tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"}, max_cells=3)
    assert rx.state["agc"] is False


def test_custom_scenario_roundtrip():
    sc = Scenario("mine", signal_dbm=-75.0)
    assert sc.name not in SCENARIOS
    _, rep = run(SimReceiver(sc))
    assert rep.verdict.shape is Shape.HEALTHY


def test_headroom_pick_treats_a_noisy_plateau_as_a_tie():
    """From the first hardware run: flat plateau, one lucky reading near the ridge."""
    from rxtune.dial import Reading
    from rxtune.optimize import Point, pick_headroom
    spec = DialSpec("MER", resolution=0.2)

    def pt(level, score):
        r = Reading(value=score, score=score, n=10, p10=score - 0.5, p90=score + 0.5,
                    overload=0.0, level_db=level)
        return Point((0,), {"g": level}, r, "coarse", "")
    pts = [pt(-31, 11.8), pt(-25, 12.7), pt(-19, 13.0), pt(-16, 12.9), pt(-13, 12.9), pt(-12.6, 13.5)]
    chosen = pick_headroom(pts, spec, lambda p: p.reading.level_db)
    assert chosen.reading.level_db <= -16, chosen.reading.level_db     # not the lucky cell by the ridge
    assert chosen.score >= 12.9


def test_a_cell_that_did_not_decode_cannot_win():
    """From the ATSC hardware run: the highest MER of the run had no video in it."""
    from rxtune.dial import Reading
    spec = DialSpec("MER", cliff=15.2, settle_s=0, window_s=1)
    axis = Axis.of(KnobSpec("g", "range", 0, 4, 1, sense="gain"))
    curve = {0: (18.4, True), 1: (18.4, True), 2: (18.5, True), 3: (18.9, False), 4: (None, False)}

    def measure(setting):
        v, alive = curve[setting["g"]]
        return Reading(value=v, score=v, n=20, p10=None if v is None else v - 0.2,
                       p90=None if v is None else v + 0.2, alive=alive)
    t = Tuner([axis], spec)
    res = t.run(measure)
    assert res.best.setting["g"] != 3 and res.best.reading.alive
    assert any("nothing decoded there" in line for line in res.log)
    from rxtune.verdict import judge
    v = judge(res, spec, t.gain_of)
    assert v.shape is Shape.HEALTHY and v.ok


def test_the_curve_brackets_an_unknown_decode_threshold():
    """From the ATSC 3.0 hardware run: 14.65 dB did not decode, 16.9 dB did."""
    from rxtune.dial import Reading
    from rxtune.verdict import judge
    spec = DialSpec("SNR", cliff=None, settle_s=0, window_s=1)
    axis = Axis.of(KnobSpec("g", "range", 0, 5, 1, sense="gain"))
    rows = {0: (9.9, 0.2, False), 1: (14.65, 0.45, False), 2: (14.45, 7.0, True),   # bursty: ignored
            3: (16.9, 0.4, True), 4: (22.9, 0.1, True), 5: (None, 0.0, False)}

    def measure(setting):
        v, spread, alive = rows[setting["g"]]
        return Reading(value=v, score=v, n=19, p10=None if v is None else v - spread / 2,
                       p90=None if v is None else v + spread / 2, alive=alive)
    t = Tuner([axis], spec, confirm=False)
    v = judge(t.run(measure), spec, t.gain_of)
    assert v.evidence["observed_cliff_lo"] == 14.65 and v.evidence["observed_cliff_hi"] == 16.9
    assert any("observed decode threshold" in n and "+7.1" in n for n in v.notes), v.notes


def test_a_pick_that_does_not_reproduce_is_replaced():
    """From the ATSC 3.0 GNU Radio run: a bursty cell read high once, low on confirmation."""
    from rxtune.dial import Reading
    spec = DialSpec("SNR", settle_s=0, window_s=1)
    axis = Axis.of(KnobSpec("g", "range", 0, 4, 1, sense="gain"))
    looks = {}

    def measure(setting):
        g = setting["g"]
        looks[g] = looks.get(g, 0) + 1
        v = {0: 10.0, 1: 21.9, 2: 22.0, 3: 12.0, 4: 8.0}[g]
        if g == 1 and looks[g] == 1:
            v = 22.6                                   # the lucky first look
        if g == 1 and looks[g] > 1:
            v = 16.0                                   # ...that does not reproduce
        return Reading(value=v, score=v, n=20, p10=v - 0.1, p90=v + 0.1, alive=True, level_db=-40.0 + 5 * g)
    t = Tuner([axis], spec, pick="max")
    res = t.run(measure)
    assert res.best.setting["g"] == 2, res.log
    assert any("did not reproduce" in line for line in res.log)
    assert res.best.phase == "confirm" and res.best.score == 22.0


def test_plumbing_is_never_claimed_without_a_known_cliff():
    from rxtune.dial import Reading
    from rxtune.verdict import judge
    spec = DialSpec("SNR", cliff=None, settle_s=0, window_s=1)
    axis = Axis.of(KnobSpec("g", "range", 0, 3, 1, sense="gain"))

    def measure(setting):
        v = [3.0, 7.6, 6.0, 2.0][setting["g"]]
        return Reading(value=v, score=v, n=20, p10=v - 0.2, p90=v + 0.2, alive=False)
    t = Tuner([axis], spec, confirm=False)
    v = judge(t.run(measure), spec, t.gain_of)
    assert v.shape is Shape.BELOW_CLIFF and not v.ok


def test_one_failed_knob_does_not_stop_the_others_being_written():
    from rxtune.knob import apply
    box = {"ant": "A", "g": 0}

    class FE:
        def knobs(self):
            return {"antenna": FunctionKnob(KnobSpec("antenna", "choice", choices=("A", "B")),
                                            lambda v: None, lambda: box["ant"]),          # ignores writes
                    "g": FunctionKnob(KnobSpec("g", "range", 0, 9, 1), lambda v: box.update(g=v),
                                      lambda: box["g"])}
    with pytest.raises(KnobWriteError) as e:
        apply(FE(), {"antenna": "B", "g": 7})
    assert box["g"] == 7, "the gain knob must still have been written"
    assert "antenna" in str(e.value)


def test_a_counting_dial_needs_evidence_before_it_may_rank():
    """From the ADS-B hardware runs: 2 and then 5 messages in a whole search."""
    from rxtune.dial import Reading
    from rxtune.verdict import judge
    spec = DialSpec("msg_rate", "msgs/s", continuous_below_cliff=False, settle_s=0, window_s=1)
    axis = Axis.of(KnobSpec("g", "range", 0, 3, 1, sense="gain"))

    def measure(setting):
        n = [0, 3, 2, 0][setting["g"]]
        return Reading(value=n / 10.0, score=n / 10.0, n=max(1, n), p10=n / 10.0, p90=n / 10.0, alive=n > 0)
    t = Tuner([axis], spec, pick="max", confirm=False)
    v = judge(t.run(measure), spec, t.gain_of)
    assert v.shape is Shape.UNPROVEN and not v.ok
    assert not any("decode threshold" in n for n in v.notes)
    assert any("too few to rank" in n for n in v.notes)
