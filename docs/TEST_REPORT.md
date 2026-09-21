# Test report — gr-rxtune 0.1.0

Run 2026-09-20/21 on Windows 11, radioconda (GNU Radio 3.10.12.0, Python 3.12,
PyQt 5.15, SoapySDR 0.8.1, gr-osmosdr 0.2.6, UHD 4.9), plus a plain CPython
3.12 with pytest for the core. Hardware: SDRplay RSPdx via SoapySDRPlay3, a
wideband discone on one port and a UHF TV antenna on another. No station names,
frequencies or locations are recorded here or anywhere in the repository; raw
run logs stay in the git-ignored `runs/`.

How to repeat everything is at the end.

## Summary

| Area | Result |
|---|---|
| Core library, no radio (pytest) | **118 passed**, 0 failed |
| `rxtune selftest` | **13 / 13 gates PASS** |
| GNU Radio block QA (8 files) | **20 passed**, 0 failed |
| Closed loop over stock DSP, headless, no Qt | **PASS** |
| GRC examples compile (`grcc`) | **2 / 2** |
| Qt flowgraph beside stock gr-qtgui widgets | **8 / 8 checks PASS**, offscreen and on a real display |
| Source-block command compatibility | gr-soapy **tested on hardware**; gr-osmosdr tested on the real block (file source); gr-uhd **not testable here** |
| Hardware: FM HD (NRSC-5), attachment mode (a) | **PASS** on the third run — HEALTHY, decoding, agrees with an independent prior calibration (runs 1-2 found 3 defects) |
| Hardware: ATSC 1.0, attachment mode (c) | **PASS with one FAILURE** — HEALTHY, +2.9 dB, decoding; graceful decoder stop failed 16 / 27 times |
| CMake install | **NOT RUN** (no cmake on this machine) |
| Linux / Raspberry Pi | **NOT RUN** |

Thirteen defects were found by this testing and fixed; three defects were found in
*other people's* blocks and are written up in §8.

## 1. Core library (`pytest`, 118 tests, ~4 s)

Runs under a Python that has neither GNU Radio nor SoapySDR; one test blocks
those imports outright and imports every core module, so the "no GNU Radio
dependency" claim is enforced, not asserted.

- **Every verdict, positive control:** 12 simulated scenarios -> the expected shape
  and the expected recommend / refuse decision. Repeated across 5 noise seeds
  (60 runs) to show the classification is not a lucky draw.
- **Negative controls:** a healthy plateau is not an island; impulse rails are not
  overload and overload is not impulse; a rising tail is not a plateau and a
  plateau is not a rising tail; a flat healthy dial is not fading; a shootout
  scored by the dial picks the *flattering* config (which is why it is scored by
  content instead).
- **Optimizer:** finds a 6 dB-wide island from a blind full-span grid; extends a
  too-narrow starting span two different ways (no dial anywhere -> full range;
  monotone climb into an edge -> extend that edge) and does *not* extend when the
  peak is interior; lands within 1 dB of the brute-force optimum; honours the
  cell budget and a profile's known-bad mask; stepwise (generator) and blocking
  APIs give the identical answer; leaves the radio ON the winner.
- **Knobs:** senses learned from the raw level (both simulated knobs are
  reductions, one an index); a knob that accepts writes and ignores them is
  caught by readback *and* by the level; negative attenuator ranges, discrete
  gain tables, swapped and step-less ranges; AGC forced off.
- **Measurement:** settle discards the lying early samples (a 0 s settle reads
  >1.5 dB low in the simulator); minimum-evidence guard; lower-is-better and
  log-scaled (BER) dials.
- **Liveness:** a 30 dB dial with no content is refused; no liveness source ->
  `UNPROVEN`, nothing recommended; content counting finds a header split across
  two reads, survives the file being recreated, and scores a growing file of null
  packets as **zero**.
- **Store:** stale when never calibrated / too old / dial sagged / previous run
  failed; any change to the RF path is a different fingerprint; the hour is logged.
- **Sharing:** a yield request stops the run and the lock is released; a decoder
  is asked to stop (CTRL_BREAK / SIGINT) and is seen to close down itself.
- **Profiles:** the shipped RSPdx profile loads and matches; unknown keys are
  rejected (profiles are data, never code).
- **A dead decoder is never an RF verdict** (`DecoderDied`).

## 2. GNU Radio blocks (`gr_unittest`, 20 tests)

| File | Tests | What it proves |
|---|---|---|
| `qa_controller` | 7 | converges on a 2-knob plant; every command is exactly the dict gr-soapy parses; liveness gate (`PLUMBING`); no liveness wire -> `UNPROVEN`; **a dial that goes silent still ends its cell** (windows close on the wall clock); gr-uhd dialect sends overall gain only and a named element is refused *at construction*; `ctrl` recal runs a second search; status says "open-loop" when it is |
| `qa_loopback` | 2 | §3 |
| `qa_dashboard` | 2 | §4 |
| `qa_compat` | 3 | §5 |
| `qa_msg_setter` | 1 | sets Channel Model and Multiply Const (neither has a message port), reads back, counts unknown knobs |
| `qa_dial_adapter` | 3 | ATSC training error -> MER (number, log line, pair); NRSC-5 lines as a PDU, worse sideband; unknown mode refused |
| `qa_dial_from_tag` | 1 | the stock `mpsk_snr_est_cc` "snr" tag becomes a dial within 1.5 dB of truth |
| `qa_dial_probe` | 1 | bare double, in dB, within 1.5 dB of truth at 10 and 25 dB |

## 3. The loop closes over stock blocks, headless

`examples/headless_loopback.py` / `qa_loopback`: random bits -> Constellation
Modulator -> weak wanted signal + strong out-of-band neighbour -> **Channel Model**
-> gain (the knob) -> rail clip + converter noise -> RRC -> AGC -> **Symbol Sync** ->
**M-PSK SNR probe** -> rxtune Controller -> rxtune Message Setter -> back to the gain.

Measured open-loop curve: 3 dB at -20 dB of gain, rising 1 dB/dB (converter-noise
limited), **28.8 dB at +10 dB**, then a collapse to ~12 dB from +15 dB upward as
the neighbour reaches the rail. Closed loop, repeatedly: **+9 or +10 dB, 29.4-30.4
dB, 19-22 cells, 37-45 s**, overload ridge reported, verdict `HEALTHY` with a
liveness source and `UNPROVEN` without one, `PLUMBING` with a deliberately broken
one. `PyQt5` is asserted absent from `sys.modules` throughout.

*Honest scope:* all DSP is stock. Two non-stock blocks are in the loop: the
controller, and the Message Setter, because Channel Model and Multiply Const
have no message ports (verified in source and by test). The liveness source in
`qa_loopback` is test scaffolding standing in for a decoder's frame counter.

**Two things this test taught us, both now in the docs:**
1. With the SNR estimator at GNU Radio's default `alpha`, the dial remembers the
   previous gain for seconds. The first closed-loop runs "converged" on nonsense.
2. With Symbol Sync at GRC's default max deviation (1.5), the demodulator never
   re-locks after an overload cell: the same setting read 30 dB, then 9 dB.
   **A demodulator with hysteresis poisons every cell measured after the first
   overload.** Bounded to 0.01 in the examples.

## 4. Alongside gr-qtgui

`examples/loopback_qtgui.grc`, compiled by `grcc`, run unattended by
`util/run_grc_qt.py` with a timer in place of a person. In one GRC top window:
**QT GUI Range, QT GUI Time Sink, QT GUI Frequency Sink, QT GUI Number Sink,
Message Debug**, and the rxtune Dashboard placed by GUI Hint.

| Check | offscreen | real display |
|---|---|---|
| dashboard is a child of the GRC top window | PASS | PASS |
| dashboard sits in `top_grid_layout` with the stock widgets (5 widgets) | PASS | PASS |
| Qt event loop never stalled — worst timer gap | 125 ms | 405 ms |
| QT GUI Range callback still reaches its block | PASS | PASS |
| a verdict arrives and is displayed | PASS | PASS |
| heatmap populated (20-21 cells), live dial displayed | PASS | PASS |
| clean `stop()` / `wait()` / quit | PASS | PASS |

Screenshot from the real-display run: `docs/img/loopback_qtgui.png`. `qa_dashboard`
additionally asserts that widget updates execute **on the GUI thread** although
the messages arrive on scheduler threads (signals, as the in-tree Python widgets
do). *Not verified by machine:* text rendering — the offscreen platform has no
fonts on Windows; the real-display screenshot was inspected by eye.

## 5. Source-block command handlers

Read in the GNU Radio maint-3.10 source first (`docs/DESIGN.md` §5), then tested.

**gr-soapy — REAL block, REAL hardware** (`util/hw_soapy_cmd.py`):

| Command posted to `cmd` | Read back from the block | |
|---|---|---|
| `{"gain": {"name": "IFGR", "gain": 45.0}}` | 45.0 (was 50.0) | PASS |
| `{"gain": {"name": "RFGR", "gain": 3.0}}` | 3.0 (was 0.0) | PASS |
| `{"antenna": "<port>"}` | that port | PASS |
| `{"freq": f + 200 kHz}` | f + 200 kHz | PASS |
| still streaming after all of the above | 2.10 of 2.00 MS/s | PASS |
| `{"setting": {"key": "rfnotch_ctrl", "value": "true"}}` | **source block stopped: 0.000 MS/s, next command ignored** | **FAIL (upstream)** |

So: **per-element gain by message works**, which was the question. Device
settings by message do not, on this driver, and fail destructively; the library
now refuses to send them unless forced.

**gr-osmosdr — REAL block** (file source): `message_ports_in()` is empty, although
its GRC definition draws a `command` port. Route = Message Setter. Bonus: the file
source accepts `set_center_freq(101e6)` and stays at 100e6; with `readback` wired,
the controller reports it — the readback rule demonstrated on somebody else's block.

**gr-uhd — NOT TESTED AGAINST THE BLOCK.** `usrp_source` cannot be constructed
without a USRP and UHD has no null device. The dialect is unit-tested against the
command table in gr-uhd's own documentation (overall `gain` as double, `antenna`
as string, `chan`), and a named gain element is refused. Someone with a USRP
should run `qa_controller`'s test 005 pattern against the real block.

**SoapySDR null device:** reports 0 channels, so `soapy.source` cannot be built on
it; hence hardware for gr-soapy.

## 6. Hardware

The radio is shared on this rig. Every run took the site's cooperative lock
through `RXTUNE_LOCK`, heart-beat during measurement, and released it (checked
after each run: free). Nothing was killed; no running daemon was contending.

**Discovery (blind, no profile):** 2 gain elements, 3 antenna ports, 7 settings,
frequency — correct. Measured: gain element `RFGR` and setting `rfgain_sel` are
**the same control** (a write to either reads back through the other), range 0-27.
Knob senses were *learned* as reductions on every run. Settings through raw
SoapySDR read back truthfully and act physically (FM notch: -10.9 -> -52.1 dBFS).

**FM HD / NRSC-5, mode (a) — the loop owns the radio and pipes IQ into an
unmodified `nrsc5`.** 35 cells, 370 s, stream 1.000 of nominal, 0 overflows,
0 dropped buffers.
- Coarse grid: silent + rails 1.00 at the three highest-gain cells (overload),
  ~13 dB across a broad plateau, fading to silence as the level falls to the
  converter floor (-65 dBFS). Exactly the textbook curve.
- **Verdict `HEALTHY`, MER 12.9 dB, decoding; overload ridge at -7 dBFS.**
- Cross-check: the parent project's own, independent calibration for this port
  and station sits on the same plateau.
- Run 1 of this test **failed usefully** - see defects 8 and 9 below. Run 2 produced the
  result above and exposed defects 9 and 10 (six silent cells reported "audio yes"; a
  pick 5 dB under the ridge). **Run 3, after the fixes** (37 cells, 391 s, stream 1.000,
  0 drops): all 10 silent cells read "audio NO"; verdict `HEALTHY`, 12.5 dB, decoding;
  the pick moved to the middle of the plateau at **-20 dBFS, 13 dB clear of the ridge** -
  one LNA state and a few dB of IF reduction away from the setting the parent project's
  independent calibration had stored for this station and port.

**ATSC 1.0 / 8-VSB, mode (c) — decoder restarted per cell.**
27 cells, 673 s (~25 s per cell: every cell is a full chain restart), LNA states 0-12
searched (the FM run had already shown states beyond that to be deaf on this radio).
- Two highest-gain cells silent (overload); a broad plateau of **17.6-18.5 dB with
  7-8.5 sequence headers/s**; 16.1-16.5 dB at LNA state 12; silence beyond.
- **Verdict `HEALTHY`, MER 18.1 dB, +2.9 dB over the 15.2 dB cliff, video decoding**,
  overload ridge reported. In line with this channel's history in the parent project.
- **The liveness law, live:** the single best dial of the whole run - **18.64 dB** -
  belonged to a cell with **no video at all**, and another cell read 18.46 dB median
  with a p10 of 4.2 dB and no video. A dial-only tuner would have chosen the first.
  This run exposed that the pick policies did not yet prefer cells that had decoded
  (defect 12); fixed, regression-tested, and the stored curve **re-judged offline**
  with the fixed code: `headroom` -> one LNA state further from the overload edge,
  18.2 dB, decoding; `knee` -> LNA state 8, 18.05 dB; `max` -> 18.45 dB.
  The channel was not re-run on hardware (see the next point).
- **FAILED: graceful stop.** "decoder hard-killed **16** time(s)" of 27. CTRL_BREAK did
  not stop the TV chain within 8 s in 16 restarts, so the fallback `terminate()` fired
  on a process that was streaming from the radio - the very thing the rig's rules warn
  against. Cause (by inspection, not proven): the chain's main thread sits in a blocking
  `wait()`, where a Python signal handler cannot run. No harm was observed - the radio
  opened and streamed normally afterwards, no stray processes, lock free - and the
  parent project's own tools hard-terminate on every cell, so this is no worse than
  current practice; but it is not what this library promises. The fix belongs in the
  decoder (poll instead of block), and mode (a) removes the problem entirely. It is
  the first item in MIGRATION.md.
- **Unexplained:** three cells with a healthy dial and zero headers had only ~half the
  dial samples (n = 23-30 vs 47): the chain started slowly there and the transport
  stream had probably not begun inside the window. In mode (c) a slow start is
  indistinguishable from a bad setting. One further cell (16.1 dB, full n) had no
  video and I do not know why.
- Mode (c) has no sample view, so there is no level and no rail count: the run is now
  labelled **open-loop** (it was mislabelled closed-loop during the run; defect 13).

## 7. Defects found by this testing, all fixed

1. Pattern search compared against a falsy `0.0` score.
2. A flat plateau peaking on the edge of a starting span was mistaken for a staircase.
3. `STARVED` could never fire (a disconnected input still shows gain-dependent noise).
4. Impulse rails were counted as overload (now judged above the all-gain baseline).
5. PyYAML reads `174.0e6` as a *string*; the known-bad mask silently never matched.
6. `pmt.is_dict()` is true for ANY pair, so `(key . value)` dials and PDUs were
   misparsed (now: a dict is a list whose elements are pairs).
7. PMT dicts do not keep key order: the heatmap's axes could swap mid-run.
8. **A decoder that exited was reported as `NO_SIGNAL`.** Now `DecoderDied`.
9. nrsc5's audio *file* keeps growing with nothing decoded; liveness moved to its
   "Audio bit rate" line (valid packets only).
10. One lucky reading on a flat plateau parked the pick 5 dB under the overload
    ridge; ties are now ties *within the measurement's own noise*.
11. The lazy Qt import returned the module, not the class, on second access.
12. **Pick policies ignored liveness**: the best dial of a run could belong to a cell
    that decoded nothing. A cell that did not decode can no longer win while any
    cell that did decode exists; it stays in the curve as evidence.
13. Restart-per-cell mode echoed its own requests back as "readback" and so called
    itself closed-loop.

Also caught in the test harness itself: a rate probe goes *stale*, not to zero,
when its upstream dies (so it cannot detect a stopped source — count items
instead); the Python block gateway finds message handlers by *name*, so a lambda
handler silently receives nothing.

## 8. Found in other projects (written up, NOT filed — owner's call)

1. **gr-soapy `setting` command.** The handler type-tests the wrong variable (only
   string values survive), applies the setting per *channel*, and does not catch
   exceptions; on a driver with device-level settings the throw ends the source
   block's thread. Reproduced on hardware.
2. **gr-soapy Python `read_setting()`** returned `True` for a boolean setting in
   every state, while `write_setting()` demonstrably worked (49 dB power change).
3. **gr-osmosdr GRC definitions** declare a `command` message input that the
   source block does not register.
4. (Quirk, not a bug) `channels.channel_model.noise_voltage()` returns the value
   set divided by sqrt(2), so a naive setter/getter readback "fails".

## 9. Not tested, and why

- **CMake build / install:** no `cmake` on this machine and nothing was installed
  into the user's radioconda. The `CMakeLists.txt` are gr_modtool's, trimmed for a
  Python-only module; QA ran from the source tree via `examples/_devpath.py`.
- **Linux, Raspberry Pi, any non-Windows platform.** Headlessness was proven by
  asserting Qt is never imported, not by running on a Pi.
- **gr-uhd against the real block** (no USRP). **gr-soapy unknown-gain-name**
  exception path: deliberately not provoked on hardware.
- **Any SDR other than the RSPdx.** Negative-range attenuators, discrete gain
  tables and a lying AGC flag are covered in simulation only.
- **Controller device-handle mode** (`frontend=`): the same `SoapyFrontend` code was
  exercised on hardware through the library runner, not from inside a flowgraph.
- **Stages 1-2 (rate probe, census) on hardware,** the multi-port survey and the
  config shootout on hardware: simulation only. **Bias-T:** never switched.
- **Long runs / recalibration on dial sag / time-of-day curves:** logic unit-tested;
  no overnight run.

## 10. Repeat it

```
python -m pytest                                  # core, any Python with numpy + PyYAML
python -m rxtune selftest
cd python/rxtune && for t in qa_*.py; do python $t; done          # a GNU Radio 3.10 Python
python util/make_grc.py && grcc -o build/grc examples/loopback_*.grc   # GRC_BLOCKS_PATH=<repo>/grc
python util/run_grc_qt.py build/grc/loopback_qtgui.py --png out.png
python util/hw_soapy_cmd.py --gains IFGR=45 RFGR=3 --antenna "<port>" --setting rfnotch_ctrl=true
python examples/hw_nrsc5.py --mhz <station> --antenna "<port>" --nrsc5 <path>
python examples/hw_atsc_stvt.py --tv-live <path> --rf <channel> --antenna "<port>"
```
