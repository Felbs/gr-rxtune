# SPDX-License-Identifier: GPL-3.0-or-later
"""The stage pipeline. Order matters and was paid for:

  1 rate probe      can the host keep up? (a starved USB link looks like a bad antenna)
  2 census          what else is in the passband? (an interferer sets the gain ceiling)
  3 path scan       every port x carrier, cheaply
  4 gain grid       the full search, on the paths worth it
  5 channel survey  (the carrier axis of 3+4: multipath is channel-specific)
  6 config shootout recovery configs, judged by CONTENT once the dial stops discriminating
  7 verdict         honest, in dB, stored against the RF path

Every stage is optional. One knob and one dial still gets stages 4 and 7."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .dial import Dial, Liveness
from .knob import Frontend, Setting, apply
from .loop import TuneReport, tune
from .measure import Clock, Measurer
from .store import Store, fingerprint


@dataclass
class ArmStats:
    """Per-arm running statistics for the shootout (after Elkadi's bandit)."""
    pulls: int = 0
    total: float = 0.0
    values: List[float] = field(default_factory=list)

    def add(self, x: float) -> None:
        self.pulls += 1
        self.total += x
        self.values.append(x)

    @property
    def mean(self) -> float:
        return self.total / self.pulls if self.pulls else float("-inf")


@dataclass
class SurveyReport:
    rate: Optional[dict] = None
    census: Optional[dict] = None
    scans: List[dict] = field(default_factory=list)          # stage 3, one row per path
    tunes: List[TuneReport] = field(default_factory=list)    # stage 4, best first
    shootout: Dict[str, ArmStats] = field(default_factory=dict)
    shootout_metric: str = ""
    config: Optional[Any] = None
    warnings: List[str] = field(default_factory=list)
    saved: List[str] = field(default_factory=list)

    @property
    def winner(self) -> Optional[TuneReport]:
        return self.tunes[0] if self.tunes else None

    def __str__(self) -> str:
        out = []
        if self.rate:
            out.append(f"rate probe : ratio {self.rate['ratio']:.3f}, overflows {self.rate['overflows']}"
                       f" -> {'ok' if self.rate.get('ok') else 'HOST CANNOT KEEP UP'}")
        if self.census and self.census.get("ok"):
            out.append(f"census     : strongest in-band feature {self.census['peak_over_floor_db']:.0f} dB "
                       "over the floor")
        for row in self.scans:
            out.append(f"scan       : {row['path']} -> {row['shape']}  {row['headline']}")
        for t in self.tunes:
            out.append(f"--- {t.fixed} ---\n{t}")
        if self.shootout:
            out.append(f"shootout ({self.shootout_metric}):")
            for name, arm in sorted(self.shootout.items(), key=lambda kv: -kv[1].mean):
                out.append(f"  {name!s:<24} mean {arm.mean:8.2f} over {arm.pulls} pull(s)")
            out.append(f"  -> config {self.config!r}")
        out += [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(out)


def survey(frontend: Frontend, dial: Dial, liveness: Optional[Liveness] = None, *,
           paths: Optional[Dict[str, Sequence]] = None, search: Optional[Sequence[str]] = None,
           fixed: Optional[Setting] = None, top: int = 2, config_knob: Optional[str] = None,
           config_reps: int = 2, clock: Optional[Clock] = None, store: Optional[Store] = None,
           label: str = "", device: Optional[Dict[str, str]] = None, probe_rate: Optional[float] = None,
           **tune_kw) -> SurveyReport:
    rep = SurveyReport()
    fixed = dict(fixed or {})

    # 1. rate probe  2. census - only if the frontend can see samples
    if probe_rate and hasattr(frontend, "probe_rate"):
        rep.rate = frontend.probe_rate(probe_rate)
        if not rep.rate.get("ok"):
            rep.warnings.append("the host drops samples at this rate; every later number is suspect "
                                "until that is fixed (cable, port, rate)")
    if hasattr(frontend, "census"):
        rep.census = frontend.census()

    # 3. path scan: every port x carrier, coarse cells only
    combos = [dict(zip(paths, vals)) for vals in itertools.product(*paths.values())] if paths else [{}]
    ranked = []
    for path in combos:
        if len(combos) == 1:
            ranked.append((0.0, path))
            break
        quick = tune(frontend, dial, liveness, search=search, fixed={**fixed, **path}, clock=clock,
                     coarse_points=3, max_cells=9, **{k: v for k, v in tune_kw.items() if k != "max_cells"})
        v = quick.verdict
        key = (v.ok, v.dial is not None, v.evidence.get("best_score", float("-inf")))
        ranked.append((key, path))
        rep.scans.append({"path": path, "shape": v.shape.value, "headline": v.headline})
    if len(combos) > 1:
        ranked.sort(key=lambda kp: kp[0], reverse=True)

    # 4+5. full gain grid on the paths worth it
    for _, path in ranked[:max(1, top)]:
        t = tune(frontend, dial, liveness, search=search, fixed={**fixed, **path}, clock=clock, **tune_kw)
        rep.tunes.append(t)
        if store is not None:
            fp = fingerprint(device or {}, {**fixed, **path}, label)
            for p in t.result.points:
                store.log(fp, dial.spec.name, p.setting, p.reading.as_dict())
            rep.saved.append(str(store.save(fp, dial.spec.name, t.verdict, t.result.curve(),
                                            {"senses": t.senses, "warnings": t.warnings})))
    rep.tunes.sort(key=lambda t: (t.verdict.ok, t.verdict.evidence.get("best_score", float("-inf"))),
                   reverse=True)

    # 6. config shootout at the winner, judged by content when the dial cannot be trusted across configs
    win = rep.winner
    if config_knob and win is not None and win.verdict.candidate is not None:
        knob = frontend.knobs()[config_knob]
        use_live = liveness is not None and (not dial.spec.comparable_across_configs
                                             or win.verdict.margin_db is None or win.verdict.margin_db > 0)
        rep.shootout_metric = "liveness rate" if use_live else f"{dial.spec.name} score"
        m = Measurer(dial, frontend, liveness, clock=clock)
        base = {**win.fixed, **win.verdict.candidate}
        for _ in range(config_reps):                  # interleaved reps: drift hits every arm alike
            for choice in knob.spec.values():
                settle = apply(frontend, {**base, config_knob: choice})
                r = m.measure(extra_settle_s=settle + dial.spec.settle_s)
                x = r.live_rate if use_live else (r.score if r.score is not None else float("-inf"))
                rep.shootout.setdefault(choice, ArmStats()).add(x if x == x else 0.0)
        rep.config = max(rep.shootout, key=lambda c: rep.shootout[c].mean)
        apply(frontend, {**base, config_knob: rep.config})
    return rep
