#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE example, attachment mode (a): rxtune owns the SDR and pipes IQ into an
unmodified nrsc5 (HD Radio). Dial = nrsc5's own MER, the WORSE sideband.
Liveness = nrsc5's "Audio bit rate" line, which it prints only from valid decoded
audio packets (its output FILE keeps growing with nothing decoded: not proof).

  python hw_nrsc5.py --mhz <station MHz> --antenna "<port>" --nrsc5 /path/to/nrsc5

nrsc5 wants cu8 at 1 488 375 Hz on stdin ("-r -"); the radio runs at exactly twice
that and a transform halves it on the way through. Nothing in nrsc5 is touched:
it never opens the radio, so rxtune can move the gain while it decodes.

Set RXTUNE_LOCK to a site lock module if the radio is shared."""
import argparse
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402

from rxtune import lock, profile, soapy  # noqa: E402
from rxtune.adapters import NRSC5_LIVE_RE, Nrsc5MerScraper  # noqa: E402
from rxtune.attach import PipeDecoder  # noqa: E402
from rxtune.loop import tune  # noqa: E402
from rxtune.store import Store, fingerprint  # noqa: E402

FS_NRSC5 = 1488375.0


def halve_to_cu8(raw: np.ndarray) -> bytes:
    """interleaved int16 IQ at 2x -> cu8 at 1x (2-tap average, then decimate)."""
    raw = raw[:(len(raw) // 4) * 4]
    i, q = raw[0::2].astype(np.int32), raw[1::2].astype(np.int32)
    out = np.empty(len(i), np.int32)
    out[0::2] = (i[0::2] + i[1::2]) // 2
    out[1::2] = (q[0::2] + q[1::2]) // 2
    return ((out >> 8) + 128).clip(0, 255).astype(np.uint8).tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mhz", type=float, required=True)
    ap.add_argument("--antenna")
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--nrsc5", default=os.environ.get("NRSC5_EXE") or shutil.which("nrsc5"))
    ap.add_argument("--prog", type=int, default=0)
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=40)
    ap.add_argument("--settle", type=float, default=4.0)
    ap.add_argument("--window", type=float, default=6.0)
    ap.add_argument("--out", help="directory for the stored result (keep it out of the repo)")
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    if not a.nrsc5:
        sys.exit("nrsc5 not found: pass --nrsc5 or set NRSC5_EXE")

    wav = os.path.join(tempfile.gettempdir(), "rxtune_nrsc5.wav")
    if os.path.exists(wav):
        os.unlink(wav)                       # a stale output file would fake liveness
    scraper = Nrsc5MerScraper(live_re=NRSC5_LIVE_RE)
    scraper.spec = scraper.spec.__class__(**{**scraper.spec.__dict__, "settle_s": a.settle,
                                             "window_s": a.window})
    # with -r, nrsc5 takes the PROGRAM only (the frequency belongs to whoever owns the radio)
    dec = PipeDecoder([a.nrsc5, "-r", "-", "-o", wav, str(a.prog)], scraper)
    t0 = time.time()
    with lock.from_env(owner="rxtune-hd", priority=60) as lk:
        fe = soapy.SoapyFrontend(a.args, rate=2 * FS_NRSC5, freq=a.mhz * 1e6)
        prof = profile.find(fe.info)
        fe.apply_profile(prof)
        fixed = {"antenna": a.antenna} if a.antenna else {}
        try:
            fe.start(sink=dec.start(), transform=halve_to_cu8)
            rep = tune(fe, scraper, scraper, lock=lk, fixed=fixed, health=dec.health,
                       search=prof.search or None, coarse_points=a.coarse, max_cells=a.max_cells,
                       known_bad=prof.is_known_bad(), thresholds=prof.thresholds or None,
                       on_cell=lambda s, r: print(
                           f"  {s}  MER {r.value if r.value is None else round(r.value, 2)}  "
                           f"audio {'yes' if r.alive else 'NO'}  level {r.level_db:.1f} dBFS  "
                           f"rails {r.overload:.2f}", flush=True))
            integ = fe.integrity()
        finally:
            fe.stop()
            dec.stop()
            fe.close()
    print(rep)
    print(f"  stream : {integ['ratio']:.3f} of nominal, overflows {integ['overflows']}, "
          f"dropped buffers {integ['dropped_buffers']};  {time.time() - t0:.0f} s total")
    if a.out:
        st = Store(a.out)
        print("  saved  :", st.save(fingerprint(fe.info, fixed, a.label), f"{a.mhz:.1f}", rep.verdict,
                                    rep.result.curve(), {"senses": rep.senses, "stream": integ}))
    return 0 if rep.verdict.ok else 2


if __name__ == "__main__":
    sys.exit(main())
