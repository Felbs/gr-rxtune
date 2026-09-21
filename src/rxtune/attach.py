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


def graceful_stop(proc: subprocess.Popen, grace_s: float = 8.0) -> None:
    """Ask first. A decoder that is STREAMING FROM A RADIO must be allowed to
    close the device itself: hard-killing a streaming process can wedge a vendor
    driver service. On Windows that means CTRL_BREAK to a process started in its
    own process group (use popen_group()); elsewhere SIGINT."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        proc.wait(timeout=grace_s)
        return
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    proc.terminate()                                  # last resort, and it is logged as one
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()


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


class RestartPerCell:
    """Mode (c). A Frontend whose knobs are decoder command-line / environment
    values; applying a setting restarts the decoder. There is no raw-sample
    view in this mode, so level() is None and the verdict is open-loop."""

    def __init__(self, specs: Sequence[KnobSpec], launch: Callable[[Setting], subprocess.Popen],
                 scraper: LineScraper, scrape: str = "stderr", grace_s: float = 8.0,
                 release_s: float = 1.5):
        self._specs = {s.name: s.with_(cost="restart") for s in specs}
        self._launch, self.scraper, self._scrape, self._grace = launch, scraper, scrape, grace_s
        self._release_s = release_s
        self.hard_kills = 0
        self._state: Setting = {}
        self._dirty = False
        self.proc: Optional[subprocess.Popen] = None

    def knobs(self) -> Dict[str, Knob]:
        return {n: FunctionKnob(s, (lambda v, n=n: self._set(n, v)), (lambda n=n: self._state.get(n)))
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
        t0 = time.monotonic()
        graceful_stop(self.proc, self._grace)
        if time.monotonic() - t0 >= self._grace:
            self.hard_kills += 1
        self.proc = None
        time.sleep(self._release_s)          # let the device be released before the next open
