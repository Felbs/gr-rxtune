#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE example: a BLIND dial. ADS-B / Mode S at 1090 MHz with dump1090.

The dial is the rate of CRC-valid messages. It is zero until decoding starts, so
it has no gradient below the cliff: rxtune treats it as a RANGE optimiser, not a
rescuer (DialSpec.continuous_below_cliff=False). Attachment mode (a): rxtune owns
the radio and pipes 2 MS/s unsigned 8-bit IQ into `dump1090 --infile - --raw`.

After tuning it runs a head-to-head on the same sky, interleaved so that traffic
drift hits every contender alike:   rxtune's pick | maximum gain | hardware AGC.

  python hw_adsb_dump1090.py --dump1090 /path/to/dump1090 --antenna "<port>"

Set RXTUNE_LOCK to a site lock module if the radio is shared."""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402

from rxtune import lock, profile, soapy  # noqa: E402
from rxtune.attach import PipeDecoder, RateScraper  # noqa: E402
from rxtune.dial import DialSpec  # noqa: E402
from rxtune.knob import apply  # noqa: E402
from rxtune.loop import tune  # noqa: E402
from rxtune.store import Store, fingerprint  # noqa: E402

RATE, FREQ = 2.0e6, 1090e6


def to_cu8(raw: np.ndarray) -> bytes:
    return ((raw.astype(np.int32) >> 8) + 128).clip(0, 255).astype(np.uint8).tobytes()


def rate_over(scraper, secs, heartbeat):
    n0, t0 = scraper.count(), time.monotonic()
    while time.monotonic() - t0 < secs:
        heartbeat()
        time.sleep(0.5)
    return (scraper.count() - n0) / (time.monotonic() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump1090", required=True)
    ap.add_argument("--antenna")
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--window", type=float, default=15.0, help="traffic is bursty: average long")
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=30)
    ap.add_argument("--rfgr", type=int, nargs=2, default=None)
    ap.add_argument("--duel-secs", type=float, default=60.0)
    ap.add_argument("--duel-rounds", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    spec = DialSpec("msg_rate", "msgs/s", cliff=None, continuous_below_cliff=False,
                    settle_s=1.5, window_s=a.window, min_samples=8, resolution=1.0, poll_s=1.0)
    scraper = RateScraper(spec, r"^\*[0-9A-Fa-f]{14,28};")
    dec = PipeDecoder([a.dump1090, "--infile", "-", "--raw"], scraper, scrape="stdout")
    t0 = time.time()
    with lock.from_env(owner="rxtune-adsb", priority=60) as lk:
        fe = soapy.SoapyFrontend(a.args, rate=RATE, freq=FREQ)
        prof = profile.find(fe.info)
        fe.apply_profile(prof)
        fixed = {"antenna": a.antenna} if a.antenna else {}
        start = {"gain:RFGR": list(range(a.rfgr[0], a.rfgr[1] + 1))} if a.rfgr else None
        try:
            fe.start(sink=dec.start(), transform=to_cu8)
            rep = tune(fe, scraper, scraper, lock=lk, fixed=fixed, health=dec.health, start=start,
                       search=prof.search or None, pick="max", coarse_points=a.coarse,
                       max_cells=a.max_cells, known_bad=prof.is_known_bad(),
                       on_cell=lambda s, r: print(
                           f"  {s}  {'-' if r.value is None else format(r.value, '6.1f')} msgs/s"
                           f"  p10..p90 {r.p10}..{r.p90}  level {r.level_db:.1f} dBFS  rails {r.overload:.2f}",
                           flush=True))
            print(rep)
            duel = {}
            pick = rep.verdict.best or rep.verdict.candidate
            if pick is not None and a.duel_rounds:
                gains = [k for k in fe.knobs() if k.startswith("gain:")]
                top = {k: (fe.knobs()[k].spec.lo if fe.knobs()[k].spec.sense == "reduction"
                           else fe.knobs()[k].spec.hi) for k in gains}
                contenders = {"rxtune pick": ("fixed", pick), "maximum gain": ("fixed", top),
                              "hardware AGC": ("agc", None)}
                print(f"\nhead-to-head, {a.duel_rounds} interleaved rounds of {a.duel_secs:.0f} s:")
                for rnd in range(a.duel_rounds):
                    for name, (kind, setting) in contenders.items():
                        fe.set_agc(kind == "agc")
                        if setting:
                            apply(fe, {**fixed, **setting})
                        time.sleep(2.0)
                        r = rate_over(scraper, a.duel_secs, lk.heartbeat)
                        duel.setdefault(name, []).append(r)
                        print(f"  round {rnd + 1}  {name:<14} {r:7.1f} msgs/s", flush=True)
                fe.set_agc(False)
                for name, rs in duel.items():
                    print(f"  MEAN   {name:<14} {sum(rs) / len(rs):7.1f} msgs/s   {['%.1f' % x for x in rs]}")
            integ = fe.integrity()
        finally:
            fe.stop()
            dec.stop()
            fe.close()
    print(f"  stream : {integ['ratio']:.3f} of nominal, overflows {integ['overflows']}, "
          f"dropped {integ['dropped_buffers']};  {time.time() - t0:.0f} s total")
    if a.out:
        st = Store(a.out)
        print("  saved  :", st.save(fingerprint(fe.info, fixed, a.label), "adsb1090", rep.verdict,
                                    rep.result.curve(), {"senses": rep.senses, "duel": duel}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
