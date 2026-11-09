# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions: semantic.

## [0.1.0] - unreleased

### Changed (2026-11-09, after the first radio runs of the servo example)
- `LineScraper.read()` returns the newest value since the last read and drops the backlog.
  It used to pop one value per poll, so a decoder printing faster than the Measurer polls
  left the dial reading the past (live: 37 lines/s vs ~5 polls/s put the dial 15 minutes
  behind the knob by the end of a 30-cell search, and the verdict picked a cell on another
  cell's number).
- `serve()` counts `duration_s` from the first verdict, not from the first knob write.
- Servo log times are relative to the first reading.
- `examples/hw_atsc_stvt_servo.py`: start-up waits for the chain's retune watcher (default
  150 s), ack timeout 30 s, knob settle 4 s (the decoder's same-channel retune is gain-only),
  `--min-live` liveness floor, equaliser telemetry every field sync.
- `docs/TEST_REPORT.md` §11: the first A/B of the servo on a real radio (steady air: both
  arms identical, servo idle by design).
- `ModuleLock`: `should_yield()` also reports a LOST lock (the site lock's `status()` names
  another holder, or nobody, while we hold) so the run stops instead of streaming without
  its reservation; heartbeats throttled to one per 2 s; `RXTUNE_LOCK_WAIT` sets the wait for
  a busy radio (default 10 s). Found when a warden misread let a waiter take the radio from a
  live run.

First extraction of the adaptive tuning loop into a standalone project.

### Added
- `rxtune` core (no GNU Radio): Dial / Knob / Frontend / Liveness interfaces,
  settle-discard-average measurement, coarse grid -> pattern search with staircase
  extension and distinct-region seeds, pick policies (max / headroom / knee),
  curve-shape classifier (13 shapes), verdict with a liveness gate, stage pipeline,
  fingerprinted stale-aware store, YAML device profiles (SDRplay RSPdx shipped),
  SoapySDR discovery frontend, decoder attachment modes a / b / c, cooperative
  device lock, simulated receiver, CLI with `selftest`.
- `gnuradio.rxtune` blocks (GNU Radio 3.10, Python only): Controller, Dial Probe
  (M-PSK SNR), Dial From Tag, Dial Adapter (ATSC MER, NRSC-5 MER/BER), Message
  Setter, Qt Dashboard; GRC definitions; radio-free loopback examples.
- Capture-per-cell attachment (`attach.CaptureMeasurer`, GNU Radio Capture Dial block),
  `rxtune.recipes` (ATSC 3.0, Mode S), a real-radio GRC example, a TV-in-a-GNU-Radio-window
  example, `util/night_watch.py`, observed decode threshold, re-pick on failed confirmation,
  minimum evidence for counting dials.
- docs: prior art, design, usage, migration plan for the parent projects, test report.
- `rxtune.servo` (2026-11-08): what happens AFTER the search. `Servo` (stepwise, the
  Tuner's API) and `serve()` (blocking): hold while comfortably over the cliff with a
  look every 30 s, probe a neighbour while the margin is small and undo any step that
  does not better the truth (resolution and 1.5 sigma; back-off 6/12/24/45 s), re-search
  only on a trigger (unlocked 10 s, sag 3 dB, request, overload above what the verdict
  accepted) with the incumbent re-measured beside the new winner; an overload descent
  one step per window; counting dials exempt from the sag trigger. Three rules after
  OnAir's auto gain (ideas; PRIOR_ART section 10), the objective stays decoder truth.
  Controller block runs the servo (`auto_recal` = servo on; new `comfort`, `reprobe_s`,
  `unlock_s`, `sag_hold_s`; `ctrl {retune}`; verdict carries `held` and servo counts).
  CLI `sim --servo S`, `tune --servo S`. Simulator: `Scenario.timeline` (the air changes
  while a setting is held), `n_writes`, scenarios `near_cliff`, `dropout`,
  `neighbour_arrives`, `slow_fade`, `traffic_drops`. 13 new tests.
