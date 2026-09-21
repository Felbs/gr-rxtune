#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE example: a BLIND dial. ADS-B / Mode S at 1090 MHz, capture flavour.

The dial is the rate of CRC-valid messages. It is zero until decoding starts, so it
has no gradient below the cliff: rxtune treats it as a RANGE optimiser, not a rescuer
(DialSpec.continuous_below_cliff=False). rxtune owns the radio, records a few seconds
per cell, and a Mode S analyzer judges the file (rxtune.recipes.modes_analyze).

Then a head-to-head on the same sky, interleaved so that traffic drift hits every
contender alike:   rxtune's pick | maximum gain | hardware AGC.

  python hw_adsb_capture.py --tools /path/to/dir/with/adsb.py --antenna "<port>"

WHY NOT A PIPE INTO dump1090? Two lessons from trying: (1) at a realistic level of
-56 dBFS a Mode S pulse is under ONE LSB of 8 bits, so converting 16-bit samples to
the customary unsigned-8 format destroyed the signal (every cell read zero messages
while another decoder heard three aircraft); (2) the only dump1090 build to hand
crashed on 16-bit file input. Feed decoders the radio's native sample width.

Set RXTUNE_LOCK to a site lock module if the radio is shared."""
import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from rxtune import lock, profile, recipes, soapy  # noqa: E402
from rxtune.attach import CaptureMeasurer  # noqa: E402
from rxtune.dial import CallableDial, DialSpec  # noqa: E402
from rxtune.knob import apply  # noqa: E402
from rxtune.loop import tune  # noqa: E402
from rxtune.store import Store, fingerprint  # noqa: E402

RATE, FREQ = 2.0e6, 1090e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", required=True, help="directory containing the Mode S analyzer (adsb.py)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--antenna")
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--secs", type=float, default=10.0, help="traffic is bursty: record long")
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=24)
    ap.add_argument("--rfgr", type=int, nargs=2, default=None)
    ap.add_argument("--duel-rounds", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    spec = DialSpec("msg_rate", "msgs/s", cliff=None, continuous_below_cliff=False,
                    settle_s=1.0, window_s=a.secs, min_samples=1, resolution=0.3)
    score, prove = recipes.modes_analyze(a.tools, a.python)
    cap = os.path.join(tempfile.gettempdir(), "rxtune_adsb.cs16")
    t0 = time.time()
    duel = {}
    with lock.from_env(owner="rxtune-adsb", priority=60) as lk:
        fe = soapy.SoapyFrontend(a.args, rate=RATE, freq=FREQ)
        prof = profile.find(fe.info)
        fe.apply_profile(prof)
        fixed = {"antenna": a.antenna} if a.antenna else {}
        start = {"gain:RFGR": list(range(a.rfgr[0], a.rfgr[1] + 1))} if a.rfgr else None
        m = CaptureMeasurer(fe, spec, cap, a.secs, score, prove, heartbeat=lk.heartbeat)
        try:
            fe.start()
            rep = tune(fe, CallableDial(spec, lambda: None), None, lock=lk, fixed=fixed, start=start,
                       search=prof.search or None, measurer=m, pick="max", coarse_points=a.coarse,
                       max_cells=a.max_cells, known_bad=prof.is_known_bad(),
                       on_cell=lambda s, r: print(
                           f"  {s}  {'-' if r.value is None else format(r.value, '5.1f')} msgs/s  [{r.note}]"
                           f"  level {r.level_db:.1f} dBFS  rails {r.overload:.2f}", flush=True))
            print(rep)
            pick = rep.verdict.best or rep.verdict.candidate
            if pick is not None and a.duel_rounds:
                gains = [k for k in fe.knobs() if k.startswith("gain:")]
                top = {k: (fe.knobs()[k].spec.lo if fe.knobs()[k].spec.sense == "reduction"
                           else fe.knobs()[k].spec.hi) for k in gains}
                contenders = {"rxtune pick": ("fixed", pick), "maximum gain": ("fixed", top),
                              "hardware AGC": ("agc", None)}
                print(f"\nhead-to-head, {a.duel_rounds} interleaved rounds of {a.secs:.0f} s captures:")
                for rnd in range(a.duel_rounds):
                    for name, (kind, setting) in contenders.items():
                        fe.set_agc(kind == "agc")
                        if setting:
                            apply(fe, {**fixed, **setting})
                        r = m.measure()
                        duel.setdefault(name, []).append(r.value or 0.0)
                        print(f"  round {rnd + 1}  {name:<14} {r.value or 0.0:6.1f} msgs/s  [{r.note}]"
                              f"  level {r.level_db:.1f} dBFS", flush=True)
                fe.set_agc(False)
                for name, rs in duel.items():
                    print(f"  MEAN   {name:<14} {sum(rs) / len(rs):6.1f} msgs/s   {['%.1f' % x for x in rs]}")
            integ = fe.integrity()
        finally:
            fe.close()
    print(f"  stream : {integ['ratio']:.3f} of nominal, overflows {integ['overflows']}; "
          f"void captures {m.void_captures};  {time.time() - t0:.0f} s total")
    if a.out:
        st = Store(a.out)
        print("  saved  :", st.save(fingerprint(fe.info, fixed, a.label), "adsb1090", rep.verdict,
                                    rep.result.curve(), {"senses": rep.senses, "duel": duel}))
    if os.path.exists(cap):
        os.unlink(cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
