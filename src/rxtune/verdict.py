# SPDX-License-Identifier: GPL-3.0-or-later
"""The honest verdict. It states a number, says when the limit is physical,
and refuses to recommend a setting that has not been proven to decode."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .dial import DialSpec
from .knob import Setting
from .optimize import Point, SearchResult
from .shape import PHYSICAL, Shape, ShapeReport, classify

ADVICE = {
    Shape.HEALTHY: "Use this setting. Recalibrate when the RF path changes.",
    Shape.ISLAND: "Usable, but narrow: small drifts will fall off it. Something ahead of the radio "
                  "is too hot (an LNA or a strong neighbour); less gain ahead of the radio would "
                  "widen it.",
    Shape.FADING: "Multipath. Gain cannot fix this: move or re-aim the antenna, or try another "
                  "channel from the same site.",
    Shape.IMPULSE: "Impulse noise. Gain cannot fix this: move the antenna or feedline away from "
                   "the source (switching supplies, USB 3, motors).",
    Shape.PLUMBING: "Not an RF problem. Check what happens after the demodulator: framing, "
                    "pipes, the player.",
    Shape.UNPROVEN: "Supply a liveness source (packets, frames, CRC-good messages) before "
                    "trusting this.",
    Shape.APERTURE_LIMITED: "Software cannot fix this. The limit is set before the radio's gain "
                            "stages: a bigger or better-placed antenna, a shorter feedline, or a "
                            "low-noise preamp AT the antenna.",
    Shape.GAIN_LIMITED: "The radio runs out of gain before the signal clears its own noise. A "
                        "preamp would help here.",
    Shape.OVERLOAD: "Too much signal for the converter even at minimum gain. Add attenuation "
                    "or a filter ahead of the radio, or remove an amplifier.",
    Shape.BELOW_CLIFF: "A peak exists but it is under the decode threshold. Try another antenna "
                       "port, channel or time of day.",
    Shape.NO_SIGNAL: "No carrier the decoder recognises at any setting. Wrong frequency, a "
                     "phantom carrier, or nothing on the air.",
    Shape.STARVED: "Nothing reaches the input: check the port selection, the cable, bias power "
                   "to any active antenna.",
    Shape.BLIND_DIAL: "This dial cannot rescue a signal that does not decode yet. Find the signal "
                      "with a continuous dial (MER, CNR, SNR, Viterbi BER) or a level scan first.",
}


@dataclass
class Verdict:
    shape: Shape
    headline: str
    advice: str
    best: Optional[Setting]            # None = nothing may be recommended
    candidate: Optional[Setting]       # the best cell found, recommended or not
    dial: Optional[float]
    units: str
    margin_db: Optional[float]
    alive: Optional[bool]
    physical: bool
    confidence: str                    # "closed-loop" | "open-loop" | "low-evidence"
    rule: str
    notes: List[str] = field(default_factory=list)
    evidence: Dict[str, float] = field(default_factory=dict)
    when: float = field(default_factory=time.time)
    cells: int = 0

    @property
    def ok(self) -> bool:
        return self.best is not None

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["shape"] = self.shape.value
        d["ok"] = self.ok
        return d

    def __str__(self) -> str:
        lines = [f"VERDICT  {self.shape.value}  -  {self.headline}",
                 f"  why    : {self.rule}"]
        if self.best is not None:
            lines.append(f"  use    : {self.best}")
        elif self.candidate is not None:
            lines.append(f"  NOT recommended (best cell was {self.candidate})")
        lines += [f"  note   : {n}" for n in self.notes]
        lines.append(f"  advice : {self.advice}")
        lines.append(f"  basis  : {self.cells} cells, {self.confidence}")
        return "\n".join(lines)


RECOMMENDABLE = {Shape.HEALTHY, Shape.ISLAND, Shape.IMPULSE, Shape.FADING}


def judge(result: SearchResult, spec: DialSpec, gain_of: Callable[[Point], float],
          closed_loop: bool = True, thresholds: Optional[dict] = None) -> Verdict:
    rep: ShapeReport = classify(result, spec, gain_of, thresholds)
    best = result.best
    b = best.reading if best else None
    dial = b.value if b else None
    margin = rep.evidence.get("margin_db")
    alive = b.alive if b else None

    # THE liveness gate: no setting is recommended unless content was decoded.
    recommend = (rep.shape in RECOMMENDABLE and best is not None and alive is True
                 and (margin is None or margin >= 0))
    u = spec.units
    if dial is None:
        headline = "no dial reading at any setting"
    elif margin is None:
        headline = f"{spec.name} {dial:.1f} {u}" + (", decoding" if alive else "")
    elif margin >= 0:
        headline = f"{spec.name} {dial:.1f} {u}, {margin:+.1f} {u} over the cliff" + (
            ", decoding" if alive else ", but NOTHING DECODED" if alive is False else ", decode unproven")
    else:
        headline = f"{spec.name} {dial:.1f} {u}, {-margin:.1f} {u} short of the cliff"
    if rep.shape in PHYSICAL and not recommend:
        headline += " - this is physical"

    confidence = "closed-loop" if closed_loop else "open-loop (knob writes not read back)"
    if b is not None and b.n < 2 * spec.min_samples:
        confidence = "low-evidence (" + confidence + ")"
    if result.budget_exhausted:
        rep.notes.append("cell budget exhausted before the search converged")

    return Verdict(shape=rep.shape, headline=headline, advice=ADVICE[rep.shape],
                   best=dict(best.setting) if recommend else None,
                   candidate=dict(best.setting) if best else None,
                   dial=dial, units=u, margin_db=margin, alive=alive,
                   physical=rep.shape in PHYSICAL, confidence=confidence, rule=rep.rule,
                   notes=rep.notes, evidence=rep.evidence, cells=len(result.points))
