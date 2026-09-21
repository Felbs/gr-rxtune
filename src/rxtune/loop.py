# SPDX-License-Identifier: GPL-3.0-or-later
"""The blocking runner: frontend + dial (+ liveness) -> Verdict."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .dial import Dial, Liveness, Reading
from .knob import Frontend, KnobSpec, KnobWriteError, Setting, apply
from .lock import DeviceLock, NullLock, Yielded


class DecoderDied(RuntimeError):
    """The decoder stopped running. That is not an RF verdict and is never
    reported as one."""
from .measure import Clock, Measurer
from .optimize import Axis, SearchResult, Tuner
from .verdict import Verdict, judge


@dataclass
class TuneReport:
    verdict: Verdict
    result: SearchResult
    senses: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    fixed: Setting = field(default_factory=dict)

    def __str__(self) -> str:
        out = [str(self.verdict)]
        if self.senses:
            out.append("  senses : " + ", ".join(f"{k}={v}" for k, v in self.senses.items()))
        out += [f"  WARNING: {w}" for w in self.warnings]
        return "\n".join(out)


def learn_senses(result: SearchResult) -> Dict[str, str]:
    """Which way does each knob turn? Learned from the raw level, never assumed:
    SDRplay's IFGR is a reduction, an Airspy HF+ attenuator runs -48..0, and an
    index is not a dB. Looks only at cells below the converter's rail."""
    senses: Dict[str, str] = {}
    pts = [p for p in result.points if p.phase != "confirm" and p.reading.level_db == p.reading.level_db
           and p.reading.level_db < -3.0]
    for k, axis in enumerate(result.axes):
        if not axis.spec.ordered or axis.spec.sense == "none":
            continue
        deltas = []
        groups: Dict[tuple, list] = {}
        for p in pts:
            groups.setdefault(p.cell[:k] + p.cell[k + 1:], []).append(p)
        for g in groups.values():
            g.sort(key=lambda p: p.cell[k])
            for a, b in zip(g, g[1:]):
                deltas.append((b.reading.level_db - a.reading.level_db) / (b.cell[k] - a.cell[k]))
        if len(deltas) < 2:
            continue
        mean = sum(deltas) / len(deltas)
        if abs(mean) < 0.05:
            senses[axis.spec.name] = "no-effect"
        else:
            senses[axis.spec.name] = "gain" if mean > 0 else "reduction"
    return senses


def tune(frontend: Frontend, dial: Dial, liveness: Optional[Liveness] = None, *,
         search: Optional[Sequence[str]] = None, fixed: Optional[Setting] = None,
         start: Optional[Dict[str, Sequence]] = None, clock: Optional[Clock] = None,
         lock: Optional[DeviceLock] = None, known_bad: Optional[Callable[[Setting], bool]] = None,
         pick: str = "headroom", coarse_points: int = 5, max_cells: int = 120,
         on_cell: Optional[Callable[[Setting, Reading], None]] = None,
         agc_off: bool = True, thresholds: Optional[dict] = None,
         health: Optional[Callable[[], Optional[str]]] = None,
         measurer=None) -> TuneReport:
    """Search the named knobs (default: every fast gain knob the frontend has),
    holding `fixed` constant. `start` restricts a knob's COARSE span; the
    staircase rule extends it if the answer lies outside."""
    lock = lock or NullLock()
    knobs = frontend.knobs()
    specs: Dict[str, KnobSpec] = {n: k.spec for n, k in knobs.items()}
    if search is None:
        search = [n for n, s in specs.items() if s.cost == "fast" and s.sense != "none"]
    axes = [Axis.of(specs[n], (start or {}).get(n)) for n in search]
    warnings: List[str] = []
    closed = all(getattr(knobs[n], "closed_loop", True) for n in search)

    if agc_off and hasattr(frontend, "set_agc"):
        frontend.set_agc(False)           # a hardware AGC fights the search
        if hasattr(frontend, "verify_agc_off") and not frontend.verify_agc_off():
            warnings.append("hardware AGC still appears active after being switched off")

    if measurer is None:
        measurer = Measurer(dial, frontend, liveness, clock=clock, heartbeat=lock.heartbeat)
    elif getattr(measurer, "heartbeat", "absent") is None:
        measurer.heartbeat = lock.heartbeat          # a custom measurer: anything with .measure()
    current: Setting = {}
    failed: Dict[str, int] = {}
    tuner = Tuner(axes, dial.spec, coarse_points=coarse_points, max_cells=max_cells,
                  pick=pick, known_bad=known_bad)

    def measure(setting: Setting) -> Reading:
        reason = lock.should_yield()
        if reason:
            raise Yielded(reason)
        setting = dict(setting)
        scale = setting.pop("__window_scale__", 1.0)
        try:
            settle = apply(frontend, {**(fixed or {}), **setting}, current)
        except KnobWriteError as e:
            for part in str(e).split("; "):
                failed[part.split(":")[0]] = failed.get(part.split(":")[0], 0) + 1
            settle = getattr(e, "settle_s", 0.0)
        reading = measurer.measure(extra_settle_s=settle + dial.spec.settle_s, window_scale=scale)
        if health is not None:
            problem = health()
            if problem:
                raise DecoderDied(problem)
        if on_cell:
            on_cell(setting, reading)
        return reading

    with lock:
        result = tuner.run(measure)
        senses = learn_senses(result)
        for name, n in failed.items():
            warnings.append(f"{name}: {n} write(s) did not read back - the knob is not doing "
                            "what it was told")
        for name, s in senses.items():
            if s == "no-effect":
                warnings.append(f"{name}: writes had no measurable effect on the raw level "
                                "(ignored by the driver, or overridden by an AGC?)")
        verdict = judge(result, dial.spec, tuner.gain_of, closed_loop=closed and not failed, thresholds=thresholds)
        if verdict.best is not None:
            try:
                apply(frontend, {**(fixed or {}), **verdict.best}, current)   # leave it on the winner
            except KnobWriteError as e:
                warnings.append(str(e))
    return TuneReport(verdict, result, senses, warnings, dict(fixed or {}))
