# SPDX-License-Identifier: GPL-3.0-or-later
"""The servo, proven with no radio: each of its three rules with a positive and a
negative control, against the simulated receiver and its timeline (the air changes
while a setting is held). Every assertion is a number the report also prints."""
from rxtune.dial import Reading
from rxtune.knob import KnobSpec
from rxtune.optimize import Axis
from rxtune.servo import SEARCHING, HOLDING, PROBING, Servo, ServoPolicy, serve, truth_better
from rxtune.sim import SimReceiver

FIXED = {"antenna": "A"}


def run(name, duration_s=1500.0, policy=None, **kw):
    rx = SimReceiver(name)
    rep = serve(rx, rx, rx, duration_s=duration_s, clock=rx.clock, fixed=FIXED, policy=policy, **kw)
    return rx, rep


def first_verdict_t(rep):
    return next(e.t for e in rep.events if e.reason.startswith("verdict"))


def state_matches(rx, setting):
    return all(rx.state[k] == v for k, v in setting.items())


# ------------------------------------------------------------ rule 1: hold while comfortable
def test_comfortable_hold_moves_no_knob():
    rx, rep = run("healthy")
    v = rep.verdicts[0]
    assert v.shape.value == "HEALTHY" and rep.proven
    assert rep.counts["searches"] == 1 and rep.counts.get("probes", 0) == 0
    assert rep.off_held_share == 0.0
    # the hold still LOOKS (rule 1's second half): about one window per reprobe + window
    held_s = rep.time["holding_s"]
    assert rep.counts["hold_windows"] >= held_s / 60.0
    assert state_matches(rx, rep.held)


def test_hold_writes_nothing_after_the_search():
    """Knob writes during the hold = 0: the search's writes are the only ones."""
    writes_at = {}
    rx = SimReceiver("healthy")

    def on_event(state, setting, reading):
        writes_at.setdefault(state, []).append(rx.n_writes)

    rep = serve(rx, rx, rx, duration_s=1500.0, clock=rx.clock, fixed=FIXED, on_event=on_event)
    holds = writes_at[HOLDING]
    assert len(holds) >= 20
    assert holds[0] == holds[-1], "the knob moved while holding"
    assert "probing" not in writes_at


def test_small_margin_is_not_comfort():
    """Negative control for rule 1: just over the cliff the loop may not rest - it
    probes (rule 3 then undoes every probe exactly, since the plateau is flat)."""
    rx, rep = run("near_cliff")
    v = rep.verdicts[0]
    assert v.shape.value == "HEALTHY" and 0 < v.margin_db < 3.0, str(v)
    assert rep.counts.get("probes", 0) >= 3
    assert rep.counts.get("kept", 0) == 0, "a flat plateau has nothing to climb"
    assert rep.counts["reverted"] == rep.counts["probes"]
    # every revert put the radio back on the held setting, exactly
    for e in rep.events:
        if e.reason.startswith("reverted"):
            assert e.setting == rep.held
    assert state_matches(rx, rep.held)
    # the cost is bounded: rejected probes back off 6, 12, 24, 45 s
    assert rep.off_held_share < 0.5, str(rep)
    t0 = first_verdict_t(rep)
    probes = [e.t for e in rep.events if e.state == PROBING and e.reason.startswith("margin")]
    gaps = [b - a for a, b in zip(probes, probes[1:])]
    assert len(gaps) >= 2 and max(gaps) >= 45.0, gaps


# ------------------------------------------------------------ rule 2: re-run on triggers only
def test_dropout_triggers_a_search_and_the_incumbent_is_judged_again():
    rx, rep = run("dropout")
    t_v = first_verdict_t(rep)
    assert t_v < 600.0, "the air drops at 600 s, after the first verdict"
    unl = [e for e in rep.events if e.reason.startswith("no decoded content")]
    assert len(unl) >= 1
    pol = ServoPolicy()
    # detected within one rest + the unlock wait + two windows of the drop
    assert 600.0 <= unl[0].t <= 600.0 + pol.reprobe_s + pol.unlock_s + 2 * 12.0, unl[0]
    assert rep.counts["searches"] == 2
    # the air was gone throughout the re-search: nothing recommendable, so the
    # incumbent STAYS (a cell nothing decoded in is not a setting) ...
    assert not rep.verdicts[-1].ok and rep.counts.get("kept_incumbent", 0) == 1
    # ... and when the air returns the hold proves it again without a third search
    assert rep.counts.get("reproven", 0) == 1
    assert rep.proven and state_matches(rx, rep.held)
    assert rep.held == rep.verdicts[0].best


def test_no_trigger_no_search():
    """Negative control for rule 2: a steady air gets one search in 1500 s."""
    _, rep = run("healthy")
    assert rep.counts["searches"] == 1
    assert not [k for k in rep.counts if k.startswith("trigger:")]


def test_counting_dial_ignores_a_traffic_drop():
    """A message-rate dial follows the traffic of the hour, so its sag is not a
    trigger (the ADS-B lesson); liveness is."""
    _, rep = run("traffic_drops")
    assert rep.verdicts[0].ok
    assert rep.counts["searches"] == 1
    assert "trigger:sag" not in rep.counts
    values = [e.value for e in rep.events if e.value is not None]
    assert values  # the dial did move


def test_slow_fade_sag_then_unlock():
    """The signal fades to the cliff (sag -> re-search, the incumbent judged again),
    then under it (unlock -> re-search finds nothing recommendable -> the incumbent
    stays, and fruitless searches back off instead of looping)."""
    rx, rep = run("slow_fade", duration_s=3000.0)
    trig = {k[8:]: v for k, v in rep.counts.items() if k.startswith("trigger:")}
    assert trig.get("sag", 0) >= 1, trig
    assert trig.get("unlocked", 0) >= 1, trig
    assert rep.counts.get("kept_incumbent", 0) >= 1
    assert not rep.proven            # honest: nothing decodes at the end
    assert state_matches(rx, rep.held)
    # rule 3 at the search level: after the sag the incumbent was re-measured beside
    # the new pick, and one of them was kept on decoder truth
    assert rep.counts.get("adopted", 0) + rep.counts.get("reverted_search", 0) == 1
    searches = [e.t for e in rep.events if e.reason.startswith("search:")]
    assert len(searches) >= 3
    # the fruitless re-searches space out: the unlock wait doubles each time
    waits = [float(e.reason.split()[4]) for e in rep.events if e.reason.startswith("no decoded content")]
    assert len(waits) >= 2 and waits == sorted(waits) and waits[-1] > waits[0], waits


def test_request_interrupts_a_rest():
    """A retune request while resting returns the next proposal at once."""
    rx = SimReceiver("healthy")
    specs = {n: k.spec for n, k in rx.knobs().items()}
    axes = [Axis.of(specs[n]) for n in ("gain:RF", "gain:IF")]
    servo = Servo(axes, rx.spec)
    from rxtune.knob import apply
    from rxtune.measure import Measurer
    m = Measurer(rx, rx, rx, clock=rx.clock)
    current = {}

    def measure(prop):
        s = dict(prop)
        scale = s.pop("__window_scale__", 1.0)
        wait = s.pop("__wait_s__", 0.0)
        apply(rx, {**FIXED, **s}, current)
        rx.clock.sleep(wait)
        return m.measure(window_scale=scale)

    prop = servo.propose()
    while servo.state == SEARCHING:
        prop = servo.report(measure(prop))
    assert servo.state == HOLDING and prop.get("__wait_s__", 0) > 0, prop
    nxt = servo.request("retune")
    assert nxt is not None and servo.state == SEARCHING
    assert servo.counts["trigger:request"] == 1 and servo.counts["searches"] == 2
    # and a request during a WINDOW waits for it
    assert servo.request("again") is None


def test_watch_only_reports_triggers_and_moves_nothing():
    """autonomous=False: the triggers are counted and logged, no knob moves."""
    rx = SimReceiver("dropout")
    writes = []
    rep = serve(rx, rx, rx, duration_s=1500.0, clock=rx.clock, fixed=FIXED,
                policy=ServoPolicy(autonomous=False),
                on_event=lambda st, s, r: writes.append((st, rx.n_writes)))
    assert rep.counts["searches"] == 1
    assert rep.counts.get("trigger:unlocked", 0) >= 1
    hold_writes = [n for st, n in writes if st == HOLDING]
    assert hold_writes[0] == hold_writes[-1]


# ------------------------------------------------------------ rule 3: undo a worsening step
def test_truth_better_is_liveness_first():
    live = Reading(value=16.0, score=16.0, alive=True)
    dead = Reading(value=24.0, score=24.0, alive=False)
    assert truth_better(live, dead, 0.2) and not truth_better(dead, live, 0.2)
    a, b = Reading(value=20.0, score=20.0, alive=True), Reading(value=20.1, score=20.1, alive=True)
    assert not truth_better(b, a, 0.2), "inside the resolution is a tie"
    assert truth_better(Reading(value=20.5, score=20.5, alive=True), a, 0.2)
    assert truth_better(a, Reading(value=None), 0.2) and not truth_better(Reading(value=None), a, 0.2)


def test_neighbour_arrives_overload_descent_then_island():
    """A hot neighbour appears under a held high gain: rails at the held setting ->
    step down until they clear (never settle on a clipped cell), and when nothing
    decodes there a re-search finds the island; the loop then probes around it and
    undoes every step that is not better."""
    rx, rep = run("neighbour_arrives", duration_s=2000.0)
    assert rep.verdicts[0].shape.value == "HEALTHY"
    assert rep.counts.get("trigger:overload", 0) >= 1
    assert rep.counts.get("descended", 0) >= 1
    assert rep.proven, str(rep)
    assert rep.verdicts[-1].shape.value in ("ISLAND", "HEALTHY")
    # the radio ends on the held cell, which decodes
    assert state_matches(rx, rep.held)
    assert rx.read() is not None and rx.count() > 0
    # no probe was kept that was not better by the resolution and the noise
    kept = [e for e in rep.events if e.reason.startswith("kept")]
    for e in kept:
        assert "needed" in e.reason
    assert rep.counts.get("reverted", 0) >= 1
    assert rep.off_held_share < 0.75, str(rep)


def test_impulse_rails_are_not_an_overload():
    """Negative control for the descent: the verdict already said the rails are at
    every gain (IMPULSE) and accepted them at the pick; the servo must not read them
    as an overload and walk the gain down a 30 dB dial."""
    rx, rep = run("impulse")
    assert rep.verdicts[0].shape.value == "IMPULSE" and rep.proven
    assert "trigger:overload" not in rep.counts, str(rep)
    assert rep.counts["searches"] == 1 and rep.counts.get("descended", 0) == 0
    assert rep.proven and state_matches(rx, rep.held)
    assert rx.count() > 0


def test_descent_reverts_a_step_that_costs_the_truth():
    """The descent on a REAL overload still undoes a step that loses decoded content
    or `sag` of the dial the clipped cell still had (rule 3 inside the emergency)."""
    specs = {n: k.spec for n, k in SimReceiver("healthy").knobs().items()}
    axes = [Axis.of(specs[n]) for n in ("gain:RF", "gain:IF")]
    servo = Servo(axes, SimReceiver("healthy").spec)
    servo.held = {"gain:RF": 2, "gain:IF": 40}
    servo.senses = {"gain:RF": "reduction", "gain:IF": "reduction"}
    held = Reading(value=24.0, score=24.0, alive=True, overload=0.6, p10=23.5, p90=24.5)
    gen = servo._descend(held)
    step = next(gen)
    assert step in ({"gain:RF": 3, "gain:IF": 40}, {"gain:RF": 2, "gain:IF": 41}), step
    # the step clears the rails but the dial falls 8 dB with content still there -> reverted
    worse = Reading(value=16.0, score=16.0, alive=True, overload=0.0, t=10.0)
    try:
        gen.send(worse)
        cleared = None
    except StopIteration as stop:
        cleared = stop.value
    assert cleared is True
    assert servo.held == {"gain:RF": 2, "gain:IF": 40}
    assert servo.counts.get("reverted", 0) == 1


def test_until_ends_a_hold_rest_within_a_second():
    """A viewer who leaves must not wait out the hold's rest: `until` is looked at in every
    one-second slice of it. (Found live: a stop request took 96 s to release the radio, because
    the hold only tested `until` between looks, about every 48 s, and then the chain had to close.)"""
    rx = SimReceiver("healthy")
    seen = {}

    def on_event(state, setting, reading):
        if state == HOLDING:
            seen.setdefault("hold_t", rx.clock.now())

    def until():                       # the viewer leaves 3 s into the first rest
        return "hold_t" in seen and rx.clock.now() >= seen["hold_t"] + 3.0

    rep = serve(rx, rx, rx, duration_s=1e9, clock=rx.clock, fixed=FIXED, on_event=on_event, until=until)
    late = rx.clock.now() - (seen["hold_t"] + 3.0)
    assert late <= 1.5, f"stopped {late:.1f} s after being asked"
    assert rep.held and state_matches(rx, rep.held), "the radio is left on the held setting"
