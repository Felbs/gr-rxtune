# SPDX-License-Identifier: GPL-3.0-or-later
"""rxtune command line.

  rxtune selftest                      no radio; numeric PASS/FAIL gates
  rxtune sim <scenario>                run the loop on a simulated receiver
  rxtune discover [--args ...]         list every knob the device reports
  rxtune tune --freq F --rate R --decoder "cmd ..." --dial-re RE [--live-re RE]
                                       mode (a): own the SDR, pipe CS16 IQ to the decoder
  rxtune report <result.json>          re-print a stored verdict"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time

from . import __version__
from .dial import DialSpec


def cmd_selftest(_args) -> int:
    from .loop import tune
    from .shape import Shape
    from .sim import SimReceiver
    from .stages import survey
    ok = True

    def gate(letter, what, cond, detail):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {letter}  {'PASS' if cond else 'FAIL'}  {what:<52} {detail}")

    def run(name, **kw):
        rx = SimReceiver(name)
        return rx, tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"}, **kw)

    print(f"rxtune {__version__} selftest - simulated receiver, no radio")
    rx, r = run("healthy")
    gate("A", "healthy: finds the plateau, recommends, decoding",
         r.verdict.shape is Shape.HEALTHY and r.verdict.ok and r.verdict.dial > 29,
         f"{r.verdict.dial:.1f} dB in {r.verdict.cells} cells")
    rx, r = run("island")
    gate("B", "island: narrow optimum at RF=8 found from a blind grid",
         r.verdict.shape is Shape.ISLAND and r.verdict.best and r.verdict.best["gain:RF"] == 8,
         f"{r.verdict.best} width {r.verdict.evidence.get('good_region_width_db', 0):.0f} dB")
    rx, r = run("island", start={"gain:RF": [2, 3, 4], "gain:IF": [32, 40, 48]})
    gate("C", "staircase: a too-narrow starting span is extended",
         r.result.extended and r.verdict.ok, f"extended={r.result.extended}")
    rx, r = run("aperture")
    gate("D", "aperture-limited: says physical, in dB, recommends nothing",
         r.verdict.shape is Shape.APERTURE_LIMITED and not r.verdict.ok
         and abs(r.verdict.margin_db + 9.0) < 0.8, r.verdict.headline)
    rx, r = run("overload")
    gate("E", "overload: clipped at every gain -> attenuate ahead",
         r.verdict.shape is Shape.OVERLOAD and not r.verdict.ok, r.verdict.rule[:48])
    rx, r = run("plumbing")
    gate("F", "liveness gate: a 30 dB dial with no content is refused",
         r.verdict.shape is Shape.PLUMBING and r.verdict.best is None, r.verdict.headline)
    rx, r = run("fading")
    gate("G", "fading: classified from spread over TIME at one setting",
         r.verdict.shape is Shape.FADING, f"spread {r.verdict.evidence['best_spread']:.1f} dB")
    rx, r = run("impulse")
    gate("H", "impulse: rails at every gain are not overload",
         r.verdict.shape is Shape.IMPULSE, f"baseline {r.verdict.evidence['rails_baseline']:.2f}")
    rx, r = run("gain_limited")
    gate("I", "gain-limited: still rising at maximum gain",
         r.verdict.shape is Shape.GAIN_LIMITED,
         f"slope {r.verdict.evidence.get('slope_at_max_gain', 0):.2f} dB/dB")
    rx, r = run("crc_dial_dead")
    gate("J", "blind dial: a CRC-rate dial refuses to attempt a rescue",
         r.verdict.shape is Shape.BLIND_DIAL, "")
    rx, r = run("stuck_knob")
    gate("K", "a knob that ignores writes is caught twice",
         r.senses.get("gain:IF") == "no-effect" and any("read back" in w for w in r.warnings), "")
    rx, r = run("healthy")
    gate("L", "knob senses learned from the level (both are reductions)",
         r.senses == {"gain:RF": "reduction", "gain:IF": "reduction"}, str(r.senses))
    rx = SimReceiver("fading")
    s = survey(rx, rx, rx, paths={"antenna": ["A", "B"]}, config_knob="config", clock=rx.clock)
    gate("M", "survey: picks the live port; shootout picks by CONTENT",
         s.winner.fixed["antenna"] == "A" and s.config == "erasure",
         f"port {s.winner.fixed['antenna']}, config {s.config} ({s.shootout_metric})")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_sim(args) -> int:
    from .loop import tune
    from .sim import SimReceiver
    rx = SimReceiver(args.scenario, seed=args.seed)
    rep = tune(rx, rx, rx, clock=rx.clock, fixed={"antenna": "A"}, pick=args.pick)
    if args.verbose:
        print("\n".join(rep.result.log))
    print(rep)
    print(f"  ({rx.clock.now():.0f} s of simulated time)")
    return 0


def cmd_discover(args) -> int:
    from . import lock, profile, soapy
    from .knob import describe
    with lock.from_env(owner="rxtune-discover", priority=args.priority):
        fe = soapy.SoapyFrontend(args.args)
        try:
            prof = profile.find(fe.info)
            print(f"device : {fe.info}   profile: {prof.name}")
            print("discovered (blind):")
            print(describe(fe.specs()))
            if prof.name != "blind":
                fe.apply_profile(prof)
                print(f"with profile '{prof.name}':")
                print(describe(fe.specs()))
                for q in prof.quirks:
                    print("  quirk:", " ".join(q.split()))
        finally:
            fe.close()
    return 0


def cmd_tune(args) -> int:
    from . import lock, profile, soapy
    from .attach import LineScraper, PipeDecoder
    from .loop import tune
    from .store import Store, fingerprint
    spec = DialSpec(args.dial_name, args.units, higher_is_better=not args.lower_is_better,
                    cliff=args.cliff, settle_s=args.settle, window_s=args.window,
                    continuous_below_cliff=not args.blind_dial)
    scraper = LineScraper(spec, args.dial_re, args.live_re)
    dec = PipeDecoder(shlex.split(args.decoder, posix=False), scraper, scrape=args.scrape)
    with lock.from_env(owner="rxtune", priority=args.priority) as lk:
        fe = soapy.SoapyFrontend(args.args, rate=args.rate, freq=args.freq, bandwidth=args.bandwidth)
        prof = profile.find(fe.info)
        fe.apply_profile(prof)
        fixed = {"antenna": args.antenna} if args.antenna else {}
        try:
            fe.start(sink=dec.start())
            rep = tune(fe, scraper, scraper if args.live_re else None, lock=lk, fixed=fixed,
                       search=args.search or (prof.search or None), pick=args.pick,
                       coarse_points=args.coarse, max_cells=args.max_cells,
                       known_bad=prof.is_known_bad(), thresholds=prof.thresholds or None,
                       on_cell=lambda s, r: print(f"  {s} -> {r.value} alive={r.alive} "
                                                  f"lvl={r.level_db:.1f} rails={r.overload:.2f}",
                                                  flush=True))
            integ = fe.integrity()
        finally:
            fe.stop()
            dec.stop()
            fe.close()
    print(rep)
    print(f"  stream : ratio {integ['ratio']:.3f} overflows {integ['overflows']} "
          f"dropped {integ['dropped_buffers']}")
    if args.out:
        st = Store(args.out)
        fp = fingerprint(fe.info, fixed, args.label)
        print("saved  :", st.save(fp, f"{args.freq:.0f}", rep.verdict, rep.result.curve(),
                                  {"senses": rep.senses, "warnings": rep.warnings, "stream": integ}))
    return 0 if rep.verdict.ok else 2


def cmd_report(args) -> int:
    doc = json.load(open(args.file, encoding="utf-8"))
    v = doc["verdict"]
    print(f"{time.ctime(doc['when'])}  target {doc['target']}  path {doc['fingerprint']}")
    print(f"VERDICT  {v['shape']}  -  {v['headline']}\n  why    : {v['rule']}\n  use    : {v['best']}"
          f"\n  advice : {v['advice']}")
    for row in doc["curve"]:
        print(f"    {row['phase']:<8}{row['setting']}  {row['value']}  lvl {row['level_db']}  "
              f"rails {row['overload']}  alive {row['alive']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rxtune", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    p = sub.add_parser("sim")
    p.add_argument("scenario")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--pick", default="headroom")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_sim)
    p = sub.add_parser("discover")
    p.add_argument("--args", default="")
    p.add_argument("--priority", type=int, default=60)
    p.set_defaults(fn=cmd_discover)
    p = sub.add_parser("tune")
    p.add_argument("--args", default="")
    p.add_argument("--freq", type=float, required=True)
    p.add_argument("--rate", type=float, required=True)
    p.add_argument("--bandwidth", type=float)
    p.add_argument("--antenna")
    p.add_argument("--decoder", required=True, help="command that reads CS16 IQ on stdin")
    p.add_argument("--scrape", choices=("stderr", "stdout"), default="stderr")
    p.add_argument("--dial-re", required=True, help="regex with one group = the dial value")
    p.add_argument("--live-re", help="regex proving decoded content (group = cumulative count, optional)")
    p.add_argument("--dial-name", default="dial")
    p.add_argument("--units", default="dB")
    p.add_argument("--cliff", type=float)
    p.add_argument("--lower-is-better", action="store_true")
    p.add_argument("--blind-dial", action="store_true", help="dial is zero until decoding (CRC rate)")
    p.add_argument("--settle", type=float, default=2.0)
    p.add_argument("--window", type=float, default=4.0)
    p.add_argument("--search", nargs="*")
    p.add_argument("--pick", default="headroom", choices=("max", "headroom", "knee"))
    p.add_argument("--coarse", type=int, default=5)
    p.add_argument("--max-cells", type=int, default=120)
    p.add_argument("--priority", type=int, default=60)
    p.add_argument("--out", help="directory for the stored result")
    p.add_argument("--label", default="", help="your name for this antenna / RF path")
    p.set_defaults(fn=cmd_tune)
    p = sub.add_parser("report")
    p.add_argument("file")
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
