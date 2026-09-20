# SPDX-License-Identifier: GPL-3.0-or-later
"""Coarse grid -> pattern search (fine sweep + hill-climb in one) -> pick.

The search is a generator: it yields a Setting and is sent a Reading. That
lets one implementation serve the blocking library runner AND the GNU Radio
controller, which must never block a scheduler thread."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Dict, Generator, List, Optional, Sequence, Tuple

from .dial import DialSpec, Reading
from .knob import KnobSpec, Setting, coarse_indices

Cell = Tuple[int, ...]


@dataclass
class Axis:
    spec: KnobSpec
    values: list                       # full resolution, axis order
    start: Optional[Sequence[int]] = None   # restrict the COARSE grid to these indices

    @classmethod
    def of(cls, spec: KnobSpec, start_values: Optional[Sequence] = None) -> "Axis":
        vals = spec.values()
        start = None if start_values is None else [vals.index(v) for v in start_values]
        return cls(spec, vals, start)


@dataclass
class Point:
    cell: Cell
    setting: Setting
    reading: Reading
    phase: str
    reason: str

    @property
    def score(self) -> Optional[float]:
        return self.reading.score


@dataclass
class SearchResult:
    axes: List[Axis]
    points: List[Point] = field(default_factory=list)
    best: Optional[Point] = None
    log: List[str] = field(default_factory=list)
    extended: bool = False
    budget_exhausted: bool = False

    def curve(self) -> List[dict]:
        return [{"setting": p.setting, "phase": p.phase, **p.reading.as_dict()} for p in self.points]


# ---- pick policies ---------------------------------------------------------
def pick_max(points: List[Point], spec: DialSpec, gain_of: Callable[[Point], float]) -> Point:
    return max(points, key=lambda p: p.score)


def pick_headroom(points: List[Point], spec: DialSpec, gain_of) -> Point:
    """Near-ties go to the cell with fewer rail transients, then to less gain:
    margin against the next strong signal is worth more than 0.2 dB of dial."""
    top = max(p.score for p in points)
    near = [p for p in points if top - p.score <= 1.5 * spec.resolution]
    rails = min(_nz(p.reading.overload) for p in near)
    near = [p for p in near if _nz(p.reading.overload) <= rails + 0.05]
    # toward less gain, but not ON the low edge of the plateau: one third of
    # the way up from its bottom
    gains = sorted(gain_of(p) for p in near)
    target = gains[0] + (gains[-1] - gains[0]) / 3.0
    return min(near, key=lambda p: (abs(gain_of(p) - target), -p.score))


def pick_knee(points: List[Point], spec: DialSpec, gain_of, within: float = 1.0) -> Point:
    """Lowest total gain within `within` of the best: the knee, not the peak."""
    top = max(p.score for p in points)
    near = [p for p in points if top - p.score <= within and _nz(p.reading.overload) < 0.05]
    near = near or [p for p in points if top - p.score <= within]
    return min(near, key=lambda p: (gain_of(p), -p.score))


PICKERS = {"max": pick_max, "headroom": pick_headroom, "knee": pick_knee}


def _nz(x: float) -> float:
    return 0.0 if x != x else x


class Tuner:
    """Search over ordered axes. Unordered choice axes (antenna port, config)
    are enumerated in full; ordered axes are coarse-sampled then refined."""

    def __init__(self, axes: Sequence[Axis], spec: DialSpec, coarse_points: int = 5,
                 top_k: int = 2, max_cells: int = 120, pick: str = "headroom",
                 known_bad: Optional[Callable[[Setting], bool]] = None,
                 confirm: bool = True):
        self.axes = list(axes)
        self.spec = spec
        self.coarse_points = coarse_points
        self.top_k = top_k
        self.max_cells = max_cells
        self.pick = pick
        self.known_bad = known_bad or (lambda s: False)
        self.confirm = confirm
        self.result = SearchResult(self.axes)
        self._seen: Dict[Cell, Point] = {}
        self._gen: Optional[Generator] = None
        self._pending: Optional[Tuple[Cell, str, str]] = None

    # ---- stepwise API (GNU Radio controller) -----------------------------
    def propose(self) -> Optional[Setting]:
        if self._gen is None:
            self._gen = self._search()
            try:
                return next(self._gen)
            except StopIteration:
                return None
        raise RuntimeError("propose() called twice without report()")

    def report(self, reading: Reading) -> Optional[Setting]:
        try:
            return self._gen.send(reading)
        except StopIteration:
            return None

    # ---- blocking API ------------------------------------------------------
    def run(self, measure: Callable[[Setting], Reading]) -> SearchResult:
        setting = self.propose()
        while setting is not None:
            setting = self.report(measure(setting))
        return self.result

    # ---- internals -----------------------------------------------------------
    def setting_of(self, cell: Cell) -> Setting:
        return {a.spec.name: a.values[i] for a, i in zip(self.axes, cell)}

    def gain_of(self, p: Point) -> float:
        """Total-gain proxy. The measured raw level is the honest one (it needs
        no knowledge of any knob's sign); knob arithmetic is the fallback."""
        lvl = p.reading.level_db
        if lvl == lvl:
            return lvl
        g = 0.0
        for a, i in zip(self.axes, p.cell):
            g += a.spec.gain_sign() * i / max(1, len(a.values) - 1)
        return g

    def _visit(self, cell: Cell, phase: str, reason: str):
        """Sub-generator: measure one cell unless cached / masked / over budget."""
        if cell in self._seen:
            return self._seen[cell]
        setting = self.setting_of(cell)
        if self.known_bad(setting):
            self.result.log.append(f"skip {setting}: known-bad combination (profile)")
            return None
        if len(self._seen) >= self.max_cells:
            self.result.budget_exhausted = True
            return None
        reading = yield setting
        p = Point(cell, setting, reading, phase, reason)
        self._seen[cell] = p
        self.result.points.append(p)
        val = "silent" if reading.value is None else f"{reading.value:.2f} {self.spec.units}"
        self.result.log.append(f"[{phase}] {setting} -> {val}"
                               f"{'' if reading.alive is None else ' alive' if reading.alive else ' NOT-ALIVE'}"
                               f"  ({reason})")
        return p

    def _scored(self) -> List[Point]:
        return [p for p in self._seen.values() if p.score is not None]

    def _search(self):
        axes = self.axes
        ordered = [k for k, a in enumerate(axes) if a.spec.ordered and len(a.values) > 1]
        coarse = []
        for a in axes:
            n = len(a.values)
            if a.start is not None:
                coarse.append(sorted(a.start))
            elif a.spec.ordered:
                coarse.append(coarse_indices(n, self.coarse_points))
            else:
                coarse.append(list(range(n)))
        # 1. coarse grid, spanning the whole range unless the caller restricted it
        for cell in itertools.product(*coarse):
            yield from self._visit(cell, "coarse", "coarse grid")

        # 2. staircase: best sits on the edge of a RESTRICTED span and the dial
        #    climbs monotonically into that edge -> the answer is outside; extend.
        #    And if the restricted span produced no dial at all, it says nothing
        #    about the radio: fall back to the whole range (the grid-span lesson).
        if not self._scored() and any(axes[k].start is not None for k in ordered):
            self.result.extended = True
            self.result.log.append("the starting span produced no dial reading anywhere -> "
                                   "falling back to the full range of every knob")
            for k in ordered:
                if axes[k].start is not None:
                    coarse[k] = sorted(set(coarse[k]) | set(
                        coarse_indices(len(axes[k].values), self.coarse_points + 2)))
            for cell in itertools.product(*coarse):
                yield from self._visit(cell, "extend", "full-span fallback")
        for k in ordered:
            a = axes[k]
            if a.start is None or not self._scored() or self.result.extended:
                continue
            best = max(self._scored(), key=lambda p: p.score)
            idxs = coarse[k]
            edge_lo, edge_hi = best.cell[k] == idxs[0], best.cell[k] == idxs[-1]
            if not (edge_lo or edge_hi) or len(idxs) < 2:
                continue
            line = [self._seen.get(best.cell[:k] + (i,) + best.cell[k + 1:]) for i in idxs]
            sc = [p.score if p and p.score is not None else float("-inf") for p in line]
            mono = sc == sorted(sc) if edge_hi else sc == sorted(sc, reverse=True)
            finite = [x for x in sc if x != float("-inf")]
            rise = (max(finite) - min(finite)) if len(finite) > 1 else float("inf")
            cliff = self.spec.cliff_score
            troubled = (cliff is not None and best.score < cliff) or _nz(best.reading.overload) >= 0.3
            # a flat plateau that happens to peak on the edge is not a staircase
            if not (mono and rise >= 3.0 and troubled):
                continue
            full = coarse_indices(len(a.values), self.coarse_points + 2)
            extra = [i for i in full if (i > idxs[-1] if edge_hi else i < idxs[0])]
            if not extra:
                continue
            self.result.extended = True
            self.result.log.append(
                f"staircase on {a.spec.name}: dial climbs monotonically into the edge of the "
                f"starting span -> extending to {[a.values[i] for i in extra]}")
            others = [c for j, c in enumerate(coarse) if j != k]
            for i in extra:
                for rest in itertools.product(*others):
                    cell = rest[:k] + (i,) + rest[k:]
                    yield from self._visit(cell, "extend", f"staircase extension on {a.spec.name}")
            coarse[k] = sorted(set(idxs) | set(extra))

        # 3. pattern search from the top-K DISTINCT regions, so that an island
        #    is not lost to a broader, lower ridge. Steps halve (coarse -> fine
        #    -> hill-climb is one mechanism); moves need to beat the hysteresis.
        seeds = self._distinct_seeds(coarse)
        hyst = self.spec.resolution
        for s_n, seed in enumerate(seeds):
            here = seed
            steps = {k: max(1, _spacing(coarse[k]) // 2) for k in ordered}
            while True:
                moved = False
                cands = []
                for k in ordered:
                    for d in (-steps[k], steps[k]):
                        i = here.cell[k] + d
                        if 0 <= i < len(axes[k].values):
                            cell = here.cell[:k] + (i,) + here.cell[k + 1:]
                            p = yield from self._visit(
                                cell, "refine", f"seed {s_n + 1}: {axes[k].spec.name} {d:+d} step(s)")
                            if p is not None and p.score is not None:
                                cands.append(p)
                floor = here.score if here.score is not None else float("-inf")
                better = [p for p in cands if p.score > floor + hyst]
                if better:
                    here = max(better, key=lambda p: p.score)
                    moved = True
                if not moved:
                    if all(v == 1 for v in steps.values()):
                        break
                    steps = {k: max(1, v // 2) for k, v in steps.items()}
                if self.result.budget_exhausted:
                    break

        # 4. pick, then confirm with a longer look (a near-miss must not be
        #    judged on one short window)
        scored = self._scored()
        if not scored:
            return
        best = PICKERS[self.pick](scored, self.spec, self.gain_of)
        self.result.log.append(f"pick[{self.pick}] -> {best.setting} "
                               f"({best.reading.value:.2f} {self.spec.units})")
        if self.confirm:
            reading = yield {**best.setting, "__window_scale__": 2.0}
            if reading.score is not None:
                best = Point(best.cell, best.setting, reading, "confirm", "2x window on the pick")
                self.result.points.append(best)
                self.result.log.append(f"[confirm] {best.setting} -> {reading.value:.2f} "
                                       f"{self.spec.units} p10..p90 {reading.p10:.2f}..{reading.p90:.2f}")
        self.result.best = best

    def _distinct_seeds(self, coarse: List[List[int]]) -> List[Point]:
        scored = sorted(self._scored(), key=lambda p: -p.score)
        seeds: List[Point] = []
        for p in scored:
            if len(seeds) >= self.top_k:
                break
            # "distinct" = not a coarse-grid neighbour of a seed already taken
            near = False
            for s in seeds:
                hops = 0
                for k, (i, j) in enumerate(zip(p.cell, s.cell)):
                    if not self.axes[k].spec.ordered:
                        hops += 0 if i == j else 99
                        continue
                    ci = coarse[k]
                    hops = max(hops, abs(_rank(ci, i) - _rank(ci, j)))
                if hops <= 1:
                    near = True
                    break
            if not near:
                seeds.append(p)
        return seeds


def _spacing(idxs: List[int]) -> int:
    if len(idxs) < 2:
        return 1
    return max(b - a for a, b in zip(idxs, idxs[1:]))


def _rank(idxs: List[int], i: int) -> int:
    return min(range(len(idxs)), key=lambda r: abs(idxs[r] - i))
