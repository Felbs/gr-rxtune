# SPDX-License-Identifier: GPL-3.0-or-later
"""Dial adapters for two real decoders, as worked examples of how little an
adapter is: a regular expression, a formula, and an honest DialSpec."""
from __future__ import annotations

import math
import re
from typing import Callable, Dict, Optional, Tuple

from .attach import LineScraper
from .dial import DialSpec

# ---- ATSC 1.0 (8-VSB): equaliser training error -> MER --------------------------
# The field-sync training symbols are +/-5, so the equaliser's RMS error against
# them IS broadcast-grade MER: MER_dB = 20*log10(5 / fs_err_rms). Data cliff 15.2 dB.
ATSC_TRAIN_RMS = 5.0
ATSC_MER = DialSpec("MER", "dB", cliff=15.2, settle_s=9.0, window_s=12.0,
                    # an averaging-depth knob in the equaliser changes what fs_err_rms means
                    comparable_across_configs=False)


def atsc_mer_from_fs_err(fs_err_rms: float) -> Optional[float]:
    if fs_err_rms is None or fs_err_rms <= 0:
        return None
    return 20.0 * math.log10(ATSC_TRAIN_RMS / fs_err_rms)


def atsc_scraper(live_re: Optional[str] = None) -> LineScraper:
    return LineScraper(ATSC_MER, r"fs_err_rms=([\d.]+)", live_re,
                       transform=lambda rms: 20.0 * math.log10(ATSC_TRAIN_RMS / max(rms, 1e-6)))


# ---- NRSC-5 (HD Radio): nrsc5 / albacore log lines -------------------------------
# "MER: 10.5 dB (lower), 11.2 dB (upper)"   "BER: 0.000123, avg: ..., min: ..., max: ..."
NRSC5_MER_RE = r"MER:\s*(-?[\d.]+) dB \(lower\),\s*(-?[\d.]+) dB \(upper\)"
NRSC5_BER_RE = r"BER:\s*([\d.eE+-]+)"
NRSC5_LIVE_RE = r"(Audio bit rate|Title:|Station name:|Slogan:)"
NRSC5_MER = DialSpec("MER", "dB", cliff=None, settle_s=3.0, window_s=6.0, min_samples=3)
NRSC5_BER = DialSpec("BER", "ratio", higher_is_better=False, transform="log10",
                     settle_s=3.0, window_s=8.0, min_samples=3)


class Nrsc5MerScraper(LineScraper):
    """Two sidebands: the dial is the WORSE one, because one good sideband with a
    wrecked twin is an interference diagnosis, not a good signal."""

    def __init__(self, live_re: Optional[str] = NRSC5_LIVE_RE, combine: str = "min"):
        super().__init__(NRSC5_MER, r"(?!x)x", live_re)      # the dial is parsed here, not there
        self._pair = re.compile(NRSC5_MER_RE)
        self._combine = {"min": min, "max": max, "mean": lambda a, b: (a + b) / 2}[combine]

    def feed(self, line: str) -> None:
        m = self._pair.search(line)
        if m:
            with self._lock:
                self._vals.append(self._combine(float(m.group(1)), float(m.group(2))))
        super().feed(line)


def nrsc5_ber_scraper(live_re: Optional[str] = NRSC5_LIVE_RE) -> LineScraper:
    return LineScraper(NRSC5_BER, NRSC5_BER_RE, live_re)


# ---- used by the GNU Radio "Dial Adapter" block -----------------------------------
def _nrsc5_min(m: "re.Match") -> float:
    return min(float(m.group(1)), float(m.group(2)))


MODES: Dict[str, Tuple[Optional[str], Callable, DialSpec]] = {
    # mode: (regex applied to text input or None, number -> dial, spec)
    "identity": (None, lambda x: x, DialSpec("dial")),
    "atsc_fs_err_rms": (r"fs_err_rms=([\d.]+)", atsc_mer_from_fs_err, ATSC_MER),
    "nrsc5_mer": (NRSC5_MER_RE, _nrsc5_min, NRSC5_MER),
    "nrsc5_ber": (NRSC5_BER_RE, lambda x: x, NRSC5_BER),
}


def adapt(mode: str, payload) -> Optional[float]:
    """number or text line -> dial value (None if the line is not telemetry)."""
    regex, fn, _ = MODES[mode]
    if isinstance(payload, (int, float)):
        return fn(float(payload))
    if regex is None:
        try:
            return fn(float(payload))
        except (TypeError, ValueError):
            return None
    m = re.search(regex, str(payload))
    if not m:
        return None
    return fn(m) if mode == "nrsc5_mer" else fn(float(m.group(1)))
