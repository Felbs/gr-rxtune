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


_MODES_SNIPPET = (
    "import sys, json, numpy as np; sys.path.insert(0, sys.argv[1]); import adsb; "
    "raw = np.fromfile(sys.argv[2], np.int16); "
    "iq = ((raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 32768.0).astype(np.complex64); "
    "r = adsb.analyze(iq, do_rescue=False); "
    "json.dump({'crc_ok': int(r['crc_ok']), 'candidates': int(r['candidates']), "
    "'secs': len(iq) / 2.0e6}, open(sys.argv[3], 'w'))")


def modes_analyze(tools_dir: str, python: str = "python", tag: str = "rxtune",
                  timeout: float = 120.0) -> Tuple[Callable, Callable]:
    """ADS-B / Mode S, for a decoder module `adsb.py` exposing `analyze(iq, do_rescue)`
    over 2 MS/s complex samples and returning {"crc_ok", "candidates"}.

    dial     = CRC-valid extended-squitter messages per second. A BLIND dial: it is zero
               until decoding starts (DialSpec.continuous_below_cliff must be False).
    liveness = the same count: a message that passed its 24-bit CRC is decoded content.
    One analysis serves both, cached per capture."""
    out_json = os.path.join(tempfile.gettempdir(), f"{tag}_modes.json")
    cache: dict = {}

    def _analyze(path: str) -> Optional[dict]:
        key = (path, os.path.getmtime(path), os.path.getsize(path))
        if cache.get("key") != key:
            cache["key"], cache["val"] = key, _run_json(
                [python, "-c", _MODES_SNIPPET, tools_dir, path, out_json], tools_dir, out_json, timeout)
        return cache["val"]

    lock = __import__("threading").Lock()

    def score(path: str) -> Optional[dict]:
        with lock:
            d = _analyze(path)
        if not d:
            return None
        rate = d["crc_ok"] / max(d["secs"], 1e-9)
        return {"value": rate, "p10": rate, "p90": rate, "n": max(1, d["crc_ok"])}

    def prove(path: str) -> dict:
        with lock:
            d = _analyze(path)
        n = int(d["crc_ok"]) if d else 0
        return {"alive": n > 0, "count": n, "rate": n / max(d["secs"], 1e-9) if d else 0.0,
                "note": f"{n} CRC-valid of {d['candidates'] if d else 0} candidates"}

    return score, prove
