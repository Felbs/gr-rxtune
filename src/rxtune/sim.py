# SPDX-License-Identifier: GPL-3.0-or-later
"""A simulated receiver, so the optimizer and every verdict can be proven with
no radio. It is a link budget, not a waveform simulator: signal, antenna
noise, thermal noise through an LNA whose noise figure depends on its state,
LNA intermodulation from a strong interferer, ADC clipping, ADC quantisation,
a fading term and an impulse term. Its two gain knobs are deliberately
awkward: both are REDUCTIONS and one is an index, as on real hardware."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .dial import DialSpec
from .knob import FunctionKnob, Knob, KnobSpec
from .measure import Clock

LNA_GAIN = [30, 27, 24, 21, 18, 12, 6, 0, -6, -12]     # dB per LNA state 0..9
LNA_NF = [2.0, 2.2, 2.5, 3.0, 4.0, 6.0, 9.0, 13.0, 18.0, 24.0]
THERMAL_DBM = -106.0                                   # kTB in 6 MHz
ADC_FULL_DBM = -10.0                                   # at the ADC input
ADC_SNR_DB = 62.0
CREST_DB = 7.0
LNA_MAX_OUT_DBM = -12.0                                # where the LNA starts to intermodulate
IF_REJECTION_DB = 35.0                                 # out-of-band interferer is filtered before the ADC


CONFIG_LIVE = {"plain": 1.0, "erasure": 1.3, "slow_avg": 0.5}


def _sum_db(*levels: float) -> float:
    return 10.0 * math.log10(sum(10.0 ** (x / 10.0) for x in levels))


class SimClock(Clock):
    """Virtual time: a twelve-second averaging window costs nothing."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)


@dataclass
class Scenario:
    name: str = "healthy"
    signal_dbm: float = -62.0
    ext_noise_dbm: float = -120.0          # antenna / environment noise in band
    interferer_dbm: float = -200.0         # strong out-of-band signal sharing the front end
    fade_db: float = 0.0                   # peak multipath swing
    fade_period_s: float = 7.0
    impulse_prob: float = 0.0              # chance per look of a rail transient
    plumbing_broken: bool = False          # dial fine, no content downstream
    mer_cap: float = 30.0
    antenna_gain_db: Dict[str, float] = field(default_factory=lambda: {"A": 0.0, "B": -40.0})
    ignore_writes_to: Optional[str] = None  # a knob that accepts writes and does nothing
    blind_below: Optional[float] = None    # CRC-style dial: 0 below this SNR
    gain_offset_db: float = 0.0            # loss between the LNA and the converter


SCENARIOS = {
    # strong signal: a wide plateau, an overload ridge at the high-gain end
    "healthy": Scenario("healthy"),
    # weak signal under antenna noise: flat vs gain, below the cliff, no clipping
    "aperture": Scenario("aperture", signal_dbm=-96.0, ext_noise_dbm=-107.0),
    # not enough gain ahead of the converter: quantisation-limited, still
    # rising at maximum gain
    "gain_limited": Scenario("gain_limited", signal_dbm=-85.0, gain_offset_db=-40.0),
    # so hot that even maximum attenuation clips
    "overload": Scenario("overload", signal_dbm=5.0),
    # a hot LNA ahead of the radio: staircase, then a narrow island far out
    "island": Scenario("island", signal_dbm=-70.0, interferer_dbm=-7.0),
    "plumbing": Scenario("plumbing", plumbing_broken=True),
    "fading": Scenario("fading", signal_dbm=-86.0, fade_db=4.0),
    "impulse": Scenario("impulse", impulse_prob=0.45),
    "no_signal": Scenario("no_signal", signal_dbm=-200.0, ext_noise_dbm=-90.0),
    "starved": Scenario("starved", signal_dbm=-200.0,
                        antenna_gain_db={"A": -120.0, "B": -120.0}),
    "stuck_knob": Scenario("stuck_knob", ignore_writes_to="gain:IF"),
    "crc_dial": Scenario("crc_dial", blind_below=15.0),
    "crc_dial_dead": Scenario("crc_dial_dead", signal_dbm=-99.0, blind_below=15.0),
}

SIM_MER = DialSpec("MER", "dB", cliff=15.2, settle_s=2.0, window_s=8.0, poll_s=0.25)
SIM_CRC = DialSpec("crc_rate", "msgs/s", cliff=None, continuous_below_cliff=False,
                   settle_s=2.0, window_s=8.0, poll_s=0.25, resolution=0.5)


class SimReceiver:
    """Frontend + Dial + Liveness in one object, sharing one virtual clock."""

    def __init__(self, scenario: Scenario | str = "healthy", seed: int = 7,
                 clock: Optional[SimClock] = None, senses_known: bool = False):
        self.sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
        self.clock = clock or SimClock()
        self.rng = random.Random(seed)
        self.state: Dict[str, Any] = {"gain:RF": 4, "gain:IF": 40, "antenna": "A", "agc": True,
                                      "config": "plain"}
        self._writes: Dict[str, Any] = dict(self.state)
        self._live = 0.0
        self._t_live = self.clock.now()
        self._t_change = -1e9
        self.spec = SIM_CRC if self.sc.blind_below is not None else SIM_MER
        sense = "reduction" if senses_known else "unknown"
        self._specs = {
            "gain:RF": KnobSpec("gain:RF", "range", 0, 9, 1, sense=sense, role="regime",
                                settle_s=0.2, note="LNA state index"),
            "gain:IF": KnobSpec("gain:IF", "range", 20, 59, 1, sense=sense, role="level",
                                settle_s=0.1, note="IF gain reduction"),
            "antenna": KnobSpec("antenna", "choice", choices=("A", "B"), ordered=False,
                                sense="none", role="path", cost="slow", settle_s=0.5),
            # a recovery-config knob. "slow_avg" FLATTERS the dial (+2 dB) while
            # decoding less: the reason a shootout is judged by content.
            "config": KnobSpec("config", "choice", choices=tuple(CONFIG_LIVE), ordered=False,
                               sense="none", role="config", cost="restart", settle_s=1.0),
        }

    # ---- Frontend -------------------------------------------------------
    def knobs(self) -> Dict[str, Knob]:
        return {n: FunctionKnob(s, (lambda v, n=n: self._set(n, v)), (lambda n=n: self.state[n]))
                for n, s in self._specs.items()}

    def _set(self, name: str, value: Any) -> None:
        self._writes[name] = value
        if name == self.sc.ignore_writes_to:
            return
        self.state[name] = value
        self._t_change = self.clock.now()

    def set_agc(self, on: bool) -> None:
        self.state["agc"] = bool(on)

    def level(self) -> Optional[dict]:
        m = self._model()
        clip = m["clip"]
        if self.sc.impulse_prob and self.rng.random() < self.sc.impulse_prob:
            clip = max(clip, 0.002)
        return {"level_db": m["level_db"] + self.rng.gauss(0, 0.1), "clip": clip}

    # ---- the link budget ------------------------------------------------
    def _model(self) -> dict:
        sc = self.sc
        rf, ifr = int(self.state["gain:RF"]), float(self.state["gain:IF"])
        ant = sc.antenna_gain_db.get(self.state["antenna"], 0.0)
        g_lna, nf = LNA_GAIN[rf], LNA_NF[rf]
        gain = g_lna + (59.0 - ifr) + sc.gain_offset_db
        ps, pi = sc.signal_dbm + ant, sc.interferer_dbm + ant
        n_ext = sc.ext_noise_dbm + ant
        n_th = THERMAL_DBM + nf
        # LNA intermod: grows 3 dB per dB once the stage output nears its limit
        x = pi + g_lna - LNA_MAX_OUT_DBM
        n_imd = (ps - 26.0 + 3.0 * x) if x > -6.0 else -300.0
        total_in = _sum_db(ps, pi - IF_REJECTION_DB, n_ext, n_th)
        p_adc = total_in + gain
        over = p_adc + CREST_DB - ADC_FULL_DBM
        clip = 0.0 if over <= 0 else min(1.0, 0.01 * over ** 1.5)
        n_clip = min(total_in + 6.0, total_in - 22.0 + 2.5 * over) if over > 0 else -300.0
        n_q = ADC_FULL_DBM - ADC_SNR_DB - gain
        n_tot = _sum_db(n_ext, n_th, n_imd, n_clip, n_q)
        snr = ps - n_tot
        level_db = min(-0.5, p_adc - ADC_FULL_DBM) if clip < 1.0 else -0.5
        # a real ADC never reads below its own quantisation floor
        level_db = max(level_db, -ADC_SNR_DB - 8.0)
        return {"snr": snr, "clip": clip, "level_db": level_db}

    # ---- Dial -----------------------------------------------------------
    def read(self) -> Optional[float]:
        self._advance_liveness()
        sc, t = self.sc, self.clock.now()
        m = self._model()
        mer = min(m["snr"], sc.mer_cap)
        if sc.fade_db:
            mer += sc.fade_db * math.sin(2 * math.pi * t / sc.fade_period_s)
        # the decoder's own loops need time after a change: early samples lie
        since = t - self._t_change
        if since < 1.5:
            mer -= 6.0 * (1.0 - since / 1.5)
        mer += self.rng.gauss(0, 0.15)
        if self.state["config"] == "slow_avg":
            mer += 2.0
        if sc.blind_below is not None:
            return 0.0 if mer < sc.blind_below else round(20.0 * (mer - sc.blind_below) + 5.0, 1)
        if mer < 4.0:
            return None                       # no lock, no telemetry
        return mer

    # ---- Liveness -------------------------------------------------------
    def _advance_liveness(self) -> None:
        t = self.clock.now()
        dt, self._t_live = t - self._t_live, t
        if self.sc.plumbing_broken or dt <= 0:
            return
        m = self._model()
        mer = min(m["snr"], self.sc.mer_cap)
        if self.sc.fade_db:
            mer += self.sc.fade_db * math.sin(2 * math.pi * t / self.sc.fade_period_s)
        threshold = self.sc.blind_below if self.sc.blind_below is not None else (SIM_MER.cliff or 0)
        if mer >= threshold:
            self._live += 30.0 * dt * CONFIG_LIVE[self.state["config"]]

    def count(self) -> int:
        self._advance_liveness()
        return int(self._live)
