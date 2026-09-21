# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-device PROFILES: data, not code. The loop works blind on any device;
a profile makes it faster and better-worded."""
from __future__ import annotations

import dataclasses
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .knob import KnobSpec, Setting

PROFILE_DIR = Path(__file__).with_name("profiles")
_KNOB_FIELDS = {"sense", "settle_s", "cost", "role", "note", "lo", "hi", "step", "ordered"}


@dataclass
class Profile:
    name: str = "blind"
    match: Dict[str, str] = field(default_factory=dict)
    knobs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    search: List[str] = field(default_factory=list)      # default search axes
    ignore: List[str] = field(default_factory=list)      # never offered to the search
    known_bad: List[Dict[str, Any]] = field(default_factory=list)
    agc_blocks: List[str] = field(default_factory=list)  # knobs ignored while hardware AGC is on
    bias_t: Optional[str] = None                         # settings key, if the device has one
    thresholds: Dict[str, float] = field(default_factory=dict)
    quirks: List[str] = field(default_factory=list)

    def matches(self, info: Dict[str, str]) -> bool:
        return bool(self.match) and all(
            fnmatch.fnmatch(str(info.get(k, "")).lower(), str(v).lower()) for k, v in self.match.items())

    def overlay(self, specs: Dict[str, KnobSpec]) -> Dict[str, KnobSpec]:
        out = {}
        for name, spec in specs.items():
            if any(fnmatch.fnmatch(name, pat) for pat in self.ignore):
                continue
            over = {k: v for k, v in self.knobs.get(name, {}).items() if k in _KNOB_FIELDS}
            out[name] = dataclasses.replace(spec, **over) if over else spec
        return out

    def is_known_bad(self) -> Callable[[Setting], bool]:
        rules = self.known_bad

        def bad(setting: Setting) -> bool:
            for rule in rules:
                conds = {k: v for k, v in rule.items() if k != "why"}
                if conds and all(_cond(setting.get(k), v) for k, v in conds.items()):
                    return True
            return False
        return bad


def _cond(value: Any, want: Any) -> bool:
    if value is None:
        return False
    if isinstance(want, (list, tuple)) and len(want) == 2:
        try:        # [lo, hi] = a range. float() on purpose: PyYAML reads "174.0e6" as a STRING
            return float(want[0]) <= float(value) <= float(want[1])
        except (TypeError, ValueError):
            return False
    return str(value).lower() == str(want).lower()


def load(path: str | Path) -> Profile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    unknown = set(data) - {f.name for f in dataclasses.fields(Profile)}
    if unknown:
        raise ValueError(f"{path}: unknown profile keys {sorted(unknown)}")
    for knob, over in (data.get("knobs") or {}).items():
        bad = set(over) - _KNOB_FIELDS
        if bad:
            raise ValueError(f"{path}: knob {knob}: unknown fields {sorted(bad)}")
    return Profile(**data)


def find(info: Dict[str, str], extra_dirs: Optional[List[Path]] = None) -> Profile:
    """The shipped or user profile matching this device, else the blind one."""
    for d in [*(extra_dirs or []), PROFILE_DIR]:
        for f in sorted(Path(d).glob("*.yaml")):
            p = load(f)
            if p.matches(info):
                return p
    return Profile()
