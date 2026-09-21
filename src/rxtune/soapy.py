# SPDX-License-Identifier: GPL-3.0-or-later
"""SoapyFrontend: the loop owns the radio.

Knobs are DISCOVERED (gain elements, antennas, device settings, frequency,
rate); there is no per-device code here. Rules learned the hard way:
  * never use the overall-gain path - drive elements;
  * every write is read back;
  * hasGainMode() can lie and ArgInfo.value is a default, not state;
  * no blocking I/O in the read loop - the sink has its own thread."""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, BinaryIO, Callable, Dict, List, Optional

import numpy as np

from .knob import FunctionKnob, Knob, KnobSpec


def _soapy():
    import SoapySDR                      # lazy: the core must import without it
    return SoapySDR


def enumerate_devices(args: str = "") -> List[dict]:
    S = _soapy()
    return [dict(d) for d in S.Device.enumerate(args)]


class SoapyFrontend:
    def __init__(self, args: str = "", channel: int = 0, rate: Optional[float] = None,
                 freq: Optional[float] = None, bandwidth: Optional[float] = None,
                 level_every: int = 4, log: Callable[[str], None] = lambda s: None):
        S = _soapy()
        self.S, self.ch, self.log = S, channel, log
        try:
            S.setLogLevel(S.SOAPY_SDR_ERROR)
        except Exception:
            pass
        self.dev = S.Device(args)
        self.RX = S.SOAPY_SDR_RX
        self.info = {"driver": self.dev.getDriverKey(), "hardware": self.dev.getHardwareKey()}
        if rate:
            self.dev.setSampleRate(self.RX, channel, float(rate))
        if bandwidth:
            self.dev.setBandwidth(self.RX, channel, float(bandwidth))
        if freq:
            self.dev.setFrequency(self.RX, channel, float(freq))
        self._specs: Dict[str, KnobSpec] = {}
        self._knobs: Dict[str, Knob] = {}
        self.discover()
        # streaming state
        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._run = threading.Event()
        self._look: Optional[dict] = None
        self._look_lock = threading.Lock()
        self._level_every = max(1, level_every)
        self._sinkq: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self._sink: Optional[BinaryIO] = None
        self.samples = 0
        self.dropped_buffers = 0
        self.overflows = 0
        self.t_start = 0.0

    # ---- discovery --------------------------------------------------------
    def discover(self) -> Dict[str, KnobSpec]:
        d, RX, ch = self.dev, self.RX, self.ch
        specs: Dict[str, KnobSpec] = {}
        for name in d.listGains(RX, ch):                       # "in order RF to baseband"
            r = d.getGainRange(RX, ch, name)
            lo, hi, step = r.minimum(), r.maximum(), r.step()
            specs[f"gain:{name}"] = KnobSpec(f"gain:{name}", "range", lo, hi, step or 1.0,
                                             sense="unknown", settle_s=0.15)
        ants = list(d.listAntennas(RX, ch))
        if len(ants) > 1:
            specs["antenna"] = KnobSpec("antenna", "choice", choices=tuple(ants), ordered=False,
                                        sense="none", role="path", cost="slow", settle_s=0.5)
        for info in d.getSettingInfo():
            key = info.key
            opts = tuple(info.options)
            if info.type == self.S.ArgInfo.BOOL:
                spec = KnobSpec(f"setting:{key}", "choice", choices=("false", "true"),
                                ordered=False, sense="none", role="path", cost="slow",
                                settle_s=0.5, note=info.name or "")
            elif opts:
                spec = KnobSpec(f"setting:{key}", "choice", choices=opts, ordered=True,
                                sense="unknown", cost="slow", settle_s=0.3, note=info.name or "")
            elif info.type in (self.S.ArgInfo.INT, self.S.ArgInfo.FLOAT) \
                    and info.range.maximum() > info.range.minimum():
                spec = KnobSpec(f"setting:{key}", "range", info.range.minimum(),
                                info.range.maximum(), info.range.step() or 1.0, sense="none",
                                cost="slow", settle_s=0.3, note=info.name or "")
            else:
                continue
            specs[spec.name] = spec
        fr = d.getFrequencyRange(RX, ch)
        if fr:
            specs["freq"] = KnobSpec("freq", "range", fr[0].minimum(), fr[-1].maximum(), None,
                                     sense="none", role="path", cost="slow", settle_s=0.3)
        self._specs = specs
        self._build_knobs()
        return specs

    def apply_profile(self, profile) -> None:
        """Overlay per-device DATA (senses, settle times, roles) on what was
        discovered. Never adds a knob the device did not report."""
        self._specs = profile.overlay(self._specs)
        self._build_knobs()

    def _build_knobs(self) -> None:
        d, RX, ch = self.dev, self.RX, self.ch
        knobs: Dict[str, Knob] = {}
        for name, spec in self._specs.items():
            kind, _, key = name.partition(":")
            if kind == "gain":
                knobs[name] = FunctionKnob(spec, (lambda v, k=key: d.setGain(RX, ch, k, float(v))),
                                           (lambda k=key: d.getGain(RX, ch, k)))
            elif kind == "antenna":
                knobs[name] = FunctionKnob(spec, lambda v: d.setAntenna(RX, ch, str(v)),
                                           lambda: d.getAntenna(RX, ch))
            elif kind == "setting":
                knobs[name] = FunctionKnob(spec, (lambda v, k=key: d.writeSetting(k, str(v))),
                                           (lambda k=key: d.readSetting(k)))
            elif kind == "freq":
                knobs[name] = FunctionKnob(spec, lambda v: d.setFrequency(RX, ch, float(v)),
                                           lambda: d.getFrequency(RX, ch), tolerance=5e3)
        self._knobs = knobs

    def knobs(self) -> Dict[str, Knob]:
        return self._knobs

    def specs(self) -> List[KnobSpec]:
        return list(self._specs.values())

    # ---- AGC ---------------------------------------------------------------
    def set_agc(self, on: bool) -> None:
        if self.dev.hasGainMode(self.RX, self.ch):
            self.dev.setGainMode(self.RX, self.ch, bool(on))

    def verify_agc_off(self) -> bool:
        """The flag only. Whether gain writes really take effect is settled by
        the sense-learning step, which watches the raw level."""
        try:
            return not self.dev.getGainMode(self.RX, self.ch)
        except Exception:
            return True

    # ---- streaming: pump thread -> level looks (+ optional sink) ------------
    def start(self, sink: Optional[BinaryIO] = None, fmt: str = "CS16", mtu: int = 65536) -> None:
        """Begin streaming. `sink` receives raw interleaved samples (attachment
        mode a: the loop owns the SDR and feeds the decoder)."""
        S = self.S
        self._fmt = fmt
        self._sink = sink
        self._stream = self.dev.setupStream(self.RX, fmt, [self.ch])
        self.dev.activateStream(self._stream)
        self._run.set()
        self.samples, self.t_start = 0, time.monotonic()
        self._thread = threading.Thread(target=self._pump, args=(mtu,), daemon=True)
        self._thread.start()
        if sink is not None:
            threading.Thread(target=self._drain, daemon=True).start()

    def _pump(self, mtu: int) -> None:
        S = self.S
        cs16 = self._fmt == "CS16"
        buf = np.empty(2 * mtu, np.int16) if cs16 else np.empty(mtu, np.complex64)
        full = 32767.0 if cs16 else 1.0
        n_buf = 0
        while self._run.is_set():
            sr = self.dev.readStream(self._stream, [buf], mtu, timeoutUs=200000)
            if sr.ret == S.SOAPY_SDR_OVERFLOW:
                self.overflows += 1
                continue
            if sr.ret <= 0:
                continue
            n = sr.ret
            self.samples += n
            n_buf += 1
            if self._sink is not None:
                chunk = buf[:2 * n].tobytes() if cs16 else buf[:n].tobytes()
                try:
                    self._sinkq.put_nowait(chunk)
                except queue.Full:
                    self.dropped_buffers += 1      # never block the read loop
            if n_buf % self._level_every:
                continue                           # duty-cycled: the look is cheap, not free
            if cs16:
                x = buf[:2 * n].astype(np.float32)
                rms = float(np.sqrt(np.mean(x * x)) * np.sqrt(2.0))   # |I+jQ| rms
                clip = float(np.mean(np.abs(x) >= 0.98 * full))
            else:
                x = buf[:n]
                rms = float(np.sqrt(np.mean(x.real ** 2 + x.imag ** 2)))
                clip = float(np.mean((np.abs(x.real) >= 0.98) | (np.abs(x.imag) >= 0.98)))
            with self._look_lock:
                self._look = {"level_db": 20.0 * np.log10(max(rms, 1e-3) / (full * np.sqrt(2.0))),
                              "clip": clip}

    def _drain(self) -> None:
        while self._run.is_set() or not self._sinkq.empty():
            try:
                chunk = self._sinkq.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._sink.write(chunk)
            except (BrokenPipeError, OSError, ValueError):
                self._sink = None
                return

    def level(self) -> Optional[dict]:
        with self._look_lock:
            look, self._look = self._look, None     # each look is handed out once
        return look

    def integrity(self) -> dict:
        """samples == wall x fs, or the capture is void."""
        wall = max(1e-6, time.monotonic() - self.t_start)
        fs = self.dev.getSampleRate(self.RX, self.ch)
        return {"ratio": self.samples / (wall * fs), "overflows": self.overflows,
                "dropped_buffers": self.dropped_buffers, "fs": fs, "wall_s": wall}

    def stop(self) -> None:
        self._run.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._stream is not None:
            try:
                self.dev.deactivateStream(self._stream)
                self.dev.closeStream(self._stream)
            finally:
                self._stream = None

    def close(self) -> None:
        self.stop()
        self.dev = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- stage helpers ----------------------------------------------------------
    def probe_rate(self, rate: float, secs: float = 1.5) -> dict:
        """Can the host actually keep up at this rate? (USB / CPU probe.)"""
        self.dev.setSampleRate(self.RX, self.ch, float(rate))
        self.start()
        time.sleep(0.3)
        self.samples, self.t_start, self.overflows = 0, time.monotonic(), 0
        time.sleep(secs)
        out = self.integrity()
        self.stop()
        out["ok"] = abs(out["ratio"] - 1.0) < 0.03 and out["overflows"] == 0
        return out

    def snapshot(self, n: int = 1 << 17) -> np.ndarray:
        """One contiguous complex capture (stream must NOT be running)."""
        st = self.dev.setupStream(self.RX, "CF32", [self.ch])
        self.dev.activateStream(st)
        out = np.empty(n, np.complex64)
        got, tries = 0, 0
        junk = np.empty(65536, np.complex64)
        for _ in range(4):                                     # let the front end settle
            self.dev.readStream(st, [junk], len(junk), timeoutUs=300000)
        while got < n and tries < 400:
            sr = self.dev.readStream(st, [out[got:]], n - got, timeoutUs=300000)
            tries += 1
            if sr.ret > 0:
                got += sr.ret
        self.dev.deactivateStream(st)
        self.dev.closeStream(st)
        return out[:got]

    def census(self, nfft: int = 4096) -> dict:
        """Interferer census: what else is in the passband, relative to the floor."""
        x = self.snapshot(nfft * 32)
        if len(x) < nfft:
            return {"ok": False}
        seg = x[:len(x) // nfft * nfft].reshape(-1, nfft) * np.hanning(nfft)
        psd = 10 * np.log10(np.mean(np.abs(np.fft.fftshift(np.fft.fft(seg), axes=1)) ** 2, axis=0) + 1e-12)
        floor = float(np.median(psd))
        fs = self.dev.getSampleRate(self.RX, self.ch)
        f0 = self.dev.getFrequency(self.RX, self.ch)
        idx = np.argsort(psd)[::-1][:8]
        peaks = [{"hz": float(f0 + (i - nfft // 2) * fs / nfft), "db_over_floor": float(psd[i] - floor)}
                 for i in sorted(idx)]
        return {"ok": True, "floor_db": floor, "peak_over_floor_db": float(psd.max() - floor),
                "peaks": peaks}
