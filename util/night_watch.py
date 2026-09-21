#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recalibrate on a schedule, all night, and keep the evidence.

"Every result goes stale; the product is the loop" is a claim. This tests it: the
same targets are re-tuned every N minutes until a stop time, every cell is logged with
its hour (Store.log), and `--report` turns the log into per-target hour curves: how
the best dial, the best SETTING and the verdict moved through the night.

  python night_watch.py --until 06:30 --every 40 --out runs/night \
      -- python examples/hw_atsc3_capture.py --atsc3 ... --rf N --antenna "..." --max-cells 12
  python night_watch.py --report runs/night

Everything after `--` is the tuning command; several targets = several `--target "cmd"`.
Each run must accept `--out DIR`. A run that cannot get the radio (it is shared) simply
fails and is tried again next round: this never waits on, or takes, a busy radio."""
import argparse
import datetime as dt
import glob
import json
import os
import shlex
import subprocess
import sys
import time


def until_ts(hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t.timestamp()


def report(out: str) -> int:
    rounds = sorted(glob.glob(os.path.join(out, "round_*", "*.json")))
    rows = {}
    for f in rounds:
        d = json.load(open(f, encoding="utf-8"))
        v = d["verdict"]
        rows.setdefault(d["target"], []).append(
            (time.strftime("%H:%M", time.localtime(d["when"])), v["shape"], v.get("dial"),
             v.get("best") or v.get("candidate")))
    for target, rs in rows.items():
        dials = [r[2] for r in rs if r[2] is not None]
        picks = {json.dumps(r[3], sort_keys=True) for r in rs if r[3]}
        print(f"\n{target}: {len(rs)} calibrations, dial {min(dials):.1f}..{max(dials):.1f}"
              f" (swing {max(dials) - min(dials):.1f}), {len(picks)} distinct best settings" if dials
              else f"\n{target}: {len(rs)} calibrations, no dial")
        for r in rs:
            print(f"  {r[0]}  {r[1]:<17} {'-' if r[2] is None else format(r[2], '6.2f')}  {r[3]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="06:30")
    ap.add_argument("--every", type=float, default=40.0, help="minutes between rounds")
    ap.add_argument("--out", default="runs/night")
    ap.add_argument("--target", action="append", default=[], help="a tuning command (quoted)")
    ap.add_argument("--targets-file", help="JSON: a list of argv lists. No shell quoting to get wrong "
                                           "(an antenna port called \"Antenna A\" will find a way).")
    ap.add_argument("--report", metavar="DIR")
    a, rest = ap.parse_known_args()
    if a.report:
        return report(a.report)
    # posix=True so that quotes GROUP and are removed ("Antenna A" stays one argument);
    # use forward slashes in paths, since a backslash is an escape here
    targets = [shlex.split(t, posix=True) for t in a.target]
    if a.targets_file:
        targets += json.load(open(a.targets_file, encoding="utf-8"))
    if rest and rest[0] == "--":
        targets.append(rest[1:])
    if not targets:
        ap.error("no tuning command given")
    os.makedirs(a.out, exist_ok=True)
    stop = until_ts(a.until)
    n = 0
    while time.time() < stop:
        n += 1
        t_round = time.time()
        rdir = os.path.join(a.out, f"round_{n:03d}")
        os.makedirs(rdir, exist_ok=True)
        for k, cmd in enumerate(targets):
            if time.time() >= stop:
                break
            log = os.path.join(rdir, f"target_{k}.log")
            with open(log, "w", encoding="utf-8") as lf:
                rc = subprocess.call(cmd + ["--out", rdir], stdout=lf, stderr=subprocess.STDOUT)
            print(f"{time.strftime('%H:%M')} round {n} target {k}: exit {rc}", flush=True)
        time.sleep(max(0.0, min(stop - time.time(), a.every * 60 - (time.time() - t_round))))
    open(os.path.join(a.out, "DONE"), "w").write(time.strftime("%c"))
    return report(a.out)


if __name__ == "__main__":
    sys.exit(main())
