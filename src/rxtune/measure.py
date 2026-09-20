# SPDX-License-Identifier: GPL-3.0-or-later
"""Settle -> discard -> average. The same discipline whether the dial is
polled (library) or pushed (GNU Radio messages): both go through Accumulator."""
from __future__ import annotations

import math
import time
from typing import Callable, List, Optional

from .dial import Dial, DialSpec, Liveness, Reading
from .knob import Frontend


class Clock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


def _pct(sorted_vals: List[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


class Accumulator:
    """Collects one window. Samples that arrive during the settle period are
    dropped: a number measured before the hardware and the decoder's own loops
    have converged describes the previous setting, not this one."""

    def __init__(self, spec: DialSpec, t0: float, settle_s: Optional[float] = None,
                 window_s: Optional[float] = None):
        self.spec = spec
        self.t_open = t0 + (spec.settle_s if settle_s is None else settle_s)
        self.t_close = self.t_open + (spec.window_s if window_s is None else window_s)
        self.values: List[float] = []
        self.levels: List[float] = []
        self.clips: List[float] = []
        self.live0: Optional[int] = None
        self.live1: Optional[int] = None
        self.dropped = 0

    def add(self, value: Optional[float], t: float) -> None:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        if t < self.t_open:
            self.dropped += 1
            return
        self.values.append(float(value))

    def add_level(self, look: Optional[dict], t: float) -> None:
        if not look or t < self.t_open:
            return
        if look.get("level_db") is not None:
            self.levels.append(float(look["level_db"]))
        if look.get("clip") is not None:
            self.clips.append(float(look["clip"]))

    def add_liveness(self, count: Optional[int], t: float) -> None:
        if count is None or t < self.t_open:
            return
        if self.live0 is None:
            self.live0 = int(count)
        self.live1 = int(count)

    def done(self, t: float) -> bool:
        return t >= self.t_close

    def result(self, t: float, min_live: int = 1) -> Reading:
        spec = self.spec
        r = Reading(value=None, t=t)
        span = max(1e-9, min(t, self.t_close) - self.t_open)
        if self.levels:
            r.level_db = sorted(self.levels)[len(self.levels) // 2]
        if self.clips:
            # share of looks that saw ANY clipping: a rail-transient counter,
            # which is what separates impulse noise from steady overload
            r.overload = sum(1 for c in self.clips if c > 1e-4) / len(self.clips)
        if self.live0 is not None and self.live1 is not None:
            delta = self.live1 - self.live0
            r.alive = delta >= min_live
            r.live_rate = delta / span
        if len(self.values) >= max(1, spec.min_samples):
            scores = sorted(s for s in (spec.score(v) for v in self.values) if s is not None)
            vals = sorted(self.values)
            r.value = vals[len(vals) // 2]
            r.score = spec.score(r.value)
            r.n = len(vals)
            r.p10, r.p90 = _pct(scores, 0.10), _pct(scores, 0.90)
        elif self.values:
            r.n = len(self.values)
            r.note = f"only {r.n} dial samples (< {spec.min_samples}); not enough evidence"
        return r


class Measurer:
    """Polling front end to Accumulator, for library (non-GNU-Radio) use."""

    def __init__(self, dial: Dial, frontend: Optional[Frontend] = None,
                 liveness: Optional[Liveness] = None, clock: Optional[Clock] = None,
                 heartbeat: Optional[Callable[[], None]] = None, min_live: int = 1):
        self.dial, self.frontend, self.liveness = dial, frontend, liveness
        self.clock = clock or Clock()
        self.heartbeat = heartbeat
        self.min_live = min_live

    def measure(self, extra_settle_s: float = 0.0, window_scale: float = 1.0) -> Reading:
        spec = self.dial.spec
        t0 = self.clock.now()
        acc = Accumulator(spec, t0, settle_s=max(spec.settle_s, extra_settle_s),
                          window_s=spec.window_s * window_scale)
        while True:
            t = self.clock.now()
            if acc.done(t):
                break
            acc.add(self.dial.read(), t)
            if self.frontend is not None:
                acc.add_level(self.frontend.level(), t)
            if self.liveness is not None:
                acc.add_liveness(self.liveness.count(), t)
            if self.heartbeat is not None:
                self.heartbeat()
            self.clock.sleep(spec.poll_s)
        return acc.result(self.clock.now(), self.min_live)
