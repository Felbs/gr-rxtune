# SPDX-License-Identifier: GPL-3.0-or-later
"""Attaching a decoder that was not written for this library.

  (a) PipeDecoder     - the loop owns the SDR and pipes IQ to the decoder's
                        stdin; the dial is scraped from its output.  PREFERRED.
  (b) your own Dial   - the decoder exposes a runtime control or a stats
                        endpoint: wrap it in CallableDial / FunctionKnob.
  (c) RestartPerCell  - the decoder insists on owning the SDR: restart it for
                        every grid cell. Slow calibration only."""
from __future__ import annotations

import re
import subprocess
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
                    # a counter in the line is taken as cumulative; otherwise each match counts one
                    self._live = int(float(lm.group(1))) if lm.groups() else self._live + 1

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
                 scraper: LineScraper, scrape: str = "stderr", grace_s: float = 6.0):
        self._specs = {s.name: s.with_(cost="restart") for s in specs}
        self._launch, self.scraper, self._scrape, self._grace = launch, scraper, scrape, grace_s
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
        self.proc.terminate()
        try:
            self.proc.wait(timeout=self._grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        time.sleep(1.0)                      # let the device be released before the next open
