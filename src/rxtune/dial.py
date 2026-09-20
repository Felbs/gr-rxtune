# SPDX-License-Identifier: GPL-3.0-or-later
"""The Dial: any scalar quality metric a decoder can report, plus what the
loop needs to know about it to measure it honestly."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class DialSpec:
    name: str                          # "MER", "CNR", "BER", "crc_rate"
    units: str = "dB"
    higher_is_better: bool = True
    cliff: Optional[float] = None      # decode threshold in dial units, if known
    settle_s: float = 2.0              # discard this long after any knob write
    window_s: float = 4.0              # then average this long
    # True : MER / CNR / Es-N0 / Viterbi BER - has a gradient below the cliff,
    #        so it can RESCUE a signal that does not decode yet.
    # False: CRC / message-rate dials (AIS, ADS-B) - zero until decoding, so
    #        they can only optimise range on a signal that already decodes.
    continuous_below_cliff: bool = True
    # False when a config knob changes what the number means (an averaging
    # depth inside the estimator, say). The shootout then scores by liveness.
    comparable_across_configs: bool = True
    transform: str = "linear"          # "log10": score = -10*log10(v) (BER-like dials)
    resolution: float = 0.2            # smallest difference worth acting on, in score units
    min_samples: int = 5               # minimum-evidence guard
    poll_s: float = 0.2

    def score(self, value: Optional[float]) -> Optional[float]:
        """Map a dial value onto 'bigger is better, roughly dB-like'."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        if self.transform == "log10":
            v = -10.0 * math.log10(max(float(value), 1e-12))
            return v if not self.higher_is_better else -v
        return float(value) if self.higher_is_better else -float(value)

    @property
    def cliff_score(self) -> Optional[float]:
        return None if self.cliff is None else self.score(self.cliff)

    @property
    def can_rescue(self) -> bool:
        return self.continuous_below_cliff


@dataclass
class Reading:
    """One settled, averaged measurement at one setting."""
    value: Optional[float]             # median dial value; None = dial silent
    score: Optional[float] = None      # DialSpec.score(value)
    n: int = 0
    p10: Optional[float] = None        # in SCORE units (so p10 is always the bad tail)
    p90: Optional[float] = None
    overload: float = math.nan         # 0..1 share of looks with clipped samples
    level_db: float = math.nan         # raw level, dBFS (gain proxy)
    alive: Optional[bool] = None       # None = no liveness source was given
    live_rate: float = math.nan        # liveness units per second
    t: float = 0.0
    note: str = ""

    @property
    def spread(self) -> float:
        if self.p10 is None or self.p90 is None:
            return 0.0
        return self.p90 - self.p10

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["spread"] = self.spread
        return {k: (None if isinstance(v, float) and math.isnan(v) else v)
                for k, v in d.items()}


@runtime_checkable
class Dial(Protocol):
    spec: DialSpec

    def read(self) -> Optional[float]:
        """Latest instantaneous value, non-blocking. None = nothing new/valid."""


@runtime_checkable
class Liveness(Protocol):
    """Proof of decoded CONTENT: TS packets, headers, audio frames, CRC-ok
    messages. A dial without this is a mirage."""

    def count(self) -> int:
        """Monotone counter of decoded-content units."""


@dataclass
class CallableDial:
    spec: DialSpec
    fn: Callable[[], Optional[float]]

    def read(self) -> Optional[float]:
        return self.fn()


@dataclass
class CallableLiveness:
    fn: Callable[[], int]

    def count(self) -> int:
        return int(self.fn())


@dataclass
class LatchDial:
    """A dial that something else pushes values into (a log scraper, a message
    handler). read() hands each pushed value out once."""
    spec: DialSpec
    _pending: list = field(default_factory=list)

    def push(self, value: float) -> None:
        self._pending.append(float(value))

    def read(self) -> Optional[float]:
        if not self._pending:
            return None
        return self._pending.pop(0)


# Ready-made specs for the dials this method grew up on.
ATSC_MER = DialSpec("MER", "dB", cliff=15.2, settle_s=9.0, window_s=12.0)
HD_CNR = DialSpec("CNR", "dB", cliff=None, settle_s=2.0, window_s=4.0)
MPSK_SNR = DialSpec("SNR", "dB", cliff=None, settle_s=0.5, window_s=1.0, poll_s=0.05)
VITERBI_BER = DialSpec("BER", "ratio", higher_is_better=False, transform="log10",
                       settle_s=2.0, window_s=5.0)
CRC_RATE = DialSpec("crc_rate", "msgs/s", cliff=None, continuous_below_cliff=False,
                    settle_s=2.0, window_s=20.0, resolution=0.5)
