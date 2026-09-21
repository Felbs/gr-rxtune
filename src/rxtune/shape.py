# SPDX-License-Identifier: GPL-3.0-or-later
"""Failure classification by the SHAPE of the curve.

Every rule is a named function of the measured points; every threshold is in
THRESHOLDS; every rule is proven against the simulator with a positive and a
negative control. All dial arithmetic is in SCORE units (bigger = better,
dB-like), and "gain" means the measured raw level where there is one."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .dial import DialSpec
from .optimize import Point, SearchResult


class Shape(str, Enum):
    HEALTHY = "HEALTHY"
    ISLAND = "ISLAND"
    FADING = "FADING"
    IMPULSE = "IMPULSE"
    PLUMBING = "PLUMBING"
    UNPROVEN = "UNPROVEN"
    APERTURE_LIMITED = "APERTURE_LIMITED"
    GAIN_LIMITED = "GAIN_LIMITED"
    OVERLOAD = "OVERLOAD"
    BELOW_CLIFF = "BELOW_CLIFF"
    NO_SIGNAL = "NO_SIGNAL"
    STARVED = "STARVED"
    BLIND_DIAL = "BLIND_DIAL"


PHYSICAL = {Shape.APERTURE_LIMITED, Shape.FADING, Shape.IMPULSE, Shape.OVERLOAD,
            Shape.GAIN_LIMITED, Shape.STARVED, Shape.NO_SIGNAL}

THRESHOLDS: Dict[str, float] = {
    "flat_eps": 1.0,          # score spread that still counts as "flat"
    "plateau_span_db": 10.0,  # ...over at least this much gain, to call it a plateau
    "rising_slope": 0.25,     # score per dB of gain at the max-gain end = "still rising"
    "fall_db": 2.0,           # drop from the peak toward high gain = an overload ridge
    "clip_high": 0.30,        # share of looks with clipping = steady overload
    "rails_present": 0.15,    # share of looks with clipping = rail transients present
    "fade_spread": 3.0,       # p90-p10 at a FIXED setting
    "island_width_db": 9.0,   # width of the usable region (above the cliff), in gain dB
    "island_within": 1.5,
    "starved_level_db": -22.0,  # max raw level over the whole span; device-dependent, a
                                # profile may override it
}


@dataclass
class ShapeReport:
    shape: Shape
    rule: str                                   # the rule that fired, in words
    notes: List[str] = field(default_factory=list)
    evidence: Dict[str, float] = field(default_factory=dict)


def _nz(x: Optional[float]) -> float:
    return 0.0 if x is None or x != x else x


def classify(result: SearchResult, spec: DialSpec, gain_of: Callable[[Point], float],
             th: Optional[Dict[str, float]] = None) -> ShapeReport:
    th = {**THRESHOLDS, **(th or {})}
    pts = [p for p in result.points if p.phase != "confirm"]
    scored = [p for p in pts if p.score is not None]
    best = result.best
    ev: Dict[str, float] = {}
    notes: List[str] = []

    levels = [p.reading.level_db for p in pts if p.reading.level_db == p.reading.level_db]
    have_level = len(levels) >= 2
    clips = [_nz(p.reading.overload) for p in pts if p.reading.overload == p.reading.overload]
    if have_level:
        ev["level_min_db"], ev["level_max_db"] = min(levels), max(levels)
    # Rail transients that are there at EVERY gain are not overload (they are
    # impulse noise), so overload is judged on what rises above that baseline.
    base = min(clips) if clips else 0.0
    ev["rails_baseline"] = base

    def hot(p: Point) -> float:
        return max(0.0, _nz(p.reading.overload) - base)

    if clips:
        ev["clip_share_cells"] = sum(1 for c in clips if c >= th["clip_high"]) / len(clips)

    # ---- nothing on the dial anywhere ------------------------------------
    no_dial = not scored or (not spec.continuous_below_cliff
                             and all(_nz(p.score) <= 0 for p in scored))
    if no_dial:
        if clips and ev["clip_share_cells"] >= 0.9:
            return ShapeReport(Shape.OVERLOAD, "no dial at any setting and the converter clips at "
                               "every setting, including minimum gain", notes, ev)
        if have_level and max(levels) < th["starved_level_db"]:
            return ShapeReport(Shape.STARVED, "even at maximum gain the converter only reaches "
                               f"{max(levels):.0f} dBFS: the radio hears little more than its own "
                               "noise, so nothing is reaching the input", notes, ev)
        if not spec.continuous_below_cliff:
            return ShapeReport(Shape.BLIND_DIAL, f"'{spec.name}' is zero until decoding starts, so it "
                               "has no gradient to follow below the cliff", notes, ev)
        return ShapeReport(Shape.NO_SIGNAL, "the noise floor moves with gain but no setting produced "
                           "a dial reading", notes, ev)

    assert best is not None
    b = best.reading
    cliff = spec.cliff_score
    above = True if cliff is None else (b.score is not None and b.score >= cliff)
    ev["best_score"] = _nz(b.score)
    ev["best_spread"] = b.spread
    ev["best_rails"] = _nz(b.overload)
    if cliff is not None:
        ev["margin_db"] = _nz(b.score) - cliff

    order = sorted(scored, key=gain_of)
    g_best = gain_of(best)
    top = max(p.score for p in scored)

    # an overload ridge is worth reporting even when the verdict is HEALTHY
    hi_side = [p for p in order if gain_of(p) > g_best]
    ridge = [p for p in hi_side if top - p.score >= th["fall_db"]
             and (hot(p) >= th["clip_high"] or not have_level)]
    silent_hot = [p for p in pts if p.score is None and hot(p) >= th["clip_high"]]
    if ridge or silent_hot:
        first = min(ridge + silent_hot, key=gain_of)
        ev["overload_ridge_at"] = gain_of(first)
        where = (f"above a raw level of about {gain_of(first):.0f} dBFS" if have_level
                 else f"from {first.setting} upward")      # no sample view: name the setting
        notes.append(f"overload ridge: the dial collapses {where}")

    # Width of the USABLE region around the best, in gain dB. With a known cliff that
    # is everything that clears it (a sharp peak with 9 dB of margin is not fragile: a
    # drift costs margin, not the signal). Without one, the near-best region.
    if cliff is not None and above:
        near = [p for p in scored if p.score >= cliff and p.reading.alive is not False]
    else:
        near = [p for p in scored if top - p.score <= th["island_within"]]
    width = (max(map(gain_of, near)) - min(map(gain_of, near))) if len(near) > 1 else 0.0
    ev["good_region_width_db"] = width

    rails_everywhere = _nz(b.overload) >= th["rails_present"] and base >= th["rails_present"]

    if above:
        if b.alive is False:
            return ShapeReport(Shape.PLUMBING, "the dial is healthy and nothing is decoded: the fault "
                               "is downstream of the demodulator, not in the RF", notes, ev)
        if cliff is not None and b.p10 is not None and b.p10 < cliff and b.spread >= th["fade_spread"]:
            return ShapeReport(Shape.FADING, "at a fixed setting the dial swings across the cliff "
                               f"({b.spread:.1f} {spec.units} p10..p90)", notes, ev)
        if rails_everywhere:
            return ShapeReport(Shape.IMPULSE, "rail transients at every gain, including low gain, "
                               "while the dial stays high: impulse noise, not overload", notes, ev)
        if b.alive is None:
            return ShapeReport(Shape.UNPROVEN, "good dial, but no liveness source was supplied, so "
                               "nothing proves content is being decoded", notes, ev)
        lower = [p for p in order if gain_of(p) < g_best and (cliff is None or p.score < cliff)]
        upper = [p for p in order if gain_of(p) > g_best and (cliff is None or p.score < cliff)]
        upper_bad = upper or silent_hot
        if have_level and width < th["island_width_db"] and lower and upper_bad and cliff is not None:
            return ShapeReport(Shape.ISLAND, f"a narrow optimum ({width:.0f} dB wide in gain) with "
                               "failure on both sides", notes, ev)
        return ShapeReport(Shape.HEALTHY, "peak above the cliff with content decoding", notes, ev)

    # ---- below the cliff --------------------------------------------------
    if b.spread >= th["fade_spread"] and (b.p90 or 0) >= cliff - 1.0:
        return ShapeReport(Shape.FADING, "the dial reaches the cliff on its peaks and falls below it "
                           f"in the troughs ({b.spread:.1f} {spec.units} p10..p90)", notes, ev)
    lowest = order[0]
    if hot(best) >= th["clip_high"] or (
            best is lowest and len(order) >= 3 and hot(order[1]) >= th["clip_high"]):
        return ShapeReport(Shape.OVERLOAD, "the best setting is the lowest gain available and the "
                           "converter is still clipping: attenuate ahead of the radio", notes, ev)
    if have_level and len(order) >= 3:
        hi = order[-1]
        span = gain_of(hi) - gain_of(order[0])
        tail = [p for p in order if gain_of(hi) - gain_of(p) <= th["plateau_span_db"]]
        if len(tail) >= 2 and span > 0:
            dg = gain_of(tail[-1]) - gain_of(tail[0])
            slope = (tail[-1].score - tail[0].score) / dg if dg > 1e-6 else 0.0
            ev["slope_at_max_gain"] = slope
            plateau = [p for p in order if top - p.score <= th["flat_eps"]]
            p_span = max(map(gain_of, plateau)) - min(map(gain_of, plateau))
            ev["plateau_span_db"] = p_span
            clean = hot(best) < th["rails_present"]
            if clean and p_span >= th["plateau_span_db"]:
                return ShapeReport(Shape.APERTURE_LIMITED, f"the dial is flat ({th['flat_eps']:.0f} dB) "
                                   f"across {p_span:.0f} dB of gain with no clipping: more gain adds "
                                   "signal and noise alike", notes, ev)
            if clean and hi is max(tail, key=lambda p: p.score) and slope >= th["rising_slope"]:
                return ShapeReport(Shape.GAIN_LIMITED, "the dial is still rising at maximum gain: the "
                                   "receiver's own noise is the limit", notes, ev)
    return ShapeReport(Shape.BELOW_CLIFF, "a peak exists but it is under the cliff", notes, ev)
