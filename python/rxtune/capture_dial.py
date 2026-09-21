#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Capture Dial: for decoders whose honest quality number only exists OFFLINE.

Each time the controller starts a cell this block waits for the setting to
settle, records a few seconds of the stream to a file, and hands the file to two
callables - `score(path)` for the dial and `prove(path)` for proof of decoded
content (see rxtune.recipes). It then publishes liveness, and a dial message
marked `cell_done` so the controller closes the cell at once instead of waiting
out its window. A result that belongs to a superseded cell is discarded."""
import os
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

from ._pmt import is_real_dict

IDLE, SETTLE, RECORD, JUDGE = range(4)


class capture_dial(gr.sync_block):
    """
    stream in (complex) ; message in 'status' (from the controller) ;
    message out 'dial' = {value, level_db, clip, cell_done, note}, 'liveness' = counter
    """

    def __init__(self, samp_rate=1e6, secs=5.0, settle_s=1.0, path="rxtune_capture.cs16",
                 score=None, prove=None):
        gr.sync_block.__init__(self, name="rxtune_capture_dial", in_sig=[np.complex64], out_sig=None)
        # absolute: the judges are usually subprocesses with a DIFFERENT working directory
        # (found on hardware: every capture was "never acquired" because nobody could find it)
        self.samp_rate, self.secs, self.settle_s = float(samp_rate), float(secs), float(settle_s)
        self.path = os.path.abspath(path)
        self.cf32 = self.path.lower().endswith(".cf32")     # otherwise interleaved int16
        self.void_captures = 0
        self._t_rec = 0.0
        self.score, self.prove = score, prove
        self.state, self.gen = IDLE, 0
        self._t_go = 0.0
        self._want = 0
        self._file = None
        self._got = 0
        self._sum2, self._clip, self._n = 0.0, 0, 0
        self.live = 0
        self.cells = 0
        self.last_note = ""
        self._lock = threading.Lock()
        self.message_port_register_in(pmt.intern("status"))
        self.set_msg_handler(pmt.intern("status"), self.on_status)
        self.message_port_register_out(pmt.intern("dial"))
        self.message_port_register_out(pmt.intern("liveness"))

    def on_status(self, msg):
        if not is_real_dict(msg):
            return
        d = pmt.to_python(msg)
        if d.get("reason") != "cell start":
            return
        with self._lock:
            self.gen += 1                                  # whatever was in flight is now stale
            self._close()
            self._t_go = time.monotonic() + self.settle_s + float(d.get("settle_s", 0.0))
            self._want = int(self.secs * float(d.get("window_scale", 1.0)) * self.samp_rate)
            self.state = SETTLE

    def _close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        with self._lock:
            if self.state == SETTLE and time.monotonic() >= self._t_go:
                self._file = open(self.path, "wb")
                self._got, self._sum2, self._clip, self._n = 0, 0.0, 0, 0
                self._t_rec = time.monotonic()
                self.state = RECORD
            if self.state == RECORD:
                take = x[:max(0, self._want - self._got)]
                # THE HOT PATH MUST BE CHEAP. A Python block that converts every sample
                # cannot keep up with a 7 MS/s radio: the source overflows, samples are
                # dropped, and the capture is garbage of exactly the right length
                # (found on hardware). cf32 = the stream's own bytes, no conversion; the
                # level look uses one sample in 16.
                if self.cf32:
                    self._file.write(take.tobytes())
                else:
                    iq = np.empty(2 * len(take), np.float32)
                    iq[0::2], iq[1::2] = take.real, take.imag
                    self._file.write(np.clip(np.round(iq * 32767.0), -32768, 32767).astype(np.int16).tobytes())
                thin = take[::16]
                self._sum2 += float(np.sum(thin.real ** 2 + thin.imag ** 2))
                self._clip += int(np.sum((np.abs(thin.real) >= 0.98) | (np.abs(thin.imag) >= 0.98)))
                self._n += len(thin)
                self._got += len(take)
                if self._got >= self._want:
                    self._close()
                    self.state = JUDGE
                    wall = max(1e-6, time.monotonic() - self._t_rec)
                    rms = np.sqrt(self._sum2 / max(1, self._n))
                    look = {"level_db": float(20 * np.log10(max(rms, 1e-6) / np.sqrt(2.0))),
                            "clip": self._clip / max(1, self._n),
                            # samples == wall x fs, or the capture is void
                            "ratio": self._got / (wall * self.samp_rate)}
                    threading.Thread(target=self._judge, args=(self.gen, look), daemon=True).start()
        return n

    def _judge(self, gen, look):
        box = {}
        if look["ratio"] < 0.97:
            # the flowgraph did not keep up: samples were dropped while recording
            self.void_captures += 1
            with self._lock:
                if gen != self.gen:
                    return
                self.state = IDLE
                self.cells += 1
            if self.prove is not None:
                self.message_port_pub(pmt.intern("liveness"), pmt.from_long(self.live))
            self.message_port_pub(pmt.intern("dial"), pmt.to_pmt(
                {"cell_done": True, "level_db": look["level_db"], "clip": look["clip"], "n": 0,
                 "note": f"capture VOID: {100 * look['ratio']:.0f}% of the samples arrived"}))
            return
        jobs = [threading.Thread(target=lambda: box.update(score=self.score(self.path)))]
        if self.prove is not None:
            jobs.append(threading.Thread(target=lambda: box.update(prove=self.prove(self.path))))
        for j in jobs:
            j.start()
        for j in jobs:
            j.join()
        with self._lock:
            if gen != self.gen:
                return                                     # the controller has moved on
            self.state = IDLE
            self.cells += 1
        sc, pv = box.get("score") or {}, box.get("prove") or {}
        if self.prove is not None:
            if pv.get("alive"):
                self.live += max(1, int(pv.get("count", 1)))
            self.message_port_pub(pmt.intern("liveness"), pmt.from_long(self.live))
            time.sleep(0.05)                               # liveness lands before the cell closes
        self.last_note = str(pv.get("note", ""))
        doc = {"cell_done": True, "level_db": look["level_db"], "clip": look["clip"],
               "note": self.last_note, "n": int(sc.get("n", 0))}
        if sc.get("value") is not None:
            doc.update(value=float(sc["value"]), p10=float(sc.get("p10", sc["value"])),
                       p90=float(sc.get("p90", sc["value"])))
        self.message_port_pub(pmt.intern("dial"), pmt.to_pmt(doc))

    def stop(self):
        with self._lock:
            self.gen += 1
            self._close()
        return True
