#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE example, attachment mode (a), capture flavour: ATSC 3.0 (NextGen TV).

rxtune owns the SDR. At every grid cell it records a few seconds of IQ, and the
receiver's OWN offline tools judge the file, both at once:

  dial      SNR read off the frame's known +/-1 dummy cells. No constellation
            hypothesis, continuous far below the decode cliff - which FEC% is not:
            near threshold FEC% is a cliff, and turns a fraction of a dB of drift
            into a 0%-or-100% verdict.
  liveness  the capture is actually DECODED: LDPC blocks converged and BCH-clean.
            Baseband frames that pass BCH are content, not a status line.

  python hw_atsc3_capture.py --atsc3 /path/to/atsc3 --rf <channel> --antenna "<port>"

Expects an ATSC 3.0 receiver tree providing `lab/e86_snr.py CAPTURE --rate R --json OUT`
and `python -m atsc3 watch --capture CAPTURE --rate R --player none --json OUT`.
Set RXTUNE_LOCK to a site lock module if the radio is shared."""
import argparse
import dataclasses
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from rxtune import lock, profile, recipes, soapy  # noqa: E402
from rxtune.attach import CaptureMeasurer  # noqa: E402
from rxtune.dial import CallableDial, DialSpec  # noqa: E402
from rxtune.loop import tune  # noqa: E402
from rxtune.store import Store, fingerprint  # noqa: E402

RATE = recipes.ATSC3_RATE
centre_hz = recipes.us_tv_centre_hz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--atsc3", required=True, help="root of the ATSC 3.0 receiver tree")
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--antenna")
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--python", default=sys.executable, help="interpreter that can run the receiver")
    ap.add_argument("--secs", type=float, default=5.0, help="capture length per cell")
    ap.add_argument("--cliff", type=float, default=None, help="decode threshold in dB for this ModCod, if known")
    ap.add_argument("--rfgr", type=int, nargs=2, default=None, help="restrict the LNA-state span")
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=34)
    ap.add_argument("--pick", default="headroom")
    ap.add_argument("--min-decoded", type=float, default=0.9, help="BCH-clean share that counts as decoding")
    ap.add_argument("--ports", nargs="+", help="survey these antenna ports instead of one --antenna")
    ap.add_argument("--out")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    tmp = tempfile.gettempdir()
    cap = os.path.join(tmp, "rxtune_atsc3.cs16")
    spec = DialSpec("SNR", "dB", cliff=a.cliff, settle_s=1.0, window_s=a.secs, min_samples=6,
                    resolution=0.2)

    # the judges are a reusable recipe (the GNU Radio Capture Dial block uses the same pair)
    score, prove = recipes.atsc3(a.atsc3, a.python, RATE, min_decoded=a.min_decoded)

    t0 = time.time()
    with lock.from_env(owner="rxtune-atsc3", priority=60) as lk:
        fe = soapy.SoapyFrontend(a.args, rate=RATE, freq=centre_hz(a.rf))
        prof = profile.find(fe.info)
        fe.apply_profile(prof)
        fixed = {"antenna": a.antenna} if a.antenna else {}
        measurer = CaptureMeasurer(fe, spec, cap, a.secs, score, prove, heartbeat=lk.heartbeat)
        start = {"gain:RFGR": list(range(a.rfgr[0], a.rfgr[1] + 1))} if a.rfgr else None
        try:
            fe.start()
            if a.ports:
                from rxtune.stages import survey
                sv = survey(fe, CallableDial(spec, lambda: None), None, paths={"antenna": a.ports},
                            search=prof.search or None, top=1, lock=lk, start=start, measurer=measurer,
                            pick=a.pick, coarse_points=a.coarse, max_cells=a.max_cells,
                            store=Store(a.out) if a.out else None, label=a.label, device=fe.info)
                print(sv)
                fe.close()
                return 0 if sv.winner and sv.winner.verdict.ok else 2
            rep = tune(fe, CallableDial(spec, lambda: None), None, lock=lk, fixed=fixed,
                       search=prof.search or None, start=start, measurer=measurer, pick=a.pick,
                       coarse_points=a.coarse, max_cells=a.max_cells, known_bad=prof.is_known_bad(),
                       on_cell=lambda s, r: print(
                           f"  {s}  SNR {'-' if r.value is None else format(r.value, '.2f')} dB"
                           f"  spread {r.spread:.2f}  n={r.n}  {'DECODES' if r.alive else 'no decode'}"
                           f"  [{r.note}]  level {r.level_db:.1f} dBFS  rails {r.overload:.2f}", flush=True))
            integ = fe.integrity()
        finally:
            fe.close()
    print(rep)
    print(f"  stream : {integ['ratio']:.3f} of nominal, overflows {integ['overflows']}; "
          f"void captures {measurer.void_captures};  {time.time() - t0:.0f} s total")
    if a.out:
        st = Store(a.out)
        fp = fingerprint(fe.info, fixed, a.label)
        for pt in rep.result.points:                 # every cell, with its hour: the time axis
            st.log(fp, f"rf{a.rf}", pt.setting, pt.reading.as_dict())
        print("  saved  :", st.save(fingerprint(fe.info, fixed, a.label), f"rf{a.rf}", rep.verdict,
                                    rep.result.curve(), {"senses": rep.senses}))
    if os.path.exists(cap):
        os.unlink(cap)
    return 0 if rep.verdict.ok else 2


if __name__ == "__main__":
    sys.exit(main())
