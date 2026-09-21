#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""rxtune Controller: headless. Dial messages in, command dicts out.

It runs the pure-Python rxtune search as a state machine. It never blocks a
scheduler thread: dial messages only fill an accumulator, and a small ticker
thread closes windows on wall-clock time, so a dial that goes SILENT still
ends its cell (silence is a measurement)."""
import threading
import time

import pmt
from gnuradio import gr

from ._pmt import is_real_dict, unpack

from rxtune import commands as cmds
from rxtune.dial import DialSpec
from rxtune.knob import KnobSpec
from rxtune.measure import Accumulator
from rxtune.optimize import Axis, Tuner
from rxtune.verdict import judge

IDLE, SEARCHING, HOLDING = "idle", "searching", "holding"


def _axis(d):
    """{"name", "lo","hi","step"} or {"name","choices"} (+ sense, settle_s, start)."""
    d = dict(d)
    start = d.pop("start", None)
    if "choices" in d:
        d["choices"] = tuple(d["choices"])
        d.setdefault("kind", "choice")
    return Axis.of(KnobSpec(**d), start)


class controller(gr.basic_block):
    """
    Message ports
      in : dial      bare number | (key . number) | {value, [alive], [level_db], [clip]}
           liveness  monotone counter of decoded content (number or pair)
           level     {level_db, clip} raw-sample look (optional overload veto)
           readback  {knob, value, readback} from a Message Setter (optional)
           ctrl      {start} | {stop} | {recal}
      out: cmd       ONE dict per knob write, in the chosen dialect
           status    {state, cell, setting, value, reason}
           verdict   the Verdict and the measured curve
    """

    def __init__(self, axes=(), dial_name="dial", units="dB", cliff=None, higher_is_better=True,
                 settle_s=1.0, window_s=2.0, continuous_below_cliff=True, min_samples=5,
                 dialect="soapy", chan=None, autostart=True, pick="headroom", coarse_points=5,
                 max_cells=120, fixed=None, auto_recal=False, sag=1.5, sag_hold_s=10.0,
                 frontend=None, allow_unsafe=False):
        gr.basic_block.__init__(self, name="rxtune_controller", in_sig=None, out_sig=None)
        self.spec = DialSpec(dial_name, units, higher_is_better=bool(higher_is_better),
                             cliff=None if cliff in (None, "", "None") else float(cliff),
                             settle_s=float(settle_s), window_s=float(window_s),
                             continuous_below_cliff=bool(continuous_below_cliff),
                             min_samples=int(min_samples))
        self.axes = [_axis(a) for a in axes]
        self.dialect, self.chan = dialect, chan
        self.pick, self.coarse_points, self.max_cells = pick, int(coarse_points), int(max_cells)
        self.fixed = dict(fixed or {})
        self.auto_recal, self.sag, self.sag_hold_s = bool(auto_recal), float(sag), float(sag_hold_s)
        self.frontend = frontend           # device-handle mode: write knobs directly, with readback
        self.autostart = bool(autostart)
        self.allow_unsafe = bool(allow_unsafe)
        problems = [] if frontend is not None else cmds.check(
            dialect, [a.spec.name for a in self.axes] + list(self.fixed), self.allow_unsafe)
        if problems:
            raise ValueError("; ".join(problems))

        self._lock = threading.RLock()
        self.state = IDLE
        self.tuner = None
        self.acc = None
        self.current = {}
        self.pending = None
        self.verdict = None
        self.warnings = []
        self.have_liveness = False
        self._direct = None
        self._readback_seen = set()
        self._last_live = None
        self._ema = None
        self._sag_since = None
        self._ticker = None
        self._alive = threading.Event()

        for p in ("dial", "liveness", "level", "readback", "ctrl"):
            self.message_port_register_in(pmt.intern(p))
        self.set_msg_handler(pmt.intern("dial"), self._on_dial)
        self.set_msg_handler(pmt.intern("liveness"), self._on_liveness)
        self.set_msg_handler(pmt.intern("level"), self._on_level)
        self.set_msg_handler(pmt.intern("readback"), self._on_readback)
        self.set_msg_handler(pmt.intern("ctrl"), self._on_ctrl)
        for p in ("cmd", "status", "verdict"):
            self.message_port_register_out(pmt.intern(p))

    # ---- lifecycle -----------------------------------------------------------
    def start(self):
        self._alive.set()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._ticker.start()
        if self.autostart:
            self.begin()
        return True

    def stop(self):
        self._alive.clear()
        if self._ticker is not None:
            self._ticker.join(timeout=1.0)
        return True

    def begin(self, reason="start"):
        with self._lock:
            self.tuner = Tuner(self.axes, self.spec, coarse_points=self.coarse_points,
                               max_cells=self.max_cells, pick=self.pick)
            self.verdict, self.warnings = None, []
            self._ema, self._sag_since = None, None
            self.state = SEARCHING
            self._status(reason)
            self._advance(self.tuner.propose())

    # ---- message handlers (scheduler threads) -----------------------------------
    @staticmethod
    def _number(msg):
        return unpack(msg)[1]

    def _on_dial(self, msg):
        now = time.monotonic()
        with self._lock:
            kind, d = unpack(msg)
            if kind == "dict":
                value = d.get("value")
                if self.acc is not None:
                    self.acc.add_level(d, now)
                if d.get("cell_done") and self.state == SEARCHING and self.acc is not None:
                    # an aggregated result for the WHOLE cell (rxtune Capture Dial): close
                    # the cell now; it carries its own n / p10 / p90
                    self._direct = d
                    return
            else:
                value = d
            try:
                value = None if value is None else float(value)
            except (TypeError, ValueError):
                return
            if self.state == SEARCHING and self.acc is not None:
                self.acc.add(value, now)
            elif self.state == HOLDING and value is not None:
                self._watch(value, now)

    def _on_liveness(self, msg):
        with self._lock:
            try:
                self._last_live = int(float(self._number(msg)))
            except (TypeError, ValueError):
                return
            self.have_liveness = True
            if self.acc is not None:
                self.acc.add_liveness(self._last_live, time.monotonic())

    def _on_level(self, msg):
        with self._lock:
            if self.acc is not None and is_real_dict(msg):
                self.acc.add_level(pmt.to_python(msg), time.monotonic())

    def _on_readback(self, msg):
        with self._lock:
            d = pmt.to_python(msg) if is_real_dict(msg) else {}
            if d:
                self._readback_seen.add(d.get("knob"))
            if d and not _same(d.get("readback"), d.get("value")):
                w = f"{d.get('knob')}: wrote {d.get('value')!r}, read back {d.get('readback')!r}"
                if w not in self.warnings:
                    self.warnings.append(w)

    def _on_ctrl(self, msg):
        d = pmt.to_python(msg) if is_real_dict(msg) else {str(self._number(msg)): True}
        if "stop" in d:
            with self._lock:
                self.state, self.acc = IDLE, None
                self._status("stopped by ctrl")
        elif "start" in d or "recal" in d:
            self.begin("recal requested" if "recal" in d else "start requested")

    # ---- the state machine ---------------------------------------------------------
    def _tick_loop(self):
        while self._alive.is_set():
            time.sleep(0.05)
            with self._lock:
                if self.state != SEARCHING or self.acc is None:
                    continue
                now = time.monotonic()
                if self.have_liveness and self._last_live is not None:
                    self.acc.add_liveness(self._last_live, now)   # a counter that stopped is evidence too
                if self.acc.done(now) or self._direct is not None:
                    reading = self.acc.result(now)
                    reading = self._apply_direct(reading)
                    self.acc = None
                    self._status("cell done", reading)
                    self._advance(self.tuner.report(reading))

    def _apply_direct(self, reading):
        d, self._direct = self._direct, None
        if not d:
            return reading
        reading.n = int(d.get("n", 0) or 0)
        reading.note = str(d.get("note", ""))
        if d.get("clip") is not None:
            reading.overload = 1.0 if float(d["clip"]) > 1e-3 else 0.0
        v = d.get("value")
        if isinstance(v, (int, float)) and reading.n >= 1:
            reading.value = float(v)
            reading.score = self.spec.score(reading.value)
            a = self.spec.score(float(d.get("p10", v)))
            b = self.spec.score(float(d.get("p90", v)))
            reading.p10, reading.p90 = min(a, b), max(a, b)
        return reading

    def _advance(self, setting):
        if setting is None:
            return self._finish()
        setting = dict(setting)
        scale = setting.pop("__window_scale__", 1.0)
        self.pending = setting
        self._direct = None
        settle = self._write({**self.fixed, **setting})
        self.message_port_pub(pmt.intern("status"), pmt.to_pmt(_plain(
            {"state": self.state, "reason": "cell start", "setting": setting, "settle_s": settle,
             "window_scale": scale, "cell": len(self.tuner.result.points) + 1,
             "mode": "device-handle" if self.frontend is not None else "message"})))
        self.acc = Accumulator(self.spec, time.monotonic(), settle_s=self.spec.settle_s + settle,
                               window_s=self.spec.window_s * scale)

    def _write(self, setting):
        settle = 0.0
        for a in self.axes:
            if a.spec.name in setting and self.current.get(a.spec.name) != setting[a.spec.name]:
                settle = max(settle, a.spec.settle_s)
        if self.frontend is not None:
            knobs = self.frontend.knobs()
            for name, value in setting.items():
                if self.current.get(name) != value:
                    try:
                        knobs[name].set(value)
                    except Exception as e:                       # KnobWriteError and driver errors
                        if str(e) not in self.warnings:
                            self.warnings.append(str(e))
        else:
            for _, body in cmds.commands(self.dialect, setting, self.current, self.chan,
                                         self.allow_unsafe):
                self.message_port_pub(pmt.intern("cmd"), pmt.to_pmt(body))
        self.current.update(setting)
        return settle

    def _finish(self):
        # closed-loop = every searched knob was read back (device handle, or a Message
        # Setter with getters wired to 'readback') and nothing disagreed
        verified = self.frontend is not None or all(
            a.spec.name in self._readback_seen for a in self.axes)
        closed = verified and not self.warnings
        v = judge(self.tuner.result, self.spec, self.tuner.gain_of, closed_loop=closed)
        v.notes.extend(self.warnings)
        self.verdict = v
        target = v.best or v.candidate
        if target is not None:
            self._write({**self.fixed, **target})     # leave the radio on the best cell found
        self.state = HOLDING
        doc = v.as_dict()
        doc["curve"] = self.tuner.result.curve()
        doc["log"] = self.tuner.result.log
        self.message_port_pub(pmt.intern("verdict"), pmt.to_pmt(_plain(doc)))
        self._status("verdict: " + v.shape.value)

    def _watch(self, value, now):
        """Recalibration trigger: the live dial sags below what was calibrated."""
        score = self.spec.score(value)
        self._ema = score if self._ema is None else self._ema + 0.2 * (score - self._ema)
        ref = self.verdict.evidence.get("best_score") if self.verdict else None
        if ref is None:
            return
        if self._ema < ref - self.sag:
            self._sag_since = self._sag_since or now
            if now - self._sag_since >= self.sag_hold_s:
                self._sag_since = None
                self._status(f"dial sagged {ref - self._ema:.1f} below the calibrated value")
                if self.auto_recal:
                    self.begin("recal: dial sag")
        else:
            self._sag_since = None

    def _status(self, reason, reading=None):
        n = len(self.tuner.result.points) if self.tuner else 0
        doc = {"state": self.state, "cell": n, "setting": dict(self.pending or {}), "reason": reason,
               "mode": ("device-handle" if self.frontend is not None else
                        "message + readback" if self._readback_seen else "message (open-loop)")}
        if reading is not None:
            doc.update({"value": reading.value, "alive": reading.alive, "level_db": reading.level_db,
                        "overload": reading.overload, "n": reading.n})
        self.message_port_pub(pmt.intern("status"), pmt.to_pmt(_plain(doc)))


def _same(got, want):
    try:
        return abs(float(got) - float(want)) <= 1e-3 * max(1.0, abs(float(want)))
    except (TypeError, ValueError):
        return str(got).strip().lower() == str(want).strip().lower()


def _plain(x):
    """pmt.to_pmt cannot take None / NaN-laden floats / enums: make it JSON-plain."""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if x is None:
        return "none"
    if isinstance(x, float) and x != x:
        return "nan"
    if isinstance(x, (bool, int, float, str)):
        return x
    return str(x)
