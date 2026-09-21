# SPDX-License-Identifier: GPL-3.0-or-later
"""Attaching a decoder that was not written for this library.

  (a) PipeDecoder     - the loop owns the SDR and pipes IQ to the decoder's
                        stdin; the dial is scraped from its output.  PREFERRED.
  (b) your own Dial   - the decoder exposes a runtime control or a stats
                        endpoint: wrap it in CallableDial / FunctionKnob.
  (c) RestartPerCell  - the decoder insists on owning the SDR: restart it for
                        every grid cell. Slow calibration only."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

from .dial import DialSpec
from .knob import FunctionKnob, Knob, KnobSpec, Setting


class LineScraper:
    """Turns a decoder's text output into a dial and a liveness counter."""

    def __init__(self, spec: DialSpec, dial_re: str, live_re: Optional[str] = None,
                 transform: Optional[Callable[[float], float]] = None):
        self.spec = spec
        self._dial_re = re.compile(dial_re)
        self._live_re = re.compile(live_re) if live_re else None
        self._transform = transform
        self._vals: List[float] = []
        self._live = 0
        self._lock = threading.Lock()
        self.tail: List[str] = []

    def feed(self, line: str) -> None:
        m = self._dial_re.search(line)
        with self._lock:
            self.tail = (self.tail + [line.rstrip()])[-40:]
            if m:
                try:
                    v = float(m.group(1))
                    self._vals.append(self._transform(v) if self._transform else v)
                except (ValueError, ZeroDivisionError):
                    pass
            if self._live_re is not None:
                lm = self._live_re.search(line)
                if lm:
                    # a NUMERIC group is a cumulative counter; anything else counts one per match
                    try:
                        self._live = int(float(lm.group(1)))
                    except (IndexError, TypeError, ValueError):
                        self._live += 1

    def watch(self, stream) -> threading.Thread:
        def run():
            for raw in iter(stream.readline, b""):
                self.feed(raw.decode("utf-8", "replace"))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    # Dial
    def read(self) -> Optional[float]:
        with self._lock:
            return self._vals.pop(0) if self._vals else None

    # Liveness
    def count(self) -> int:
        with self._lock:
            return self._live


class RateScraper(LineScraper):
    """A RATE dial: decoded messages per second, from lines that only appear when a
    message passed its CRC (ADS-B, AIS, pagers). It is zero until decoding starts, so
    its DialSpec must say continuous_below_cliff=False. Every counted line is also
    liveness: a CRC-valid message IS decoded content."""

    def __init__(self, spec: DialSpec, line_re: str):
        super().__init__(spec, r"(?!x)x", None)
        self._line_re = re.compile(line_re)
        self._n = 0
        self._t_last: Optional[float] = None
        self._n_last = 0

    def feed(self, line: str) -> None:
        if self._line_re.search(line):
            with self._lock:
                self._n += 1
        else:
            super().feed(line)                         # keep a tail of everything else for health()

    def read(self) -> Optional[float]:
        now = time.monotonic()
        with self._lock:
            n = self._n
        if self._t_last is None or now - self._t_last < 0.5:
            if self._t_last is None:
                self._t_last, self._n_last = now, n
            return None
        rate = (n - self._n_last) / (now - self._t_last)
        self._t_last, self._n_last = now, n
        return rate

    def count(self) -> int:
        with self._lock:
            return self._n


class FileGrowth:
    """Liveness from a decoder's OUTPUT file: decoded audio, a transport stream.
    Bytes that were actually written are content; a status line is not."""

    def __init__(self, path: str, unit: int = 4096):
        self.path, self.unit = path, unit

    def count(self) -> int:
        try:
            return os.path.getsize(self.path) // self.unit
        except OSError:
            return 0


class PatternCounter:
    """Liveness from CONTENT inside a growing output file: counts a byte pattern
    (an MPEG-2 sequence header, a frame sync word). File growth alone is not
    proof - a demodulator can emit full-rate null padding forever."""

    def __init__(self, path: str, pattern: bytes):
        self.path, self.pattern = path, pattern
        self._pos, self._count, self._tail = 0, 0, b""

    def count(self) -> int:
        try:
            size = os.path.getsize(self.path)
            if size < self._pos:                      # the file was recreated
                self._pos, self._tail = 0, b""
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                data = f.read()
        except OSError:
            return self._count
        self._pos += len(data)
        blob = self._tail + data
        self._count += blob.count(self.pattern)
        keep = len(self.pattern) - 1
        self._tail = blob[-keep:] if keep and not blob.endswith(self.pattern) else b""
        return self._count


def graceful_stop(proc: subprocess.Popen, grace_s: float = 15.0) -> bool:
    """Ask first. A decoder that is STREAMING FROM A RADIO must be allowed to
    close the device itself: hard-killing a streaming process can wedge a vendor
    driver service. On Windows that means CTRL_BREAK to a process started in its
    own process group (use popen_group()); elsewhere SIGINT.

    Returns True if the decoder stopped by itself. The default grace is long on
    purpose: a Python decoder whose main thread sleeps 10 s at a time cannot run
    its signal handler until the sleep returns (measured: with an 8 s grace, 16 of
    27 stops timed out and fell through to terminate())."""
    if proc.poll() is not None:
        return True
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        proc.wait(timeout=grace_s)
        return True
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    proc.terminate()                                  # last resort, and it is logged as one
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
    return False


def popen_group(argv, **kw) -> subprocess.Popen:
    """Popen in its own process group, so graceful_stop() can signal it on Windows."""
    if sys.platform == "win32":
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, **kw)


class PipeDecoder:
    """Mode (a). Start the decoder once; hand its stdin to SoapyFrontend.start(sink=...)."""

    def __init__(self, argv: Sequence[str], scraper: LineScraper, env: Optional[dict] = None,
                 scrape: str = "stderr"):
        self.argv, self.scraper, self.env, self.scrape = list(argv), scraper, env, scrape
        self.proc: Optional[subprocess.Popen] = None

    def start(self):
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, env=self.env,
            stdout=subprocess.PIPE if self.scrape == "stdout" else subprocess.DEVNULL,
            stderr=subprocess.PIPE if self.scrape == "stderr" else subprocess.DEVNULL)
        self.scraper.watch(self.proc.stdout if self.scrape == "stdout" else self.proc.stderr)
        return self.proc.stdin

    def health(self) -> Optional[str]:
        """None while the decoder runs; otherwise why it is not running. A dead
        decoder reads exactly like a dead signal, so the loop asks before it
        believes a silent dial."""
        if self.proc is None:
            return "decoder was never started"
        code = self.proc.poll()
        if code is None:
            return None
        tail = " | ".join(self.scraper.tail[-3:])
        return f"decoder exited with code {code}" + (f": {tail}" if tail else "")

    def stop(self, grace_s: float = 6.0) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()              # EOF first: let it finish its own way
        except Exception:
            pass
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class CaptureMeasurer:
    """Mode (a), capture flavour: the loop owns the radio, RECORDS a few seconds
    at each setting, and the decoder's own offline tools judge the file. For
    decoders whose honest quality number exists only offline, and for anything
    that can replay a capture faster than it can be restarted on the air.

      score(path) -> {"value", "p10", "p90", "n"} or None     the dial
      prove(path) -> {"alive": bool, "rate": float, "note": str} or None   content

    Both run concurrently on the same capture. A capture that is not whole
    (samples != wall x fs) is void and is scored as nothing."""

    def __init__(self, frontend, spec: DialSpec, path: str, secs: float,
                 score: Callable[[str], Optional[dict]],
                 prove: Optional[Callable[[str], Optional[dict]]] = None,
                 heartbeat: Optional[Callable[[], None]] = None, poll_s: float = 0.1):
        self.poll_s = poll_s
        self.frontend, self.spec, self.path, self.secs = frontend, spec, path, secs
        self.score, self.prove, self.heartbeat = score, prove, heartbeat
        self.void_captures = 0

    def _beat_while(self, threads) -> None:
        while any(t.is_alive() for t in threads):
            if self.heartbeat:
                self.heartbeat()
            time.sleep(self.poll_s)

    def measure(self, extra_settle_s: float = 0.0, window_scale: float = 1.0):
        from .dial import Reading
        time.sleep(max(self.spec.settle_s, extra_settle_s))
        self.frontend.level()                                   # discard the pre-settle look
        box: dict = {}
        rec = threading.Thread(target=lambda: box.update(
            rec=self.frontend.record(self.path, self.secs * window_scale)))
        rec.start()
        looks = []
        while rec.is_alive():
            look = self.frontend.level()
            if look:
                looks.append(look)
            if self.heartbeat:
                self.heartbeat()
            time.sleep(self.poll_s)
        r = Reading(value=None, t=time.time())
        if looks:
            lv = sorted(l["level_db"] for l in looks)
            r.level_db = float(lv[len(lv) // 2])
            r.overload = sum(1 for l in looks if l.get("clip", 0) > 1e-4) / len(looks)
        if not box.get("rec", {}).get("ok"):
            self.void_captures += 1
            r.note = f"capture void: {box.get('rec')}"
            if self.prove is not None:
                r.alive = False
            return r
        jobs = [threading.Thread(target=lambda: box.update(score=self.score(self.path)))]
        if self.prove is not None:
            jobs.append(threading.Thread(target=lambda: box.update(prove=self.prove(self.path))))
        for j in jobs:
            j.start()
        self._beat_while(jobs)
        sc = box.get("score")
        if sc and sc.get("value") is not None and sc.get("n", 0) >= max(1, self.spec.min_samples):
            r.value = float(sc["value"])
            r.score = self.spec.score(r.value)
            r.n = int(sc.get("n", 0))
            lo, hi = sc.get("p10", r.value), sc.get("p90", r.value)
            a, b = self.spec.score(lo), self.spec.score(hi)
            r.p10, r.p90 = min(a, b), max(a, b)
        if self.prove is not None:
            pv = box.get("prove") or {}
            r.alive = bool(pv.get("alive", False))
            r.live_rate = float(pv.get("rate", 0.0))
            r.note = str(pv.get("note", ""))
        return r


class RestartPerCell:
    """Mode (c). A Frontend whose knobs are decoder command-line / environment
    values; applying a setting restarts the decoder. There is no raw-sample
    view in this mode, so level() is None and the verdict is open-loop."""

    def __init__(self, specs: Sequence[KnobSpec], launch: Callable[[Setting], subprocess.Popen],
                 scraper: LineScraper, scrape: str = "stderr", grace_s: float = 15.0,
                 release_s: float = 1.5):
        self._specs = {s.name: s.with_(cost="restart") for s in specs}
        self._launch, self.scraper, self._scrape, self._grace = launch, scraper, scrape, grace_s
        self._release_s = release_s
        self.hard_kills = 0
        self._state: Setting = {}
        self._dirty = False
        self.proc: Optional[subprocess.Popen] = None

    def knobs(self) -> Dict[str, Knob]:
        # getter=None on purpose: echoing back what we asked for is not a readback, and
        # this mode cannot see the hardware. The verdict says "open-loop".
        return {n: FunctionKnob(s, (lambda v, n=n: self._set(n, v)), None)
                for n, s in self._specs.items()}

    def _set(self, name: str, value) -> None:
        if self._state.get(name) != value:
            self._state[name] = value
            self._dirty = True

    def level(self) -> Optional[dict]:
        if self._dirty:                      # first look after a change = restart point
            self._dirty = False
            self.stop()
            self.proc = self._launch(dict(self._state))
            self.scraper.watch(self.proc.stdout if self._scrape == "stdout" else self.proc.stderr)
        return None

    def stop(self) -> None:
        if self.proc is None:
            return
        if not graceful_stop(self.proc, self._grace):
            self.hard_kills += 1
        self.proc = None
        time.sleep(self._release_s)          # let the device be released before the next open
