#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""HARDWARE test: does a REAL gr-soapy source obey the commands rxtune sends?

Builds soapy.source on a real device, posts the controller's own command dicts to
its 'cmd' port, and reads every value back through the block's API.

  python hw_soapy_cmd.py --args driver=sdrplay --freq 100e6 --rate 2e6 \
      --gains IFGR=45 RFGR=3 --antenna "Antenna C" --setting rfnotch_ctrl=true

Set RXTUNE_LOCK to a site lock module if the radio is shared. Deliberately NOT
tested: an unknown gain name (gr-soapy does not catch the exception in its
handler, and killing a process that is streaming can wedge some vendor services)."""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "examples"))
import _devpath  # noqa: E402,F401

import pmt  # noqa: E402
from gnuradio import blocks, gr, soapy  # noqa: E402

from rxtune import commands, lock  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--args", default="driver=sdrplay")
    ap.add_argument("--freq", type=float, default=100e6)
    ap.add_argument("--rate", type=float, default=2e6)
    ap.add_argument("--gains", nargs="*", default=[])
    ap.add_argument("--antenna")
    ap.add_argument("--setting", nargs="*", default=[])
    ap.add_argument("--try-setting-message", action="store_true",
                    help="also post a 'setting' command (stops the source on SoapySDRPlay)")
    a = ap.parse_args()
    ok = True
    with lock.from_env(owner="rxtune-hwtest", priority=60):
        src = soapy.source(a.args, "fc32", 1, "", "", [""], [""])
        src.set_sample_rate(0, a.rate)
        src.set_frequency(0, a.freq)
        src.set_gain_mode(0, False)
        probe = blocks.probe_rate(gr.sizeof_gr_complex, 250.0, 0.5)
        tb = gr.top_block()
        tb.connect(src, probe)
        tb.start()
        time.sleep(1.5)
        post = src.to_basic_block()._post

        def check(what, got, want, tol=0.51):
            nonlocal ok
            try:
                good = abs(float(got) - float(want)) <= tol
            except (TypeError, ValueError):
                good = str(got).lower() == str(want).lower()
            ok &= good
            print(f"  {'PASS' if good else 'FAIL'}  {what}: sent {want!r}, block reads back {got!r}")

        def measured_rate(secs=1.5):
            # item COUNT over wall time: a rate probe goes stale, not to zero, when its
            # upstream block dies, so it cannot be used to detect a stopped source
            n0, t0 = probe.nitems_read(0), time.monotonic()
            time.sleep(secs)
            return (probe.nitems_read(0) - n0) / (time.monotonic() - t0)

        def probe_ok():
            nonlocal ok
            rate = measured_rate()
            good = abs(rate / a.rate - 1.0) < 0.05
            ok &= good
            print(f"  {'PASS' if good else 'FAIL'}  still streaming: {rate / 1e6:.3f} of {a.rate / 1e6:.3f} MS/s")

        print("ports in:", pmt.to_python(src.message_ports_in()))
        for item in a.gains:
            name, val = item.split("=")
            before = src.get_gain(0, name)
            post(pmt.intern("cmd"), pmt.to_pmt(commands.command("soapy", f"gain:{name}", float(val))))
            time.sleep(0.6)
            check(f"per-element gain {name} by MESSAGE (was {before})", src.get_gain(0, name), float(val))
        if a.antenna:
            post(pmt.intern("cmd"), pmt.to_pmt(commands.command("soapy", "antenna", a.antenna)))
            time.sleep(0.8)
            check("antenna by message", src.get_antenna(0), a.antenna)
        post(pmt.intern("cmd"), pmt.to_pmt(commands.command("soapy", "freq", a.freq + 200e3)))
        time.sleep(0.6)
        check("freq by message", src.get_frequency(0), a.freq + 200e3, tol=2e3)
        probe_ok()

        # Device settings: NOT by message (see below) but through the block's API,
        # which is what the rxtune Message Setter calls. Flip, read back, restore.
        for item in a.setting:
            key, val = item.split("=")
            before = src.read_setting(key)
            if str(before).lower() == val.lower():        # make sure the write CHANGES something
                val = "false" if val.lower() == "true" else "true"
            src.write_setting(key, val)
            time.sleep(0.6)
            check(f"setting {key} via write_setting() (was {before!r})", src.read_setting(key), val)
            src.write_setting(key, str(before).lower())
            time.sleep(0.4)
            check(f"setting {key} restored", src.read_setting(key), str(before).lower())
        probe_ok()

        if a.try_setting_message and a.setting:
            # LAST, because on a device-level-settings driver this stops the source block.
            key, val = a.setting[0].split("=")
            print(f"  ....  posting {{'setting': {{key: {key}, value: {val}}}}} to 'cmd' (expected to be fatal)")
            post(pmt.intern("cmd"), pmt.to_pmt(commands.command("soapy", f"setting:{key}", val,
                                                                allow_unsafe=True)))
            time.sleep(1.0)
            r = measured_rate()
            post(pmt.intern("cmd"), pmt.to_pmt(commands.command("soapy", "freq", a.freq)))
            time.sleep(0.8)
            f_now = src.get_frequency(0)
            print(f"  INFO  after the setting message: {r / 1e6:.3f} MS/s "
                  f"({'source block STOPPED streaming' if r < 0.5 * a.rate else 'still streaming'}); "
                  f"a following freq command was {'OBEYED' if abs(f_now - a.freq) < 2e3 else 'IGNORED'}")
        tb.stop()
        tb.wait()
        del src
        time.sleep(1.0)
    print("HW SOAPY CMD", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
