#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE example, attachment mode (b): the decoder owns the SDR but exposes a
runtime control, so the chain runs ONCE and keeps playing while the loop searches
the gain and then the servo holds / probes / re-searches (rxtune.servo).

Decoder: the Software-TV-Tuner ATSC 1.0 (8-VSB) live chain with STVT_PERSIST_RETUNE=1,
which watches <out dir>/retune.cmd for {"rf", "rfgain_sel", "ifgr", ...}, applies it to
the running radio (set_gain / write_setting) and answers <out dir>/retune.ack.
Dial = MER from the equaliser's field-sync training error (fs_err_rms, STVT_EQ_TELEM=1):
MER_dB = 20*log10(5 / fs_err_rms), data cliff 15.2 dB. Liveness = MPEG-2 sequence
headers in the transport stream it writes, read incrementally by a meter that also
counts packets, TEI flags and continuity errors - the numbers an A/B is judged on.

  python hw_atsc_stvt_servo.py --tv-live Z:/src/magic-tv-decoder/tools/tv_live.py --rf 34 \\
      --antenna "Antenna A" --servo 3600 --out-dir C:/captures/stvt_servo/run1
  ... --watch-only          the A/B baseline: same search, then hold with no knob moved

Readback: the ack says the command was applied without an exception and the chain's
own log line names the values; a "write failed" line raises KnobWriteError. There is
no sample view in this mode (no level, no rails): the overload trigger cannot fire.
The chain is stopped with CTRL_BREAK so that IT closes the radio."""
import argparse
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from rxtune import lock  # noqa: E402
from rxtune.adapters import ATSC_MER, atsc_scraper  # noqa: E402
from rxtune.attach import graceful_stop, popen_group  # noqa: E402
from rxtune.knob import FunctionKnob, KnobSpec, KnobWriteError  # noqa: E402
from rxtune.servo import ServoPolicy, serve  # noqa: E402

SEQ_HEADER = b"\x00\x00\x01\xb3"
TS_PKT = 188


class TsMeter:
    """Liveness AND quality from the transport stream the chain writes: sequence
    headers (count() = liveness), packets, TEI flags, continuity errors, null packets.
    Reads only the bytes written since the last call; survives the chain's rotation
    (the file reopened at offset 0 on every retune)."""

    def __init__(self, path: str):
        self.path = path
        self._pos, self._tail = 0, b""
        self.headers = self.packets = self.tei = self.cc_err = self.nulls = 0
        self._cc = {}
        self._lock = threading.Lock()

    def _ingest(self, data: bytes) -> None:
        blob = self._tail + data
        self.headers += blob.count(SEQ_HEADER)
        i = 0
        n = len(blob)
        while i + TS_PKT <= n:
            if blob[i] != 0x47:
                i += 1
                continue
            if i + TS_PKT < n and blob[i + TS_PKT] != 0x47 and i + 2 * TS_PKT <= n:
                i += 1                      # not aligned: a 0x47 inside the payload
                continue
            pkt = blob[i:i + TS_PKT]
            self.packets += 1
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pkt[1] & 0x80:
                self.tei += 1
            if pid == 0x1FFF:
                self.nulls += 1
            elif pkt[3] & 0x10:             # payload present: the counter must step by one
                cc = pkt[3] & 0x0F
                last = self._cc.get(pid)
                if last is not None and cc != (last + 1) & 0x0F and not (pkt[1] & 0x80):
                    self.cc_err += 1
                self._cc[pid] = cc
            i += TS_PKT
        self._tail = blob[max(0, i - 3):] if i < n else b""
        if len(self._tail) > 2 * TS_PKT:
            self._tail = self._tail[-2 * TS_PKT:]

    def count(self) -> int:
        with self._lock:
            try:
                size = os.path.getsize(self.path)
                if size < self._pos:                  # rotated / recreated
                    self._pos, self._tail, self._cc = 0, b"", {}
                with open(self.path, "rb") as f:
                    f.seek(self._pos)
                    data = f.read()
            except OSError:
                return self.headers
            self._pos += len(data)
            if data:
                self._ingest(data)
            return self.headers

    def snapshot(self) -> dict:
        with self._lock:
            return {"packets": self.packets, "tei": self.tei, "cc_errors": self.cc_err,
                    "nulls": self.nulls, "seq_headers": self.headers}


class RetuneFrontend:
    """Mode (b) for STVT: gain knobs written through the running chain's retune.cmd."""

    def __init__(self, out_dir: str, rf: int, scraper, rfgr, ifgr, ack_timeout: float = 30.0):
        self.cmd_path = os.path.join(out_dir, "retune.cmd")
        self.ack_path = os.path.join(out_dir, "retune.ack")
        self.rf, self.scraper, self.ack_timeout = rf, scraper, ack_timeout
        self.state = {"rfgain_sel": None, "ifgr": None}
        self.writes = 0
        self._specs = [
            KnobSpec("gain:RFGR", "range", rfgr[0], rfgr[1], 1, sense="reduction", role="regime",
                     settle_s=10.0, note="rfgain_sel (LNA state) via retune.cmd"),
            KnobSpec("gain:IFGR", "range", ifgr[0], ifgr[1], 1, sense="reduction", role="level",
                     settle_s=10.0, note="IFGR via retune.cmd")]
        self._key = {"gain:RFGR": "rfgain_sel", "gain:IFGR": "ifgr"}

    def knobs(self):
        return {s.name: FunctionKnob(s, (lambda v, n=s.name: self._set(n, v)),
                                     (lambda n=s.name: self.state[self._key[n]]))
                for s in self._specs}

    def level(self):
        return None                                   # no sample view in this mode

    def _set(self, name: str, value) -> None:
        key = self._key[name]
        want = dict(self.state)
        want[key] = int(value)
        cmd = {"rf": self.rf, "rfgain_sel": want["rfgain_sel"], "ifgr": want["ifgr"]}
        cmd = {k: v for k, v in cmd.items() if v is not None}
        t0 = time.time()
        try:
            os.unlink(self.ack_path)
        except OSError:
            pass
        tmp = self.cmd_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(cmd))
        os.replace(tmp, self.cmd_path)
        self.writes += 1
        ack = None
        while time.time() - t0 < self.ack_timeout:
            try:
                with open(self.ack_path, encoding="utf-8") as f:
                    ack = json.loads(f.read())
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        if ack is None:
            raise KnobWriteError(f"{name}: no retune.ack within {self.ack_timeout:.0f} s")
        if not ack.get("ok"):
            raise KnobWriteError(f"{name}: retune failed: {ack.get('error')}")
        time.sleep(0.3)                               # let the chain's log lines land
        bad = [ln for ln in self.scraper.tail[-12:] if "write failed" in ln]
        if bad and key == "rfgain_sel" and any("rfgain_sel" in ln for ln in bad):
            raise KnobWriteError(f"{name}: the chain logged: {bad[-1].strip()}")
        self.state[key] = int(value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv-live", required=True)
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--rfgr", type=int, nargs=2, default=[0, 12], help="rfgain_sel span (RSPdx: 0-27)")
    ap.add_argument("--ifgr", type=int, nargs=2, default=[20, 59])
    ap.add_argument("--start", type=int, nargs=2, default=[4, 40], metavar=("RFGR", "IFGR"),
                    help="the chain starts here (the search then spans the whole range)")
    ap.add_argument("--coarse", type=int, default=4)
    ap.add_argument("--max-cells", type=int, default=30)
    ap.add_argument("--settle", type=float, default=9.0)
    ap.add_argument("--window", type=float, default=12.0)
    ap.add_argument("--servo", type=float, default=3600.0, help="seconds to hold after the search")
    ap.add_argument("--watch-only", action="store_true", help="A/B baseline: hold, report triggers, move nothing")
    ap.add_argument("--comfort", type=float, default=3.0)
    ap.add_argument("--unlock", type=float, default=10.0)
    ap.add_argument("--reprobe", type=float, default=30.0)
    ap.add_argument("--out-dir", required=True, help="live.ts, retune.cmd/ack, chain.log, events.jsonl")
    ap.add_argument("--startup", type=float, default=150.0,
                    help="seconds to wait for the chain's retune watcher (the SDRplay API can hold the device ~60 s after a previous chain)")
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    ts = os.path.join(a.out_dir, "live.ts")
    for stale in ("retune.cmd", "retune.ack", "live.ts"):
        try:
            os.unlink(os.path.join(a.out_dir, stale))
        except OSError:
            pass
    scraper = atsc_scraper()
    scraper.spec = dataclasses.replace(ATSC_MER, settle_s=a.settle, window_s=a.window, min_samples=8)
    meter = TsMeter(ts)
    chain_log = open(os.path.join(a.out_dir, "chain.log"), "ab", buffering=0)

    env = os.environ.copy()
    env.update({"STVT_ANTENNA": a.antenna, "STVT_IFGR": str(a.start[1]), "STVT_RFGAIN_SEL": str(a.start[0]),
                "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
                "STVT_DABNOTCH": "0" if a.rf < 14 else "1", "STVT_RS": "stock", "STVT_SPS": "1.1",
                "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
                "STVT_EQ_TELEM": "1", "STVT_EQ_TELEM_EVERY": "1", "STVT_PERSIST_RETUNE": "1"})
    env.pop("PYTHONPATH", None)
    proc = popen_group([a.python, "-u", a.tv_live, "--rf", str(a.rf), "--out", ts], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            chain_log.write(raw)
            scraper.feed(raw.decode("utf-8", "replace"))
    threading.Thread(target=pump, daemon=True).start()

    fe = RetuneFrontend(a.out_dir, a.rf, scraper, a.rfgr, a.ifgr)
    fe.state = {"rfgain_sel": a.start[0], "ifgr": a.start[1]}
    t0 = time.time()
    armed, seen_mer = False, False
    while time.time() - t0 < a.startup:
        if proc.poll() is not None:
            print("the chain exited during start-up; see chain.log")
            return 3
        armed = any("PERSIST-RETUNE: watcher armed" in ln for ln in scraper.tail)
        seen_mer = seen_mer or scraper.read() is not None      # read() pops: remember it
        if armed and seen_mer:
            break
        time.sleep(0.5)
    print(f"chain up after {time.time() - t0:.0f} s; retune watcher armed: {armed}; "
          f"first MER lines seen: {seen_mer}", flush=True)
    if not armed:
        print("the chain never armed its retune watcher; the knobs cannot be written -- stopping")
        graceful_stop(proc, grace_s=15.0)
        return 3

    events = open(os.path.join(a.out_dir, "events.jsonl"), "a", encoding="utf-8")
    search_writes = {"n": None}

    def on_event(state, setting, reading):
        snap = meter.snapshot()
        row = {"t": time.time(), "state": state, "setting": setting, "mer": reading.value,
               "p10": reading.p10, "p90": reading.p90, "n": reading.n, "alive": reading.alive,
               "hdr_per_s": reading.live_rate, "writes": fe.writes, **snap}
        events.write(json.dumps(row) + "\n")
        events.flush()
        print(f"  [{state:9}] {setting}  MER {None if reading.value is None else round(reading.value, 2)}  "
              f"p10..p90 {None if reading.p10 is None else round(reading.p10, 1)}.."
              f"{None if reading.p90 is None else round(reading.p90, 1)}  n={reading.n}  "
              f"video {'yes' if reading.alive else 'NO'} ({reading.live_rate:.1f} hdr/s)  "
              f"writes={fe.writes} pkts={snap['packets']} tei={snap['tei']} cc_err={snap['cc_errors']}",
              flush=True)
        if state != "searching" and search_writes["n"] is None:
            search_writes["n"] = fe.writes

    policy = ServoPolicy(autonomous=not a.watch_only, comfort=a.comfort, unlock_s=a.unlock, reprobe_s=a.reprobe)
    before = meter.snapshot()
    with lock.from_env(owner="rxtune-atsc-servo", priority=60) as lk:
        try:
            rep = serve(fe, scraper, meter, duration_s=a.servo, policy=policy, lock=lk,
                        search=["gain:RFGR", "gain:IFGR"], coarse_points=a.coarse, max_cells=a.max_cells,
                        health=lambda: ("decoder exited" if proc.poll() is not None else None),
                        on_event=on_event)
        finally:
            closed = graceful_stop(proc, grace_s=15.0)
            chain_log.close()
            events.close()
    after = meter.snapshot()
    print("\n".join(rep.log))
    print(rep)
    hold_writes = fe.writes - (search_writes["n"] or fe.writes)
    print(f"  knob writes: {fe.writes} total, {hold_writes} after the search")
    d = {k: after[k] - before[k] for k in after}
    print(f"  TS over the run: {d['packets']} packets, {d['tei']} TEI, {d['cc_errors']} continuity errors, "
          f"{d['nulls']} nulls, {d['seq_headers']} sequence headers")
    print(f"  {time.time() - t0:.0f} s total; chain closed the radio itself: {closed}")
    with open(os.path.join(a.out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"held": rep.held, "proven": rep.proven, "counts": rep.counts, "time": rep.time,
                   "verdicts": [v.as_dict() for v in rep.verdicts], "knob_writes": fe.writes,
                   "hold_writes": hold_writes, "ts": d, "watch_only": a.watch_only,
                   "events": [str(e) for e in rep.events]}, f, indent=1, default=str)
    return 0 if rep.proven else 2


if __name__ == "__main__":
    sys.exit(main())
