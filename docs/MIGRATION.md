# Migrating the two parent projects onto rxtune

Nothing in either project was changed by the gr-rxtune work. This is the plan
for when they become consumers. Both can do it file by file; nothing here needs
a flag day.

## What each project has today (surveyed read-only, 2026-09-20)

**Software-TV-Tuner, `adaptive-tv/`** — `mer_meter`, `mer_gain_cal`,
`config_shootout`, `tune_antenna`, `overnight_cube`, `deep_tune`, `live_tune`.
The dial formula, the cliff constant, the telemetry regexes and a ~15-variable
base environment block are repeated verbatim in five files. Every tool drives
the radio *indirectly*: environment variables, a chain subprocess, a log file,
regular expressions, then a hard `terminate()`.

**radiotuna / albacore `lab/`** — `hd_ant_autotune` (grid + knee pick),
`hd_ladder`, `hd_night_cube`, `hd_day_lab2`, `hf_knob` (the hour as a knob).
These open SoapySDR in-process and write `rfgain_sel` with no readback.

## Mapping

| Today | rxtune |
|---|---|
| `MER_dB = 20*log10(5/fs_err_rms)`, `CLIFF_DB = 15.2`, `RE_FS` (x5) | `rxtune.adapters.ATSC_MER`, `atsc_scraper()` |
| nrsc5 `MER:` / `BER:` parsing | `Nrsc5MerScraper`, `nrsc5_ber_scraper()` |
| "skip the first third", `SETTLE_SECS`, per-tool sleeps | `DialSpec.settle_s / window_s`, `measure.Accumulator` |
| `GRID`, `COARSE`, `DEEP`, the refine cross | `optimize.Tuner` (full-span coarse grid, staircase extension, pattern search) |
| `results.sort(reverse=True)` / 0.3 dB rail tie-break / knee-within-1-dB | pick policies `max` / `headroom` / `knee` |
| `railing > 3 -> OVERLOAD`, CLEAN / IMPULSE / BELOW-CLIFF / PHANTOM, the disease taxonomy | `shape.classify` -> `Verdict` |
| header counting, `quality_judge`, `hd_audio_meter` | `Liveness` (`PatternCounter`, `FileGrowth`, or a `CallableLiveness` around the judge) |
| `profiles/<antenna>.json`, `hd_ant_cal.json`, `cube_log.jsonl` | `store.Store` (fingerprinted, stale-aware) + `measurements.jsonl` / `hour_curve()` |
| direct `SoapySDR.Device` + `writeSetting("rfgain_sel")` in a bare `try` | `soapy.SoapyFrontend` (discovery, readback, AGC off) |
| per-tool lock handling, or none | `lock.ModuleLock` via `RXTUNE_LOCK` |

## Order of work

1. **radiotuna first** (smallest, and already in-process). Replace the body of
   `hd_ant_autotune.measure()`/grid loop with `SoapyFrontend` + `tune(pick="knee")`.
   Keep writing `hd_ant_cal.json` from the `Verdict` so `hd_radio.open_sdr` is
   untouched. *Check:* same station, same antenna, old tool and new within one
   IFGR step / one LNA state of each other on three stations.
2. **STVT `mer_gain_cal` and `tune_antenna`** via attachment mode (c)
   (`attach.RestartPerCell`), exactly as `examples/hw_atsc_stvt.py` does it.
   The five copies of the environment block collapse into one `launch()`.
   *Check:* `tune_antenna`'s stored profiles for two antennas reproduce.
3. **STVT to mode (a)** — the real prize: teach `tv_live.py` to take IQ from a
   pipe or ZMQ so the loop owns the radio. Cells drop from ~30 s (a chain
   restart each) to ~10 s, level and rail looks become available (so `IMPULSE`
   and `OVERLOAD` are judged on samples, not on a `max|x|` regex), and gain can
   be re-trimmed *while the television plays*.
4. **Overnight cube / `hf_knob`** onto `Store.log()` + `hour_curve()`.
5. **Panels** (`tv_tuna_panel`, the radio panel): read `Verdict.as_dict()`
   instead of re-deriving classifications.

## Things the survey found that the migration should fix on the way

- **The adaptive-tv tuning tools never take the cooperative radio lock.** Only
  `iq_capture` and the panel do. rxtune takes it for every run, heartbeats in
  the measure loop, and yields between cells.
- **`writeSetting("rfgain_sel", ...)` is never read back, in either project**, and
  sits inside a bare `try/except`. rxtune reads every write back and says so
  when it cannot. (Measured during this work: `rfgain_sel` and the gain element
  `RFGR` are the same control, and its range on the RSPdx is 0-27, not 0-9.)
- **Chains are stopped with a hard `terminate()` while streaming.**
  `attach.graceful_stop()` sends CTRL_BREAK / SIGINT first; `tv_live.py` already
  has the handler for it.
- **A stale second copy of `adaptive-tv/` exists beside the repository**, and
  `config_shootout.py` still points into it. Pick one before migrating.
- `config_shootout` scores by the dial across configs that change what the dial
  *means* (equaliser averaging depth). `DialSpec.comparable_across_configs=False`
  makes the shootout score by decoded content instead — the rule the campaign
  notes already arrived at by hand.

## What should NOT move

Decoder internals, the judge's video-quality scoring, antenna fingerprinting,
the viewing stack. rxtune owns the radio-side loop and the verdict; everything
that knows what a television picture is stays where it is.
