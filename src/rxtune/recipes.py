# SPDX-License-Identifier: GPL-3.0-or-later
"""Recipes: how to judge a CAPTURE with a particular decoder's own tools.

A recipe is a pair of callables, `score(path) -> {"value","p10","p90","n"} | None`
and `prove(path) -> {"alive","rate","note"} | None`. They are what
attach.CaptureMeasurer and the GNU Radio Capture Dial block both consume."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Callable, Optional, Tuple

ATSC3_RATE = 6.912e6


def us_tv_centre_hz(rf: int) -> float:
    """Centre of a 6 MHz channel in the US TV channel plan (RF 7-13, 14-36)."""
    lo = (174 + (rf - 7) * 6) if rf < 14 else (470 + (rf - 14) * 6)
    return (lo + 3.0) * 1e6


def _run_json(argv, cwd: str, out_json: str, timeout: float) -> Optional[dict]:
    if os.path.exists(out_json):
        os.unlink(out_json)                  # a stale result must never be read as this cell's
    try:
        subprocess.run(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if not os.path.exists(out_json):
        return None
    with open(out_json, encoding="utf-8") as f:
        return json.load(f)


def atsc3(receiver_dir: str, python: str = "python", rate: float = ATSC3_RATE,
          min_decoded: float = 0.9, tag: str = "rxtune",
          timeout: float = 75.0) -> Tuple[Callable, Callable]:
    """ATSC 3.0 (NextGen TV), for a receiver tree that provides

        lab/e86_snr.py CAPTURE --rate R --frames N --json OUT
        python -m atsc3 watch --capture CAPTURE --rate R --player none --json OUT

    dial     = median SNR read off each frame's known +/-1 dummy cells: no constellation
               hypothesis, and continuous far below the decode cliff (FEC% is not - near
               threshold it is a cliff, and reports drift as a verdict).
    liveness = the capture is actually decoded; alive when the share of LDPC blocks that
               come out BCH-clean is at least `min_decoded`."""
    tmp = tempfile.gettempdir()
    j_snr, j_dec = os.path.join(tmp, f"{tag}_snr.json"), os.path.join(tmp, f"{tag}_dec.json")

    def score(path: str) -> Optional[dict]:
        d = _run_json([python, os.path.join("lab", "e86_snr.py"), path, "--rate", str(rate),
                       "--frames", "200", "--json", j_snr], receiver_dir, j_snr, timeout)
        return None if not d else {"value": d["median"], "p10": d["p10"], "p90": d["p90"], "n": d["n"]}

    def prove(path: str) -> dict:
        d = _run_json([python, "-m", "atsc3", "watch", "--capture", path, "--rate", str(rate),
                       "--player", "none", "--json", j_dec], receiver_dir, j_dec, timeout)
        if not d or not d.get("fec_total"):
            return {"alive": False, "rate": 0.0, "count": 0, "note": "no FEC blocks: never acquired"}
        clean = int(d.get("bch_zero", 0))
        share = clean / d["fec_total"]
        return {"alive": share >= min_decoded, "rate": clean / max(d.get("air_s", 1.0), 1e-9),
                "count": clean, "note": f"BCH-clean {clean}/{d['fec_total']} ({100 * share:.1f}%)"}

    return score, prove
