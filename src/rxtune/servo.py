# SPDX-License-Identifier: GPL-3.0-or-later
"""After the search: the servo. `tune()` answers "which setting?" once; the
servo answers "and now what?" for as long as the radio runs.

Three rules, taken as IDEAS from the auto-gain loop of adswill/OnAir
(docs/AUTOGAIN.md, "Auto v2"; GPL-3.0; no code was read into this file):

  1. hold while locked - but only while COMFORTABLE. Their first loop froze
     the moment a decoder locked and sat on a marginal gain for hours; the
     second rests only when the decoder is well over its threshold, and
     still looks every 30 s.
  2. re-run on triggers only: no decoded content for N seconds, the truth
     sagged under its settled value, a retune, or a request. Never on a clock.
  3. a step that made the truth worse is undone EXACTLY, and that neighbour
     is not tried again for a growing interval (6, 12, 24, 45 s).

What stays ours: the objective is decoder truth - liveness first, the dial
second (OnAir servos its MER/SNR; we refuse any cell nothing decoded in) -
and every re-search ends in a whole-curve Verdict, with the incumbent
setting measured again beside the new winner before either is kept.

The servo is a generator, like the Tuner: it yields a proposal and is sent
a Reading, so one implementation serves the blocking runner (`serve`) and
the GNU Radio controller, which must never block a scheduler thread. A
proposal is a Setting plus two private keys: `__window_scale__` (as the
Tuner's confirmation uses) and `__wait_s__` (sleep this long at the held
setting before measuring; the rest in rule 1)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Dict, Generator, List, Optional, Sequence, Tuple

from .dial import Dial, DialSpec, Liveness, Reading
from .knob import Frontend, KnobSpec, KnobWriteError, Setting, apply
from .lock import DeviceLock, NullLock, Yielded
from .loop import DecoderDied, learn_senses
from .measure import Clock, Measurer
from .optimize import Axis, Tuner
from .verdict import Verdict, judge

SEARCHING, HOLDING, PROBING = "searching", "holding", "probing"
SIGMA_PER_SPREAD = 1.0 / 2.56          # p10..p90 of a normal spans 2.56 sigma
Cell = Tuple[int, ...]


@dataclass(frozen=True)
class ServoPolicy:
    autonomous: bool = True        # False: watch, report triggers, never move a knob after a search
    comfort: float = 3.0           # score units over the decode threshold that let the loop REST
    reprobe_s: float = 30.0        # at rest, the held setting is still measured this often
    unlock_s: float = 10.0         # no decoded content this long -> re-search
    sag: float = 3.0               # the held truth this far under its settled value ...
    sag_hold_s: float = 10.0       # ... for this long -> re-search (continuous dials only)
    backoff_s: Tuple[float, ...] = (6.0, 12.0, 24.0, 45.0)   # between probes, after each rejection
    improve_sigma: float = 1.5     # a move must beat the held truth by the resolution AND this many sigma
    overload: float = 0.3          # share of clipped looks at the held setting that is an emergency
    probe_step: int = 1            # neighbour distance, in axis steps
    descent_max: int = 12          # overload: at most this many steps down before a search
    fruitless_max: int = 4         # a fruitless re-search doubles the unlock wait, up to 2^this


@dataclass
class Event:
    t: float
    state: str
    reason: str
    setting: Setting
    value: Optional[float] = None
    alive: Optional[bool] = None

    def __str__(self) -> str:
        v = "" if self.value is None else f" {self.value:.2f}"
        a = "" if self.alive is None else (" alive" if self.alive else " NOT-ALIVE")
        return f"[{self.t:8.1f}s {self.state:9}] {self.reason} {self.setting}{v}{a}"


@dataclass
class ServoReport:
    held: Optional[Setting]
    proven: bool
    verdicts: List[Verdict]
    events: List[Event]
    counts: Dict[str, int]
    time: Dict[str, float]
    log: List[str]
    warnings: List[str] = field(default_factory=list)

    @property
    def off_held_share(self) -> float:
        """Share of the time after the first search spent measuring something other
        than the held setting: the servo's cost."""
        on = self.time.get("holding_s", 0.0)
        off = self.time.get("probing_s", 0.0) + self.time.get("research_s", 0.0)
        return off / (on + off) if on + off > 0 else 0.0

    def __str__(self) -> str:
        c, t = self.counts, self.time
        out = [f"SERVO  held {self.held} ({'proven' if self.proven else 'UNPROVEN'})",
               f"  searches {c.get('searches', 0)}  probes {c.get('probes', 0)} "
               f"(kept {c.get('kept', 0)}, reverted {c.get('reverted', 0)}, "
               f"descended {c.get('descended', 0)})  hold windows {c.get('hold_windows', 0)}",
               f"  time   : holding {t.get('holding_s', 0):.0f} s, probing {t.get('probing_s', 0):.0f} s, "
               f"re-searching {t.get('research_s', 0):.0f} s "
               f"-> {100 * self.off_held_share:.1f} % off the held setting"]
        trig = {k[8:]: v for k, v in c.items() if k.startswith("trigger:")}
        if trig:
            out.append("  triggers: " + ", ".join(f"{k} x{v}" for k, v in sorted(trig.items())))
        out += [f"  WARNING: {w}" for w in self.warnings]
        return "\n".join(out)


def truth_better(a: Reading, b: Reading, improve: float) -> bool:
    """Is `a` better than `b` by decoder truth? Liveness first; then the dial,
    by more than `improve` (the resolution and the noise of the two readings)."""
    if a.alive is True and b.alive is False:
        return True
    if a.alive is False and b.alive is True:
        return False
    if a.score is None:
        return False
    if b.score is None:
        return True
    return a.score > b.score + improve


def _sigma(*readings: Optional[Reading]) -> float:
    return max((r.spread for r in readings if r is not None), default=0.0) * SIGMA_PER_SPREAD


def _clipped(r: Reading, limit: float) -> bool:
    return r.overload == r.overload and r.overload >= limit


class Servo:
    """Search -> hold -> (probe | descend | re-search) -> hold ... Stepwise API as the
    Tuner's: propose() once, then report(reading) returns the next proposal, forever."""

    def __init__(self, axes: Sequence[Axis], spec: DialSpec, policy: Optional[ServoPolicy] = None,
                 *, tuner_kw: Optional[dict] = None, closed_loop: Callable[[], bool] = lambda: True,
                 thresholds: Optional[dict] = None, senses: Optional[Dict[str, str]] = None):
        self.axes = list(axes)
        self.spec = spec
        self.policy = policy or ServoPolicy()
        self.tuner_kw = dict(tuner_kw or {})
        self.closed_loop = closed_loop
        self.thresholds = thresholds
        self.senses: Dict[str, str] = dict(senses or {})
        self.state = SEARCHING
        self.held: Optional[Setting] = None
        self.proven = False
        self.settled: Optional[float] = None      # the held truth's settled score
        self.verdicts: List[Verdict] = []
        self.events: List[Event] = []
        self.log: List[str] = []
        self._t_first: Optional[float] = None
        self.counts: Dict[str, int] = {}
        self.time: Dict[str, float] = {}
        self.tuner: Optional[Tuner] = None
        self.last_reason = ""
        self._now = 0.0
        self._request: Optional[str] = None
        self._waiting = False
        self._backoff: Dict[Cell, Tuple[int, float]] = {}
        self._rejections = 0                       # consecutive reverted probes
        self._next_probe_t = 0.0
        self._fruitless = 0                        # consecutive re-searches that recommended nothing
        self._axis_rr = 0
        self._observed_cliff: Optional[float] = None
        self._held_rails = 0.0                     # the clipped share the verdict accepted at the held cell
        self._rails_are_noise = False              # an IMPULSE verdict: rails at every gain, gain cannot fix it
        self._gen: Optional[Generator] = None
        self._stopped = False

    # ---- stepwise API ------------------------------------------------------------
    def propose(self) -> Optional[dict]:
        if self._gen is not None:
            raise RuntimeError("propose() called twice; use report()")
        self._gen = self._run()
        return self._send(None)

    def report(self, reading: Optional[Reading]) -> Optional[dict]:
        return self._send(reading)

    def request(self, reason: str) -> Optional[dict]:
        """Ask for a re-search (a retune, a config change, the user). Returns the
        next proposal at once when the servo was resting in a hold wait, else None:
        the request is honoured when the current window closes."""
        self._request = reason
        if self._waiting and self._gen is not None:
            return self._send(None)
        return None

    def stop(self) -> None:
        self._stopped = True

    def _send(self, reading):
        if self._gen is None:
            return None
        try:
            prop = self._gen.send(reading)
        except StopIteration:
            self._gen = None
            return None
        self._waiting = bool(prop.get("__wait_s__", 0.0) > 0.0)
        return prop

    def run(self, measure: Callable[[dict], Reading], until: Callable[[], bool]) -> None:
        prop = self.propose()
        while prop is not None and not until():
            prop = self.report(measure(prop))

    # ---- bookkeeping ---------------------------------------------------------------
    def _count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def _spent(self, key: str, reading: Reading) -> None:
        if reading.t >= self._now:
            self.time[key] = self.time.get(key, 0.0) + (reading.t - self._now)
            self._now = reading.t

    def _event(self, reason: str, setting: Optional[Setting], reading: Optional[Reading] = None) -> None:
        e = Event(self._now, self.state, reason, dict(setting or {}),
                  None if reading is None else reading.value,
                  None if reading is None else reading.alive)
        self.events.append(e)
        if self._t_first is None:
            self._t_first = e.t
        self.log.append(str(dataclasses.replace(e, t=e.t - self._t_first)))
        self.last_reason = reason

    def _cell_of(self, setting: Optional[Setting]) -> Optional[Cell]:
        if not setting:
            return None
        try:
            return tuple(a.values.index(setting[a.spec.name]) for a in self.axes)
        except (KeyError, ValueError):
            return None

    def _setting_of(self, cell: Cell) -> Setting:
        return {a.spec.name: a.values[i] for a, i in zip(self.axes, cell)}

    def _improve(self, *readings: Optional[Reading]) -> float:
        return max(self.spec.resolution, self.policy.improve_sigma * _sigma(*readings))

    def _threshold(self) -> Optional[float]:
        if self.spec.cliff_score is not None:
            return self.spec.cliff_score
        return self._observed_cliff

    def comfortable(self, score: Optional[float], alive: Optional[bool]) -> bool:
        """Rule 1's condition: decoding, and the dial far enough over the decode
        threshold. With no threshold known (a counting dial, or one never seen to
        fail) decoding alone is comfort: there is nothing to climb toward."""
        if alive is False:
            return False
        thr = self._threshold()
        if thr is None or score is None:
            return score is not None or alive is True
        return score - thr >= self.policy.comfort

    @property
    def cells(self) -> int:
        return len(self.tuner.result.points) if self.tuner else 0

    # ---- the machine -----------------------------------------------------------------
    def _run(self):
        reason = "start"
        while not self._stopped:
            yield from self._search(reason)
            reason = yield from self._hold()

    def _search(self, reason: str):
        self.state = SEARCHING
        self._count("searches")
        self._backoff.clear()
        self._rejections, self._next_probe_t = 0, 0.0
        self.tuner = Tuner(self.axes, self.spec, **self.tuner_kw)
        self._event(f"search: {reason}", self.held)
        key = "search_s" if self.held is None else "research_s"
        prop = self.tuner.propose()
        while prop is not None:
            r = yield prop
            if r is None:
                continue                      # a request during a search waits for the window
            self._spent(key, r)
            prop = self.tuner.report(r)
        result = self.tuner.result
        for k, v in learn_senses(result).items():
            self.senses[k] = v
        verdict = judge(result, self.spec, self.tuner.gain_of, closed_loop=self.closed_loop(),
                        thresholds=self.thresholds)
        self.verdicts.append(verdict)
        hi = verdict.evidence.get("observed_cliff_hi")
        if hi is not None:
            self._observed_cliff = hi
        best = result.best
        new = verdict.best                     # None = nothing may be recommended
        self._fruitless = 0 if new is not None else self._fruitless + 1
        self._rails_are_noise = verdict.shape.value == "IMPULSE"
        if best is not None and best.reading.overload == best.reading.overload:
            self._held_rails = best.reading.overload
        if self.held is None:
            # first search: as tune() does, the verdict's best, else the candidate, unproven
            self.held = dict(new) if new is not None else (dict(best.setting) if best else None)
            self.proven = new is not None
            self.settled = best.score if best else None
            self._event(f"verdict {verdict.shape.value}: holding "
                        f"{'the recommended' if self.proven else 'the UNPROVEN candidate'}",
                        self.held, best.reading if best else None)
            return
        if new is None:
            # the air may be gone or every cell dead: a cell nothing decoded in is not a
            # setting. The incumbent stays; the hold loop keeps asking whether it decodes.
            self._count("kept_incumbent")
            self._event(f"verdict {verdict.shape.value}: nothing recommendable now -> the incumbent stays",
                        self.held, best.reading if best else None)
            self.proven = False
            return
        if new == self.held:
            self.proven, self.settled = True, best.score
            self._event(f"verdict {verdict.shape.value}: the incumbent won again", self.held, best.reading)
            return
        # Rule 3 at the scale of a whole search: the incumbent is measured again NOW, with
        # the same long look the pick's confirmation had, and the new winner replaces it
        # only if it is better by decoder truth. (A re-search was triggered because the
        # held truth moved; its old settled value is stale evidence.)
        r_old = yield {**self.held, "__window_scale__": 2.0}
        if r_old is not None:
            self._spent("research_s", r_old)
        improve = self._improve(best.reading, r_old)
        if r_old is None or truth_better(best.reading, r_old, improve):
            self._count("adopted")
            self.held, self.proven, self.settled = dict(new), True, best.score
            self._event(f"verdict {verdict.shape.value}: new winner adopted "
                        f"(incumbent re-measured {_fmt(r_old)})", self.held, best.reading)
        else:
            self._count("reverted_search")
            self.proven, self.settled = r_old.alive is True, r_old.score
            self._event(f"verdict {verdict.shape.value}: new pick {new} ({_fmt(best.reading)}) did not "
                        f"beat the incumbent by {improve:.2f} {self.spec.units} -> reverted",
                        self.held, r_old)

    def _hold(self):
        """Rules 1 and 2, with rule 3 for the probes. Returns the reason for the next search."""
        pol = self.policy
        if self.held is None:
            # nothing was measurable anywhere: wait, then look again
            self.state = HOLDING
            r = yield {"__wait_s__": pol.unlock_s * 2 ** min(self._fruitless, pol.fruitless_max)}
            if r is not None:
                self._spent("holding_s", r)
            self._count("trigger:nothing_held")
            return "nothing to hold"
        ema = self.settled
        unlock_s = pol.unlock_s * 2 ** min(self._fruitless, pol.fruitless_max)
        unlock_since: Optional[float] = None
        sag_since: Optional[float] = None
        alive_last: Optional[bool] = True if self.proven else None
        while True:
            if self._request:
                reason, self._request = self._request, None
                self._count("trigger:request")
                return reason
            self.state = HOLDING
            rest = self.comfortable(ema, alive_last)
            if rest or not pol.autonomous:
                wait = pol.reprobe_s if rest else 0.0
            elif self._neighbour() is None:
                wait = pol.reprobe_s             # every neighbour is backed off or exhausted: nothing to climb
            else:
                wait = max(0.0, min(pol.reprobe_s, self._next_probe_t - self._now))
            r = yield {**self.held, "__wait_s__": wait}
            if r is None:
                continue                                  # interrupted: a request is waiting
            self._spent("holding_s", r)
            self._count("hold_windows")
            if r.alive is not None:
                alive_last = r.alive
            # -- rule 2: the triggers
            if r.alive is False or (r.alive is None and r.score is None):
                unlock_since = unlock_since if unlock_since is not None else r.t
                if r.t - unlock_since >= unlock_s:
                    self._count("trigger:unlocked")
                    self._event(f"no decoded content for {r.t - unlock_since:.0f} s", self.held, r)
                    if pol.autonomous:
                        return "no decoded content"
                    unlock_since = None
            else:
                unlock_since = None
                if not self.proven and r.alive is True:
                    self.proven, self.settled, ema = True, r.score, r.score
                    self._count("reproven")
                    self._event("the incumbent decodes again", self.held, r)
            if r.score is not None and r.alive is not False:
                ema = r.score if ema is None else ema + 0.5 * (r.score - ema)
                if self.spec.continuous_below_cliff and self.settled is not None \
                        and ema < self.settled - pol.sag:
                    sag_since = sag_since if sag_since is not None else r.t
                    if r.t - sag_since >= pol.sag_hold_s:
                        self._count("trigger:sag")
                        self._event(f"truth sagged {self.settled - ema:.1f} {self.spec.units} under its "
                                    f"settled value", self.held, r)
                        if pol.autonomous:
                            return "truth sagged"
                        sag_since = None
                else:
                    sag_since = None
            # an emergency is rails the verdict did NOT accept: well above what the held
            # cell showed when it was chosen, and never under an IMPULSE verdict
            if not self._rails_are_noise and _clipped(r, max(pol.overload, self._held_rails + pol.overload)):
                self._count("trigger:overload")
                self._event(f"overload at the held setting ({r.overload:.2f} of looks clipped)", self.held, r)
                if pol.autonomous:
                    cleared = yield from self._descend(r)
                    if not cleared:
                        return "overload"
                    ema, alive_last, sag_since, unlock_since = self.settled, self.proven or None, None, None
                continue
            # -- rule 1: rest when comfortable; otherwise rule 3: try a neighbour
            if not pol.autonomous or rest or r.alive is False or r.score is None:
                continue
            if self.comfortable(ema, r.alive) or self._now < self._next_probe_t:
                continue
            nb = self._neighbour()
            if nb is None:
                continue
            kept = yield from self._probe(nb, r, ema, "margin small: trying a neighbour")
            if kept:
                ema, alive_last, sag_since, unlock_since = self.settled, True, None, None

    def _probe(self, cell: Cell, r_held: Reading, ema: Optional[float], why: str):
        """Rule 3: one neighbour, one window; kept only if the truth improves by more
        than the resolution and the noise; else undone exactly and backed off."""
        self.state = PROBING
        self._count("probes")
        setting = self._setting_of(cell)
        self._rotate(cell)
        self._event(why, setting)
        r = yield setting
        if r is None:
            r = Reading(value=None, t=self._now)      # interrupted: counts as no improvement
        self._spent("probing_s", r)
        ref = Reading(value=r_held.value, score=ema if ema is not None else r_held.score,
                      p10=r_held.p10, p90=r_held.p90, alive=r_held.alive, overload=r_held.overload)
        improve = self._improve(r, r_held)
        keep = truth_better(r, ref, improve) and not _clipped(r, self.policy.overload)
        if keep:
            self._count("kept")
            self.held, self.proven, self.settled = setting, r.alive is True, r.score
            self._backoff.clear()
            self._rejections = 0
            gain = (r.score - ref.score) if (r.score is not None and ref.score is not None) else float("nan")
            self._event(f"kept: better by {gain:+.2f} {self.spec.units} (needed {improve:.2f})", setting, r)
            return True
        n, _ = self._backoff.get(cell, (0, 0.0))
        self._backoff[cell] = (n + 1, r.t + self.policy.backoff_s[min(n, len(self.policy.backoff_s) - 1)])
        self._rejections += 1
        gap = self.policy.backoff_s[min(self._rejections - 1, len(self.policy.backoff_s) - 1)]
        self._next_probe_t = r.t + gap
        self._count("reverted")
        self._event(f"reverted: {_fmt(r)} vs held {_fmt(ref)}, needed {improve:+.2f}; "
                    f"next probe in {gap:.0f} s", self.held, r)
        return False

    def _descend(self, r_held: Reading):
        """An overload at the held setting: step toward less gain until the rails
        clear (OnAir cuts at once and proportionally; one axis step per window is
        our proportional). A step that LOSES decoded content the clipped cell still
        had is undone: rails with content is impulse noise, not overload."""
        for _ in range(self.policy.descent_max):
            nb = self._neighbour(less_gain=True)
            if nb is None:
                return False
            self.state = PROBING
            self._count("probes")
            self._count("descended")
            setting = self._setting_of(nb)
            self._event("overload: one step less gain", setting)
            r = yield setting
            if r is None:
                return False
            self._spent("probing_s", r)
            lost = r.alive is False or (r.score is not None and r_held.score is not None
                                        and r.score < r_held.score - self.policy.sag)
            if r_held.alive is True and lost:
                self._count("reverted")
                self._event("the step cost the truth the clipped cell still had -> reverted (impulse noise, "
                            "not overload)", self.held, r)
                self._backoff[nb] = (1, r.t + self.policy.backoff_s[-1])
                self._held_rails = max(self._held_rails, r_held.overload if r_held.overload == r_held.overload else 0.0)
                return True
            self.held = setting
            self.proven, self.settled = r.alive is True, r.score
            if not _clipped(r, self.policy.overload):
                self._held_rails = r.overload if r.overload == r.overload else 0.0
                self._event(f"overload cleared ({r.overload:.2f} of looks clipped)", setting, r)
                return True
            r_held = r
        return False

    def _rotate(self, cell: Cell) -> None:
        here = self._cell_of(self.held)
        if here is None:
            return
        for k, (i, j) in enumerate(zip(cell, here)):
            if i != j:
                ordered = [m for m, a in enumerate(self.axes) if a.spec.ordered and len(a.values) > 1]
                if k in ordered:
                    self._axis_rr = (ordered.index(k) + 1) % len(ordered)
                return

    def _neighbour(self, less_gain: bool = False) -> Optional[Cell]:
        """The next untried neighbour of the held cell: ordered axes round-robin, more
        gain first (the margin is small; sensitivity before linearity), then less."""
        cell = self._cell_of(self.held)
        if cell is None:
            return None
        ordered = [k for k, a in enumerate(self.axes) if a.spec.ordered and len(a.values) > 1]
        if not ordered:
            return None
        known_bad = self.tuner_kw.get("known_bad") or (lambda s: False)
        for j in range(len(ordered)):
            k = ordered[(self._axis_rr + j) % len(ordered)]
            a = self.axes[k]
            sense = self.senses.get(a.spec.name, a.spec.sense)
            sign = -1 if sense == "reduction" else 1          # +sign = more gain
            dirs = [-sign] if less_gain else [sign, -sign]
            for d in dirs:
                i = cell[k] + d * self.policy.probe_step
                if not 0 <= i < len(a.values):
                    continue
                nb = cell[:k] + (i,) + cell[k + 1:]
                _, until = self._backoff.get(nb, (0, -1e9))
                if until > self._now or known_bad(self._setting_of(nb)):
                    continue
                return nb
        return None

    def report_out(self, warnings: Optional[List[str]] = None) -> ServoReport:
        return ServoReport(self.held, self.proven, list(self.verdicts), list(self.events),
                           dict(self.counts), dict(self.time), list(self.log), list(warnings or []))


def _fmt(r: Optional[Reading]) -> str:
    if r is None:
        return "no reading"
    v = "silent" if r.value is None else f"{r.value:.2f}"
    return v + ("" if r.alive is None else " alive" if r.alive else " not-alive")


def serve(frontend: Frontend, dial: Dial, liveness: Optional[Liveness] = None, *,
          duration_s: float, policy: Optional[ServoPolicy] = None,
          search: Optional[Sequence[str]] = None, fixed: Optional[Setting] = None,
          start: Optional[Dict[str, Sequence]] = None, clock: Optional[Clock] = None,
          lock: Optional[DeviceLock] = None, known_bad: Optional[Callable[[Setting], bool]] = None,
          pick: str = "headroom", coarse_points: int = 5, max_cells: int = 120,
          on_event: Optional[Callable[[str, Setting, Reading], None]] = None,
          agc_off: bool = True, thresholds: Optional[dict] = None,
          health: Optional[Callable[[], Optional[str]]] = None, measurer=None,
          until: Optional[Callable[[], bool]] = None, servo: Optional[Servo] = None) -> ServoReport:
    """The blocking runner: tune(), then hold / probe / re-search for `duration_s`
    counted from the first verdict (or until `until()` says so). Leaves the radio
    on the held setting."""
    lock = lock or NullLock()
    clock = clock or Clock()
    policy = policy or ServoPolicy()
    knobs = frontend.knobs()
    specs: Dict[str, KnobSpec] = {n: k.spec for n, k in knobs.items()}
    if search is None:
        search = [n for n, s in specs.items() if s.cost == "fast" and s.sense != "none"]
    axes = [Axis.of(specs[n], (start or {}).get(n)) for n in search]
    warnings: List[str] = []
    closed = all(getattr(knobs[n], "closed_loop", True) for n in search)
    if agc_off and hasattr(frontend, "set_agc"):
        frontend.set_agc(False)
        if hasattr(frontend, "verify_agc_off") and not frontend.verify_agc_off():
            warnings.append("hardware AGC still appears active after being switched off")
    if measurer is None:
        measurer = Measurer(dial, frontend, liveness, clock=clock, heartbeat=lock.heartbeat)
    elif getattr(measurer, "heartbeat", "absent") is None:
        measurer.heartbeat = lock.heartbeat
    current: Setting = {}
    failed: Dict[str, int] = {}
    if servo is None:
        servo = Servo(axes, dial.spec, policy,
                      tuner_kw=dict(coarse_points=coarse_points, max_cells=max_cells, pick=pick,
                                    known_bad=known_bad),
                      closed_loop=lambda: closed and not failed, thresholds=thresholds)
    t_end: List[Optional[float]] = [None]      # armed by the first verdict

    def measure(prop: dict) -> Reading:
        reason = lock.should_yield()
        if reason:
            raise Yielded(reason)
        setting = dict(prop)
        scale = setting.pop("__window_scale__", 1.0)
        wait = setting.pop("__wait_s__", 0.0)
        try:
            settle = apply(frontend, {**(fixed or {}), **setting}, current)
        except KnobWriteError as e:
            for part in str(e).split("; "):
                failed[part.split(":")[0]] = failed.get(part.split(":")[0], 0) + 1
            settle = getattr(e, "settle_s", 0.0)
        while wait > 0:                       # the rest, in slices, keeping the lock alive
            step = min(wait, 1.0)
            clock.sleep(step)
            wait -= step
            lock.heartbeat()
        reading = measurer.measure(extra_settle_s=settle + dial.spec.settle_s, window_scale=scale)
        if health is not None:
            problem = health()
            if problem:
                raise DecoderDied(problem)
        if on_event:
            on_event(servo.state, setting, reading)
        if t_end[0] is None and servo.state != "searching":
            t_end[0] = clock.now() + duration_s      # the hold starts at the verdict
        return reading

    with lock:
        servo.run(measure, lambda: (t_end[0] is not None and clock.now() >= t_end[0])
                   or (until is not None and until()))
        if servo.held is not None:
            try:
                apply(frontend, {**(fixed or {}), **servo.held}, current)
            except KnobWriteError as e:
                warnings.append(str(e))
    for name, n in failed.items():
        warnings.append(f"{name}: {n} write(s) did not read back - the knob is not doing what it was told")
    return servo.report_out(warnings)
