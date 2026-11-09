# gr-rxtune handoff — 2026-09-21 (branch `main`, published at github.com/Felbs/gr-rxtune)

Standalone receiver tuning loop: a decoder-truth dial drives a search over
runtime-discovered SDR knobs, and the curve's shape becomes an honest verdict.
Extracted in spirit from two existing receiver projects, neither of which was
modified. State: **0.1.0 built and tested** — core, GNU Radio blocks, GRC
examples, hardware runs. Published 2026-09-21 on the owner's instruction.
2026-11-08: `rxtune.servo` added (hold / probe / re-search after the search, three
rules after OnAir's auto gain, objective unchanged); proven on the simulator
(`tests/test_servo.py`, TEST_REPORT §11), **not yet run on hardware**.
**Every push runs the dox gate on the exact commits first. Upstream bug reports
are drafted in docs/UPSTREAM_REPORTS.md and NOT filed; filing is the owner's call.**

## Read first
1. `README.md` — what it is, the method, the blocks, credits; `docs/WALKTHROUGH.md` — follow along in GRC, with screenshots
2. `docs/TEST_REPORT.md` — what passed, what failed, what was NOT tested and why
3. `docs/DESIGN.md` — interfaces, message contract, §5 source-block findings
4. `docs/USAGE.md`, `docs/MIGRATION.md`, `docs/PRIOR_ART.md`, `docs/UPSTREAM_REPORTS.md` (drafts, NOT filed), `BUILD_LOG.md`

## Get going on a fresh machine
```
python -m pytest && python -m rxtune selftest          # any Python with numpy + PyYAML
cd python/rxtune && python qa_controller.py            # a GNU Radio 3.10 Python; no install needed
```
Nothing is compiled: there is no `.so`/`.pyd` to copy or rebuild. From the source
tree, `examples/_devpath.py` makes `from gnuradio import rxtune` resolve here, and
GRC needs `GRC_BLOCKS_PATH=<repo>/grc`.

## Traps, in the order they bit
1. **A stale `GR_PREFIX` / `GRC_BLOCKS_PATH` in the environment** silently points
   `gr_modtool`, `grcc` and GRC at a different GNU Radio. Override both for this
   work (`GR_PREFIX=<radioconda>/Library`). `gr_modtool newmod` needs
   `--srcdir <...>/share/gnuradio/modtool/templates/gr-newmod` when the prefix is wrong.
2. **`settle_s` must cover the dial's own averaging** and the demodulator's
   re-acquisition. SNR estimator `alpha` and Symbol Sync max deviation both
   produced confident nonsense before they were bounded (TEST_REPORT §3).
3. `pmt.is_dict()` is true for any pair; PMT dicts do not keep key order; the
   Python block gateway finds message handlers BY NAME (no lambdas);
   `blocks.probe_rate` goes stale, not to zero, when its upstream dies.
4. PyYAML parses `174.0e6` as a string.
5. Bash heredocs with apostrophes in the body failed to parse in this shell more
   than once; write files with an editor/tool instead.

## Rules for whoever continues
- No upstream bug filing, no release tag, no CGRAN/conda submission without the owner's explicit word.
- Dox gate before EVERY push, on the exact commits.
- No station data, frequencies, locations, call signs or personal info in the tree.
  Run logs go to `runs/` (git-ignored). Scan before any future push.
- `src/rxtune` must import with no GNU Radio and no SoapySDR (a test enforces it).
- Every SDR touch goes through a `DeviceLock`; the rig's lock adapter is
  configuration (`RXTUNE_LOCK`), never committed. Never kill a process that is
  streaming from the radio: `attach.graceful_stop()`.
- Every knob write is read back, or the run is labelled open-loop.
- Do not send gr-soapy `setting` commands to a SoapySDRPlay source: it stops the block.

## Open work, most valuable first
0. **The servo on a real radio** (TEST_REPORT §11): `examples/hw_atsc_stvt_servo.py`
   on the ATSC 1.0 chain (mode (b): STVT's persistent retune is the knob, the TV keeps
   playing; `--watch-only` is the A/B arm), then ATSC 3.0; count knob writes after the
   search, time off the held setting, probes kept, TS continuity errors per arm.
   Status 2026-11-09: first A/B done on steady air (both arms identical, 0 writes after
   the search, servo idle by design). Still open: the same A/B on a marginal channel or
   through evening fading, where the probe and re-search paths fire; whether OnAir's
   thresholds (3 dB comfort, 10 s unlock) suit the decoder's lock time is only answerable
   there. A same-channel retune is gain-only on the decoder's side now (no FPLL cost).
   Status 2026-11-09: two smoke runs on the radio (TEST_REPORT s11, last paragraph) fixed the
   example's timings; the air sat 4 dB under the cliff at every gain, so the A/B itself is
   still owed. Run it with RXTUNE_LOCK set to the site lock module and outside any
   scheduled holder's window; a same-RF retune costs ~8 s plus the chain's re-acquire.
1. **Mode (a) for the ATSC chain** (teach it to take IQ from a pipe/ZMQ): cells
   drop from ~30 s to ~10 s and the overload veto becomes available. See MIGRATION.
2. Run the QA and the headless example on Linux and on a Raspberry Pi; run the
   CMake install somewhere that has cmake.
3. Hardware coverage still owed: rate probe + census, multi-port survey, config
   shootout, bias-T, controller device-handle mode inside a flowgraph, a second
   SDR family (RTL-SDR or Airspy) to exercise blind discovery for real.
4. gr-uhd against a real USRP.
5. Owner's decisions: upstream reports (TEST_REPORT §8), repository creation,
   then conda recipe and CGRAN listing (`MANIFEST.yml` is ready).
6. Migrate radiotuna first, then the TV project (MIGRATION.md).
