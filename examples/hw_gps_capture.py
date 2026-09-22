#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tune an SDR for GPS L1 with GPSTuna's own acquisition as the judge.

GPS is 20 dB under the noise floor, so a radio's AGC only ever sees noise: the gain it
settles on is unrelated to the signal, and a strong out-of-band neighbour drags it down.
This is the case where the receiver's own dial has to replace the AGC - here the dial is
GPSTuna's acquisition metric (correlation peak over second peak, per satellite), summed
over the satellites that clear its "strong" threshold. Capture-per-cell: a few seconds of
air each, judged offline in a couple of seconds; "alive" = at least four strong birds, the
minimum for a fix. A real fix (`locate.py --iq`) at the pick is the proof at the end.

  python hw_gps_capture.py --gpstuna /path/to/GPSTuna --antenna "Antenna B" [--baseline-only]

Bias-T is a discovered device setting and is held ON (readback-verified by the knob) for an
active antenna, and switched off at the end. Set RXTUNE_LOCK if the radio is shared.
Nothing about position is computed or stored here."""
import argparse
import json
import os

# one BLAS thread: the judge runs beside the radio's pump thread, and a 32-thread MKL pool under
# GPSTuna's acquisition FFTs starved it (measured: every capture after the first came back empty)
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "2")
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from rxtune import lock, profile, soapy  # noqa: E402
from rxtune.attach import CaptureMeasurer  # noqa: E402
from rxtune.dial import CallableDial, DialSpec  # noqa: E402
from rxtune.loop import tune  # noqa: E402

FS = 2.048e6
FC = 1575.42e6
PRNS = list(range(1, 33))
DOPP = np.arange(-7000, 7001, 250.0)


def make_judges(gt, thr):
    last = {}

    def score(path):
        x = gt.load_seg(path, FS, 0.5, 0.400)
        acq = gt.acquire(x, FS, PRNS, DOPP, 400)
        strong = {p: r["metric"] for p, r in acq.items() if r["metric"] > thr}
        last.clear()
        last.update({p: round(r["metric"], 2) for p, r in acq.items() if r["metric"] > 2.0})
        last["_n"] = len(strong)
        v = float(sum(strong.values()))
        return {"value": v, "p10": v, "p90": v, "n": 1}

    def prove(path):
        n = last.get("_n", 0)                       # score() ran on the same capture
        return {"alive": n >= 4, "rate": float(n), "note": f"{n} strong birds"}

    return score, prove, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpstuna", default=os.environ.get("GPSTUNA_DIR", ""))
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--antenna", default="Antenna B")
    ap.add_argument("--secs", type=float, default=3.0)
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=30)
    ap.add_argument("--thr", type=float, default=float(os.environ.get("GPSTUNA_MIN_METRIC", "3.5")))
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--keep", help="also keep the capture of the LAST cell here (for a fix)")
    a = ap.parse_args()
    if not a.gpstuna:
        ap.error("--gpstuna or GPSTUNA_DIR")
    sys.path.insert(0, a.gpstuna)
    import measure as gt                                          # GPSTuna's own acquisition
    score, prove, last = make_judges(gt, a.thr)
    cap = os.path.join(tempfile.gettempdir(), "rxtune_gps.cs16")
    spec = DialSpec("ACQ", "sum of peak ratios", cliff=None, settle_s=1.0, window_s=a.secs, min_samples=1,
                    resolution=0.5, continuous_below_cliff=True)

    t0 = time.time()
    with lock.from_env(owner="rxtune-gps", priority=60) as lk:
        fe = soapy.SoapyFrontend(a.args, rate=FS, freq=FC)
        prof = profile.find(fe.info)
        fe.apply_profile(prof)
        fixed = {"antenna": a.antenna, "setting:biasT_ctrl": "true"}
        measurer = CaptureMeasurer(fe, spec, cap, a.secs, score, prove, heartbeat=lk.heartbeat)
        try:
            fe.start()
            for k, v in fixed.items():                     # bias-T first, and prove it
                fe.knobs()[k].set(v)
            print(f"bias-T: {fe.knobs()['setting:biasT_ctrl'].get()}   antenna: {fe.knobs()['antenna'].get()}")
            # BASELINE: the driver's default, AGC on - what GPSTuna has always captured with
            fe.set_agc(True)
            time.sleep(1.5)
            r = measurer.measure()
            base = (r.value, dict(last))
            print(f"BASELINE (IF AGC on): ACQ {r.value:.1f}  {last.get('_n', 0)} strong birds  "
                  f"{ {p: m for p, m in last.items() if p != '_n'} }  level {r.level_db:.1f} dBFS", flush=True)
            if a.baseline_only:
                return 0
            rep = tune(fe, CallableDial(spec, lambda: None), None, lock=lk, fixed=fixed,
                       search=prof.search or None, measurer=measurer, pick="max",
                       coarse_points=a.coarse, max_cells=a.max_cells, known_bad=prof.is_known_bad(),
                       on_cell=lambda s, r: print(
                           f"  {s}  ACQ {'-' if r.value is None else format(r.value, '.1f')}  "
                           f"{'FIX-ABLE' if r.alive else 'under 4 birds'}  [{r.note}]  "
                           f"level {r.level_db:.1f} dBFS  rails {r.overload:.2f}", flush=True))
            if a.keep:
                fe.knobs()["setting:biasT_ctrl"].set("true")
                for k, v in rep.verdict.best.items():
                    if k in fe.knobs():
                        fe.knobs()[k].set(v)
                time.sleep(1.5)
                print(f"recording {a.keep} at the pick for a fix ...", flush=True)
                fe.record(a.keep, 120.0)
            integ = fe.integrity()
        finally:
            try:
                fe.knobs()["setting:biasT_ctrl"].set("false")
            except Exception:                                     # noqa: BLE001
                pass
            fe.close()
    print(rep)
    print(f"  baseline (AGC): ACQ {base[0]:.1f}, {base[1].get('_n', 0)} strong birds")
    print(f"  stream : {integ['ratio']:.3f} of nominal, overflows {integ['overflows']}; "
          f"void captures {measurer.void_captures};  {time.time() - t0:.0f} s total")
    out = os.path.join(HERE, "..", "runs", "gps_tune.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"baseline": {"acq": base[0], "prns": base[1]}, "verdict": rep.verdict.as_dict()
                   if hasattr(rep.verdict, "as_dict") else str(rep.verdict),
                   "cells": [dict(s=str(p.setting), v=p.reading.value, alive=p.reading.alive, note=p.reading.note)
                             for p in rep.result.points]}, fh, indent=1, default=str)
    if os.path.exists(cap):
        os.unlink(cap)
    return 0 if rep.verdict.ok else 2


if __name__ == "__main__":
    sys.exit(main())
