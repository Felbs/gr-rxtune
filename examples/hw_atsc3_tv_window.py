#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Television inside a GNU Radio window - with a receiver that is NOT made of GNU
Radio blocks. GNU Radio owns the radio; an external ATSC 3.0 receiver does the decoding.

  Soapy source --+--> QT GUI Frequency Sink
                 +--> QT GUI Waterfall Sink
                 +--> File Sink (growing .cf32)  ==>  receiver --capture FILE --realtime
                                                        --player mpv --wid <pane in this window>

The gain comes from an rxtune verdict (run examples/hw_atsc3_capture.py first and pass
its answer in). The receiver is started a few seconds behind the File Sink and paces
itself to real time, so it never catches the writer. The IQ file grows ~3.3 GB a minute
and is deleted on exit.

  python hw_atsc3_tv_window.py --atsc3 /path/to/receiver --rf <channel> --antenna "<port>" \
      --rfgr 5 --ifgr 46 --secs 120 [--png shot.png]

Set RXTUNE_LOCK to a site lock module if the radio is shared."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from gnuradio import blocks, gr, qtgui, soapy  # noqa: E402
from PyQt5 import Qt, QtCore, QtWidgets  # noqa: E402

try:
    import sip  # noqa: E402
except ImportError:
    from PyQt5 import sip  # noqa: E402

from rxtune import lock, recipes  # noqa: E402
from rxtune.attach import graceful_stop, popen_group  # noqa: E402

RATE = recipes.ATSC3_RATE


class TvWindow(gr.top_block, Qt.QWidget):
    def __init__(self, a, iq_path):
        gr.top_block.__init__(self, "ATSC 3.0 in a GNU Radio window")
        Qt.QWidget.__init__(self)
        self.setWindowTitle("GNU Radio + rxtune: ATSC 3.0")
        grid = Qt.QGridLayout(self)
        self.src = soapy.source(a.args, "fc32", 1, "", "", [""], [""])
        self.src.set_sample_rate(0, RATE)
        self.src.set_frequency(0, recipes.us_tv_centre_hz(a.rf))
        self.src.set_gain_mode(0, False)
        if a.antenna:
            self.src.set_antenna(0, a.antenna)
        self.src.set_gain(0, "RFGR", float(a.rfgr))          # the rxtune verdict
        self.src.set_gain(0, "IFGR", float(a.ifgr))
        self.freq = qtgui.freq_sink_c(2048, 5, 0, RATE, "ATSC 3.0 carrier (baseband)", 1, None)
        self.fall = qtgui.waterfall_sink_c(2048, 5, 0, RATE, "waterfall", 1, None)
        self.sink = blocks.file_sink(gr.sizeof_gr_complex, iq_path, False)
        self.sink.set_unbuffered(False)
        self.connect(self.src, self.freq)
        self.connect(self.src, self.fall)
        self.connect(self.src, self.sink)
        self.pane = QtWidgets.QWidget()
        self.pane.setAttribute(QtCore.Qt.WA_NativeWindow, True)   # mpv needs a real window handle
        self.pane.setStyleSheet("background: black")
        self.pane.setMinimumSize(640, 360)
        self.note = QtWidgets.QLabel(f"gain from rxtune: RFGR {a.rfgr} / IFGR {a.ifgr}   -   "
                                     "decoder: external ATSC 3.0 receiver, fed by this flowgraph")
        grid.addWidget(sip.wrapinstance(self.freq.qwidget(), Qt.QWidget), 0, 0)
        grid.addWidget(sip.wrapinstance(self.fall.qwidget(), Qt.QWidget), 1, 0)
        grid.addWidget(self.pane, 0, 1, 2, 1)
        grid.addWidget(self.note, 2, 0, 1, 2)
        grid.setColumnStretch(1, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--atsc3", required=True)
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--antenna")
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--rfgr", type=int, required=True)
    ap.add_argument("--ifgr", type=int, required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--mpv", default=shutil.which("mpv") or r"C:\Program Files\MPV Player\mpv.exe")
    ap.add_argument("--secs", type=float, default=120.0)
    ap.add_argument("--head-start", type=float, default=6.0, help="seconds of IQ on disk before the receiver starts")
    ap.add_argument("--png", help="screenshot of the window (may show broadcast content: mind where it goes)")
    ap.add_argument("--iq-dir", default=tempfile.gettempdir())
    a = ap.parse_args()

    iq = os.path.join(a.iq_dir, "rxtune_tv_window.cf32")
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(a.mpv) + os.pathsep + env.get("PATH", "")
    state = {"rx": None}
    with lock.from_env(owner="rxtune-tv", priority=60) as lk:
        app = Qt.QApplication(sys.argv[:1])
        tb = TvWindow(a, iq)
        tb.resize(1500, 800)
        tb.start()
        tb.show()
        t0 = time.time()

        def launch():
            wid = int(tb.pane.winId())
            state["rx"] = popen_group(
                [a.python, "-m", "atsc3", "watch", "--capture", iq, "--fmt", "cf32", "--rate", str(RATE),
                 "--realtime", "--player", "mpv",
                 "--player-args", f"--wid={wid} --no-border --no-osc --keepaspect=yes --force-window=yes"],
                cwd=a.atsc3, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def shot():
            if a.png:
                # grab the SCREEN region: a widget grab cannot see mpv's native surface
                g = tb.frameGeometry()
                app.primaryScreen().grabWindow(0, g.x(), g.y(), g.width(), g.height()).save(a.png)

        def tick():
            lk.heartbeat()
            if time.time() - t0 > a.secs:
                timer.stop()
                shot()
                if state["rx"] is not None:
                    graceful_stop(state["rx"], 15.0)
                tb.stop()
                tb.wait()
                app.quit()

        QtCore.QTimer.singleShot(int(a.head_start * 1000), launch)
        timer = QtCore.QTimer()
        timer.timeout.connect(tick)
        timer.start(500)
        app.exec_()
    size = os.path.getsize(iq) if os.path.exists(iq) else 0
    if os.path.exists(iq):
        os.unlink(iq)
    print(f"ran {time.time() - t0:.0f} s; streamed {size / 1e9:.1f} GB of IQ to the receiver; "
          f"receiver exit code {state['rx'].returncode if state['rx'] else None}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
