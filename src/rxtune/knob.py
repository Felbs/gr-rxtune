# SPDX-License-Identifier: GPL-3.0-or-later
"""The Knob: anything the loop may turn. Discovered from the device, supplied
by a decoder, or virtual (the hour of day). No per-device code lives here."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional, Protocol, Sequence, Tuple, runtime_checkable

Setting = Dict[str, Any]


class KnobWriteError(RuntimeError):
    """set() was accepted but get() disagrees. Readback or it did not happen."""


@dataclass(frozen=True)
class KnobSpec:
    name: str                              # "gain:IFGR", "antenna", "setting:biasT_ctrl", "freq", "config"
    kind: str = "range"                    # "range" | "choice"
    lo: Optional[float] = None
    hi: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[Tuple[Any, ...]] = None
    ordered: bool = True                   # a choice list with a meaningful order (an RTL gain table)
    sense: str = "unknown"                 # "gain" | "reduction" | "unknown" | "none"
    settle_s: float = 0.1
    cost: str = "fast"                     # "fast" | "slow" | "restart"
    role: str = "level"                    # "level" | "regime" | "path" | "config" | "virtual"
    note: str = ""

    def values(self) -> list:
        """Every legal value, in axis order."""
        if self.kind == "choice":
            return list(self.choices or ())
        if self.lo is None or self.hi is None:
            raise ValueError(f"{self.name}: range knob without lo/hi")
        lo, hi = float(self.lo), float(self.hi)
        if hi < lo:
            lo, hi = hi, lo
        step = abs(self.step) if self.step else (hi - lo) / 40.0 or 1.0
        n = int(round((hi - lo) / step))
        out = [lo + i * step for i in range(n + 1)]
        if out[-1] < hi - 1e-9:
            out.append(hi)
        return [int(v) if float(v).is_integer() and float(step).is_integer() else round(v, 6)
                for v in out]

    def gain_sign(self) -> int:
        """+1 if a larger value means more gain, -1 if less, 0 if not a gain."""
        return {"gain": 1, "reduction": -1, "unknown": 1}.get(self.sense, 0)

    def with_(self, **kw) -> "KnobSpec":
        return replace(self, **kw)


@runtime_checkable
class Knob(Protocol):
    spec: KnobSpec

    def set(self, value: Any) -> None: ...
    def get(self) -> Any: ...


@dataclass
class FunctionKnob:
    """A knob built from two callables. `getter=None` means no readback is
    possible; the knob then reports itself as open-loop."""
    spec: KnobSpec
    setter: Callable[[Any], None]
    getter: Optional[Callable[[], Any]] = None
    tolerance: float = 0.51                # readback tolerance for range knobs

    @property
    def closed_loop(self) -> bool:
        return self.getter is not None

    def set(self, value: Any) -> None:
        self.setter(value)
        if self.getter is None:
            return
        got = self.getter()
        if not _same(got, value, self.tolerance):
            raise KnobWriteError(f"{self.spec.name}: wrote {value!r}, read back {got!r}")

    def get(self) -> Any:
        return None if self.getter is None else self.getter()


def _same(got: Any, want: Any, tol: float) -> bool:
    try:
        return abs(float(got) - float(want)) <= tol
    except (TypeError, ValueError):
        return str(got).strip().lower() == str(want).strip().lower()


@runtime_checkable
class Frontend(Protocol):
    """Owns the radio (or stands in for it). The loop only ever sees this."""

    def knobs(self) -> Dict[str, Knob]: ...

    def level(self) -> Optional[dict]:
        """Raw-sample look, independent of any decoder:
        {"level_db": dBFS, "clip": 0..1}. None if samples are not visible."""


def apply(frontend: Frontend, setting: Setting, current: Optional[Setting] = None) -> float:
    """Write only what changed; return the longest settle time incurred."""
    knobs = frontend.knobs()
    settle = 0.0
    for name, value in setting.items():
        if current is not None and current.get(name) == value:
            continue
        knob = knobs[name]
        knob.set(value)
        settle = max(settle, knob.spec.settle_s)
        if current is not None:
            current[name] = value
    return settle


def coarse_indices(n: int, points: int) -> list:
    """`points` indices spanning 0..n-1 INCLUDING both ends. The whole span is
    the point: a passive antenna on a lossy port lives at max gain, a hot LNA
    chain lives at max attenuation, and a grid that starts in the middle reads
    both as dead."""
    if n <= points:
        return list(range(n))
    return sorted({int(round(i * (n - 1) / (points - 1))) for i in range(points)})


def describe(specs: Sequence[KnobSpec]) -> str:
    rows = []
    for s in specs:
        rng = (f"{s.lo:g}..{s.hi:g} step {s.step:g}" if s.kind == "range" and s.step
               else f"{s.lo:g}..{s.hi:g}" if s.kind == "range"
               else "{" + ", ".join(map(str, s.choices or ())) + "}")
        rows.append(f"  {s.name:<28} {rng:<34} sense={s.sense:<9} role={s.role:<7} "
                    f"cost={s.cost:<7} settle={s.settle_s:g}s {s.note}")
    return "\n".join(rows)
