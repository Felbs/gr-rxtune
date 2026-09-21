# SPDX-License-Identifier: GPL-3.0-or-later
"""Results are perishable. They are stored against a fingerprint of the RF
path, never reused across a path change, and they age. Every measurement is
also logged with its hour, because the hour is a knob the loop cannot turn
but can learn."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .verdict import Verdict


def fingerprint(device: Dict[str, str], path: Dict[str, Any], label: str = "") -> str:
    """Identity of an RF path: device + port + bias + anything the user says
    distinguishes it (an antenna label). Change any of it -> recalibrate."""
    blob = json.dumps({"device": device, "path": {k: str(v) for k, v in sorted(path.items())},
                       "label": label}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Staleness:
    stale: bool
    reason: str


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)       # mkdir only; never clears anything

    # ---- per-path result profiles -------------------------------------------
    def _file(self, fp: str, target: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in target)
        return self.root / f"{fp}_{safe}.json"

    def save(self, fp: str, target: str, verdict: Verdict, curve: List[dict],
             extra: Optional[dict] = None) -> Path:
        doc = {"fingerprint": fp, "target": target, "when": time.time(),
               "hour": time.localtime().tm_hour, "verdict": verdict.as_dict(),
               "curve": curve, **(extra or {})}
        f = self._file(fp, target)
        f.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        return f

    def load(self, fp: str, target: str) -> Optional[dict]:
        f = self._file(fp, target)
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

    def check(self, fp: str, target: str, max_age_s: float = 6 * 3600,
              dial_now: Optional[float] = None, sag: float = 1.5) -> Staleness:
        """Recalibration triggers: never calibrated on this path, too old, or
        the live dial has sagged below what was stored."""
        doc = self.load(fp, target)
        if doc is None:
            return Staleness(True, "no calibration for this RF path (new or changed path)")
        if not doc["verdict"].get("ok"):
            return Staleness(True, "the stored calibration did not produce a recommended setting")
        age = time.time() - doc["when"]
        if age > max_age_s:
            return Staleness(True, f"calibration is {age / 3600:.1f} h old")
        stored = doc["verdict"].get("dial")
        if dial_now is not None and stored is not None and dial_now < stored - sag:
            return Staleness(True, f"dial has sagged {stored - dial_now:.1f} below the calibrated value")
        return Staleness(False, "fresh")

    # ---- the hour as a knob ------------------------------------------------------
    def log(self, fp: str, target: str, setting: dict, reading: dict) -> None:
        rec = {"t": time.time(), "hour": time.localtime().tm_hour, "fp": fp,
               "target": target, "setting": setting, **reading}
        with open(self.root / "measurements.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def hour_curve(self, fp: str, target: str) -> Dict[int, float]:
        """Median dial per local hour for one path + target."""
        f = self.root / "measurements.jsonl"
        by: Dict[int, List[float]] = {}
        if not f.exists():
            return {}
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("fp") == fp and r.get("target") == target and r.get("value") is not None:
                by.setdefault(int(r["hour"]), []).append(float(r["value"]))
        return {h: sorted(v)[len(v) // 2] for h, v in sorted(by.items())}
