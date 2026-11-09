#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""rxtune Controller: headless. Dial messages in, command dicts out.

It runs the pure-Python rxtune servo (search -> hold -> probe / re-search) as a
state machine. It never blocks a scheduler thread: dial messages only fill an
accumulator, and a small ticker thread closes windows on wall-clock time, so a
dial that goes SILENT still ends its cell (silence is a measurement)."""
import threading
import time

import pmt
from gnuradio import gr

from ._pmt import is_real_dict, unpack

from rxtune import commands as cmds
from rxtune.dial import DialSpec
from rxtune.knob import KnobSpec
from rxtune.measure import Accumulator
from rxtune.optimize import Axis
from rxtune.servo import Servo, ServoPolicy

IDLE, SEARCHING, HOLDING, PROBING = "idle", "searching", "holding", "probing"


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
           ctrl      {start} | {stop} | {recal} | {retune}
      out: cmd       ONE dict per knob write, in the chosen dialect
           status    {state, cell, setting, value, reason}
           verdict   the Verdict and the measured curve (one per search)

    After the search the servo holds the winner. With `auto_recal` (the servo
    switch) it also probes a neighbour while the margin over the cliff is under
    `comfort`, undoing any step that does not better the truth, re-searches when
    nothing decodes for `unlock_s` or the dial sags `sag` under its settled value,
    and judges the incumbent again beside every new winner. Without it, it only
    watches and reports the triggers in `status`.
    """

    def __init__(self, axes=(), dial_name="dial", units="dB", cliff=None, higher_is_better=True,
                 settle_s=1.0, window_s=2.0, continuous_below_cliff=True, min_samples=5,
                 dialect="soapy", chan=None, autostart=True, pick="headroom", coarse_points=5,
                 max_cells=120, fixed=None, auto_recal=False, sag=3.0, sag_hold_s=10.0,
                 frontend=None, allow_unsafe=False, comfort=3.0, reprobe_s=30.0, unlock_s=10.0):
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
        self.policy = ServoPolicy(autonomous=bool(auto_recal), comfort=float(comfort),
                                  reprobe_s=float(reprobe_s), unlock_s=float(unlock_s),
                                  sag=float(sag), sag_hold_s=float(sag_hold_s))
        self.frontend = frontend           # device-handle mode: write knobs directly, with readback
        self.autostart = bool(autostart)
        self.allow_unsafe = bool(allow_unsafe)
        problems = [] if frontend is not None else cmds.check(
            dialect, [a.spec.name for a in self.axes] + list(self.fixed), self.allow_unsafe)
        if problems:
            raise ValueError("; ".join(problems))

        self._lock = threading.RLock()
        self.state = IDLE
        self.servo = None
        self.acc = None
        self.current = {}
        self.pending = None
        self.verdict = None
        self.warnings = []
        self.have_liveness = False
        self._direct = None
        self._readback_seen = set()
        self._last_live = None
        self._published = 0
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
            self.servo = Servo(self.axes, self.spec, self.policy,
                               tuner_kw=dict(coarse_points=self.coarse_points, max_cells=self.max_cells,
                                             pick=self.pick),
                               closed_loop=self._closed)
            self.verdict, self.warnings, self._published = None, [], 0
            self.state = SEARCHING
            self._status(reason)
            self._advance(self.servo.propose())

    def _closed(self):
        # closed-loop = every searched knob was read back (device handle, or a Message
        # Setter with getters wired to 'readback') and nothing disagreed
        verified = self.frontend is not None or all(
            a.spec.name in self._readback_seen for a in self.axes)
        return verified and not self.warnings

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
                if d.get("cell_done") and self.acc is not None:
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
            if self.acc is not None:
                self.acc.add(value, now)

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
                if self.servo is not None:
                    self.servo.stop()
                self._status("stopped by ctrl")
            return
        reason = next((f"{k} requested" for k in ("recal", "retune", "start") if k in d), None)
        if reason is None:
            return
        with self._lock:
            if self.servo is None or self.state == IDLE:
                self.begin(reason)
                return
            nxt = self.servo.request(reason)
            self._status(reason)
            if nxt is not None:                   # the servo was resting: the search starts now
                self.acc = None
                self._advance(nxt)

    # ---- the state machine ---------------------------------------------------------
    def _tick_loop(self):
        while self._alive.is_set():
            time.sleep(0.05)
            with self._lock:
                if self.state == IDLE or self.acc is None:
                    continue
                now = time.monotonic()
                if self.have_liveness and self._last_live is not None:
                    self.acc.add_liveness(self._last_live, now)   # a counter that stopped is evidence too
                if self.acc.done(now) or self._direct is not None:
                    reading = self.acc.result(now)
                    reading = self._apply_direct(reading)
                    self.acc = None
                    self._status("cell done", reading)
                    self._advance(self.servo.report(reading))

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

    def _advance(self, prop):
        self._publish_verdicts()
        if prop is None:
            self.state = IDLE
            self._status("servo ended")
            return
        setting = dict(prop)
        scale = setting.pop("__window_scale__", 1.0)
        wait = setting.pop("__wait_s__", 0.0)
        self.state = {"searching": SEARCHING, "holding": HOLDING, "probing": PROBING}.get(
            self.servo.state, SEARCHING)
        self.pending = setting
        self._direct = None
        settle = self._write({**self.fixed, **setting})
        self.message_port_pub(pmt.intern("status"), pmt.to_pmt(_plain(
            {"state": self.state, "reason": "cell start" if self.state == SEARCHING else self.servo.last_reason,
             "setting": setting, "settle_s": settle, "wait_s": wait, "window_scale": scale,
             "cell": self.servo.cells + 1, "mode": self._mode()})))
        self.acc = Accumulator(self.spec, time.monotonic(), settle_s=self.spec.settle_s + settle + wait,
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

    def _publish_verdicts(self):
        """One verdict message per finished search, with its curve and log."""
        while self.servo is not None and self._published < len(self.servo.verdicts):
            v = self.servo.verdicts[self._published]
            self._published += 1
            v.notes.extend(w for w in self.warnings if w not in v.notes)
            self.verdict = v
            doc = v.as_dict()
            result = self.servo.tuner.result if self.servo.tuner is not None else None
            doc["curve"] = result.curve() if result else []
            doc["log"] = list(result.log) if result else []
            doc["held"] = self.servo.held
            doc["servo"] = {"counts": dict(self.servo.counts), "time": dict(self.servo.time),
                            "events": [str(e) for e in self.servo.events[-20:]]}
            self.message_port_pub(pmt.intern("verdict"), pmt.to_pmt(_plain(doc)))
            self._status("verdict: " + v.shape.value)

    def _mode(self):
        return ("device-handle" if self.frontend is not None else
                "message + readback" if self._readback_seen else "message (open-loop)")

    def _status(self, reason, reading=None):
        n = self.servo.cells if self.servo is not None else 0
        doc = {"state": self.state, "cell": n, "setting": dict(self.pending or {}), "reason": reason,
               "mode": self._mode()}
        if self.servo is not None:
            doc["held"] = self.servo.held
            doc["servo"] = self.servo.last_reason
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
