# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions: semantic.

## [0.1.0] - unreleased

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
