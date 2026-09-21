#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""rxtune Dashboard: live dial, gain-grid heatmap, verdict text.

Optional and separate from the controller (which must run headless). It
follows the pattern of GNU Radio's in-tree Python Qt widgets: it IS a QWidget
and a gr block, GRC docks it through gui_hint, and because message handlers
run on scheduler threads every update crosses to the GUI thread by signal."""
import math

import pmt
from gnuradio import gr
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import pyqtSignal

from ._pmt import is_real_dict, unpack


def _num(x):
    try:
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


class _Heatmap(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 180)
        self.cells = {}          # (x, y) -> value or None (silent)
        self.best = None
        self.now = None
        self.axes = ("", "")
        self.cliff = None

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(24, 24, 28))
        p.setPen(QtGui.QColor(200, 200, 200))
        if not self.cells:
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "waiting for the first cell")
            return
        xs = sorted({k[0] for k in self.cells})
        ys = sorted({k[1] for k in self.cells})
        vals = [v for v in self.cells.values() if v is not None]
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
        m, w, h = 34, self.width(), self.height()
        cw, ch = (w - m - 6) / max(1, len(xs)), (h - m - 6) / max(1, len(ys))
        for (x, y), v in self.cells.items():
            r = QtCore.QRectF(m + xs.index(x) * cw, 4 + (len(ys) - 1 - ys.index(y)) * ch, cw - 1, ch - 1)
            if v is None:
                p.fillRect(r, QtGui.QColor(60, 60, 66))            # silent: no dial
            else:
                t = 0.5 if hi <= lo else (v - lo) / (hi - lo)
                below = self.cliff is not None and v < self.cliff
                col = QtGui.QColor.fromHsvF(0.0 + 0.33 * t, 0.85 if not below else 0.45, 0.35 + 0.6 * t)
                p.fillRect(r, col)
            if (x, y) == self.best:
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 2))
                p.drawRect(r)
            elif (x, y) == self.now:
                p.setPen(QtGui.QPen(QtGui.QColor(255, 220, 0), 1, QtCore.Qt.DashLine))
                p.drawRect(r)
        p.setPen(QtGui.QColor(190, 190, 190))
        f = p.font()
        f.setPointSize(7)
        p.setFont(f)
        for i, x in enumerate(xs):
            if len(xs) <= 12 or i % 2 == 0:
                p.drawText(QtCore.QRectF(m + i * cw, h - m + 2, cw, 12), QtCore.Qt.AlignCenter, f"{x:g}")
        for j, y in enumerate(ys):
            p.drawText(QtCore.QRectF(0, 4 + (len(ys) - 1 - j) * ch, m - 3, ch),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, f"{y:g}")
        p.drawText(QtCore.QRectF(m, h - 14, w - m, 12), QtCore.Qt.AlignCenter, self.axes[0])
        p.save()
        p.translate(9, h / 2)
        p.rotate(-90)
        p.drawText(QtCore.QRectF(-60, -8, 120, 12), QtCore.Qt.AlignCenter, self.axes[1])
        p.restore()


class dashboard(gr.sync_block, QtWidgets.QFrame):
    """message in: dial, status, verdict (all optional). No streams."""

    sig_dial = pyqtSignal(float)
    sig_status = pyqtSignal(object)
    sig_verdict = pyqtSignal(object)

    def __init__(self, label="rxtune", units="dB", cliff=None, dial_min=0.0, dial_max=30.0,
                 parent=None):
        gr.sync_block.__init__(self, name="rxtune_dashboard", in_sig=None, out_sig=None)
        QtWidgets.QFrame.__init__(self, parent)
        self.units = units
        self.cliff = None if cliff in (None, "", "None") else float(cliff)
        self.dial_min, self.dial_max = float(dial_min), float(dial_max)

        self.setFrameStyle(QtWidgets.QFrame.StyledPanel)
        lay = QtWidgets.QGridLayout(self)
        self.title = QtWidgets.QLabel(label)
        self.big = QtWidgets.QLabel("--")
        f = self.big.font()
        f.setPointSize(26)
        f.setBold(True)
        self.big.setFont(f)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.state = QtWidgets.QLabel("idle")
        self.state.setWordWrap(True)
        self.heat = _Heatmap()
        self.heat.cliff = self.cliff
        self.verdict = QtWidgets.QLabel("no verdict yet")
        self.verdict.setWordWrap(True)
        self.verdict.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.title, 0, 0)
        lay.addWidget(self.big, 1, 0)
        lay.addWidget(self.bar, 2, 0)
        lay.addWidget(self.state, 3, 0)
        lay.addWidget(self.heat, 0, 1, 4, 1)
        lay.addWidget(self.verdict, 4, 0, 1, 2)
        lay.setColumnStretch(1, 1)

        self.sig_dial.connect(self._show_dial)
        self.sig_status.connect(self._show_status)
        self.sig_verdict.connect(self._show_verdict)
        for port, fn in (("dial", self._on_dial), ("status", self._on_status),
                         ("verdict", self._on_verdict)):
            self.message_port_register_in(pmt.intern(port))
            self.set_msg_handler(pmt.intern(port), fn)

    # ---- scheduler-thread side: parse, then hop threads by signal ---------------
    def _on_dial(self, msg):
        kind, d = unpack(msg)
        v = _num(d.get("value") if kind == "dict" else d)
        if v is not None:
            self.sig_dial.emit(v)

    def _on_status(self, msg):
        if is_real_dict(msg):
            self.sig_status.emit(pmt.to_python(msg))

    def _on_verdict(self, msg):
        if is_real_dict(msg):
            self.sig_verdict.emit(pmt.to_python(msg))

    # ---- GUI-thread side -----------------------------------------------------------
    def _show_dial(self, v):
        self.big.setText(f"{v:.1f} {self.units}")
        span = max(1e-9, self.dial_max - self.dial_min)
        self.bar.setValue(int(1000 * min(1.0, max(0.0, (v - self.dial_min) / span))))
        good = self.cliff is None or v >= self.cliff
        self.big.setStyleSheet("color: %s" % ("#2ecc71" if good else "#e74c3c"))

    @staticmethod
    def _xy(setting):
        # a PMT dict does not keep insertion order: sort, so the axes never swap mid-run
        nums = [(k, _num(v)) for k, v in sorted(setting.items())]
        nums = [(k, v) for k, v in nums if v is not None]
        if not nums:
            return None, ("", "")
        if len(nums) == 1:
            return (nums[0][1], 0.0), (nums[0][0], "")
        return (nums[0][1], nums[1][1]), (nums[0][0], nums[1][0])

    def _show_status(self, d):
        setting = d.get("setting") or {}
        xy, axes = self._xy(setting) if isinstance(setting, dict) else (None, ("", ""))
        self.state.setText(f"{d.get('state', '')}  cell {d.get('cell', '')}\n{d.get('reason', '')}\n"
                           f"{d.get('mode', '')}")
        if xy is not None:
            self.heat.axes = axes
            self.heat.now = xy
            if "value" in d:
                self.heat.cells[xy] = _num(d.get("value"))
            self.heat.update()

    def _show_verdict(self, d):
        best = d.get("best") if isinstance(d.get("best"), dict) else d.get("candidate")
        if isinstance(best, dict):
            self.heat.best, _ = self._xy(best)
            self.heat.update()
        notes = d.get("notes") or []
        self.verdict.setText(f"<b>{d.get('shape', '')}</b> &mdash; {d.get('headline', '')}<br>"
                             f"<i>{d.get('rule', '')}</i><br>{d.get('advice', '')}"
                             + "".join(f"<br>&bull; {n}" for n in notes if isinstance(n, str)))
