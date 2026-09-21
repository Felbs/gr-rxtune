#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE example, attachment mode (c): the decoder insists on owning the SDR,
so it is restarted for every grid cell. Slow, but it needs nothing from the decoder.

Decoder: the Software-TV-Tuner ATSC 1.0 (8-VSB) live chain. Dial = MER derived from
the equaliser's field-sync training error (fs_err_rms, printed with STVT_EQ_TELEM=1):
MER_dB = 20*log10(5 / fs_err_rms), data cliff 15.2 dB. Liveness = MPEG-2 sequence
headers found in the transport stream it writes (file growth would not do: the chain
can emit full-rate null packets with nothing in them).

  python hw_atsc_stvt.py --tv-live /path/to/tools/tv_live.py --rf 36 --antenna "Antenna A"

The chain is stopped with CTRL_BREAK / SIGINT so that IT closes the radio. Set
RXTUNE_LOCK to a site lock module if the radio is shared."""
import argparse
import dataclasses
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from rxtune import lock  # noqa: E402
from rxtune.adapters import ATSC_MER, atsc_scraper  # noqa: E402
from rxtune.attach import PatternCounter, RestartPerCell, popen_group  # noqa: E402
from rxtune.knob import KnobSpec  # noqa: E402
from rxtune.loop import tune  # noqa: E402
from rxtune.store import Store, fingerprint  # noqa: E402

SEQ_HEADER = b"\x00\x00\x01\xb3"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv-live", required=True)
    ap.add_argument("--rf", type=int, required=True, help="RF channel number")
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--python", default=sys.executable, help="interpreter that can run the chain")
    ap.add_argument("--rfgr", type=int, nargs=2, default=[0, 27])
    ap.add_argument("--ifgr", type=int, nargs=2, default=[20, 59])
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=30)
    ap.add_argument("--settle", type=float, default=14.0, help="includes the chain's start-up")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--dll-dir", default=r"C:\Program Files\SDRplay\API\x64")
    ap.add_argument("--out")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    ts = os.path.join(tempfile.gettempdir(), "rxtune_atsc_live.ts")
    scraper = atsc_scraper()
    scraper.spec = dataclasses.replace(ATSC_MER, settle_s=a.settle, window_s=a.window, min_samples=8)
    live = PatternCounter(ts, SEQ_HEADER)

    def launch(setting):
        if os.path.exists(ts):
            os.unlink(ts)
        env = os.environ.copy()
        if os.path.isdir(a.dll_dir):
            env["PATH"] = a.dll_dir + os.pathsep + env.get("PATH", "")
        env.update({"STVT_ANTENNA": a.antenna, "STVT_IFGR": str(int(setting["gain:IFGR"])),
                    "STVT_RFGAIN_SEL": str(int(setting["gain:RFGR"])), "STVT_EQ": "long",
                    "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
                    "STVT_DABNOTCH": "0" if a.rf < 14 else "1",    # the DAB notch IS TV band III
                    "STVT_RS": "stock", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
                    "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0", "STVT_EQ_TELEM": "1"})
        return popen_group([a.python, "-u", a.tv_live, "--rf", str(a.rf), "--out", ts], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    specs = [KnobSpec("gain:RFGR", "range", a.rfgr[0], a.rfgr[1], 1, sense="reduction", role="regime"),
             KnobSpec("gain:IFGR", "range", a.ifgr[0], a.ifgr[1], 1, sense="reduction", role="level")]
    fe = RestartPerCell(specs, launch, scraper, scrape="stdout")
    t0 = time.time()
    with lock.from_env(owner="rxtune-atsc", priority=60) as lk:
        try:
            rep = tune(fe, scraper, live, lock=lk, search=["gain:RFGR", "gain:IFGR"],
                       coarse_points=a.coarse, max_cells=a.max_cells,
                       on_cell=lambda s, r: print(
                           f"  {s}  MER {r.value if r.value is None else round(r.value, 2)}  "
                           f"p10..p90 {r.p10 if r.p10 is None else round(r.p10, 1)}.."
                           f"{r.p90 if r.p90 is None else round(r.p90, 1)}  n={r.n}  "
                           f"video {'yes' if r.alive else 'NO'} ({r.live_rate:.1f} hdr/s)", flush=True))
        finally:
            fe.stop()
    print(rep)
    print(f"  {time.time() - t0:.0f} s total; decoder hard-killed {fe.hard_kills} time(s) "
          "(0 = it always closed the radio itself)")
    if a.out:
        st = Store(a.out)
        print("  saved  :", st.save(fingerprint({"decoder": "stvt"}, {"antenna": a.antenna}, a.label),
                                    f"rf{a.rf}", rep.verdict, rep.result.curve(), {"senses": rep.senses}))
    return 0 if rep.verdict.ok else 2


if __name__ == "__main__":
    sys.exit(main())
