# Using rxtune

## Install

```
pip install .                         # core only: numpy + PyYAML
pip install -e .[test] && pytest      # development
```

GNU Radio blocks (3.10, Python only, nothing to compile):

```
mkdir build && cd build && cmake .. && make install     # blocks + core package + GRC yml
```

or, with nothing installed, run from the source tree: `examples/_devpath.py`
grafts `python/` onto the `gnuradio` package, and GRC finds the blocks if
`GRC_BLOCKS_PATH` includes this repo's `grc/` directory.

> If `gr_modtool`, `grcc` or GRC behave as if they belonged to a different GNU
> Radio, check the environment for a stale `GR_PREFIX` / `GRC_BLOCKS_PATH`
> pointing at another install. It cost this project an hour.

## 1. Bring a dial

Anything your decoder can report as one number. Say what it is:

```python
DialSpec("MER", "dB", cliff=15.2,      # decode threshold, if you know it
         settle_s=9, window_s=12,      # how long YOUR decoder needs after a gain change
         continuous_below_cliff=True)  # False for CRC / message-rate dials
```

**`settle_s` is the parameter people get wrong.** It must cover the hardware,
the decoder's loops, *and any averaging inside the estimator*. GNU Radio's
M-PSK SNR estimator at its default `alpha=0.001` still describes the previous
gain setting many seconds later; a loop that does not wait chases its own tail.
And a demodulator with **hysteresis** (one that does not re-lock after an
overload cell) poisons every later cell: bound its loops, or make `settle_s`
cover re-acquisition. Both were found the hard way in `examples/headless_loopback.py`.

Sources of a dial: `LineScraper` (a regex over the decoder's log),
`CallableDial` (a function), `LatchDial` (something pushes values in), or in
GNU Radio any message: a bare number, a `(key . number)` pair, or
`{value, level_db, clip}`.

## 2. Bring liveness

`PatternCounter(path, b"\x00\x00\x01\xb3")`, `FileGrowth(path)`, a regex with a
counter group, or `CallableLiveness(fn)`. Without it every good result is
`UNPROVEN` and **no setting is recommended** — by design.

Prefer content over activity: a demodulator can write full-rate null packets
forever, so file growth proves nothing for a transport stream; count sequence
headers instead.

## 3. Let the loop own the radio

```python
fe = SoapyFrontend("driver=...", rate=..., freq=...)
fe.apply_profile(profile.find(fe.info))      # optional
fe.start(sink=decoder_stdin, transform=to_what_the_decoder_wants)
report = tune(fe, dial, liveness, health=decoder.health, lock=lock.from_env())
```

`rxtune discover` prints what the device reports. Discovered knobs:
`gain:<element>`, `antenna`, `setting:<key>`, `freq`. The default search is
every fast gain element; pass `search=[...]`, `fixed={...}`, and
`start={knob: [values]}` to restrict the *first* grid (the staircase rule
extends it if the answer is outside).

Outer loops — ports, channels, recovery configs — are `stages.survey(...)`.

## 4. Read the verdict

| Shape | Means | Recommended? |
|---|---|---|
| `HEALTHY` | peak over the cliff, content decoding | yes |
| `ISLAND` | works, but only in a narrow gain window with failure both sides | yes, with a warning |
| `FADING` | the dial swings across the cliff at one fixed setting: multipath | if decoding; physical |
| `IMPULSE` | rail transients at *every* gain while the dial stays high | if decoding; physical |
| `PLUMBING` | healthy dial, nothing decoded: look downstream of the demodulator | no |
| `UNPROVEN` | good dial, no liveness source supplied | no |
| `APERTURE_LIMITED` | flat versus gain, under the cliff, no clipping | no — physical |
| `GAIN_LIMITED` | still rising at maximum gain | no — a preamp would help |
| `OVERLOAD` | clipping even at minimum gain | no — attenuate ahead of the radio |
| `BELOW_CLIFF` | a peak exists, under threshold | no |
| `NO_SIGNAL` / `STARVED` | no dial anywhere / the converter hears only itself | no |
| `BLIND_DIAL` | a CRC-rate dial was asked to rescue a non-decoding signal | no |

`Verdict.rule` is the sentence for the rule that fired, `Verdict.evidence` the
numbers, `shape.THRESHOLDS` every threshold (a device profile may override
them). A decoder that **dies** raises `DecoderDied`; it is never reported as an
RF verdict.

## 5. In GNU Radio

Two ways to drive the radio from the Controller block:

- **Message mode** (drops into an existing flowgraph). Pick the dialect of the
  block on the other end of `cmd`. It is *open-loop* unless a Message Setter's
  `readback` is wired back in.
  - `soapy`: per-element gain, antenna, freq, rate. **Not** device settings
    (refused unless `allow_unsafe=True`: on SoapySDRPlay a `setting` command
    stops the source block — measured).
  - `uhd`: overall gain, antenna, freq. Named gain elements: use the setter.
  - `generic`: `{knob, value}` for the **Message Setter**, which calls setter
    *methods* on any block: the osmocom source (no message port at all),
    `write_setting` on a Soapy source, Channel Model, Multiply Const.
    Do **not** use gr-soapy's `read_setting` as a getter for boolean settings:
    it returned `True` regardless of state in our measurements.
- **Device-handle mode** (Python flowgraphs): pass
  `frontend=SoapyFrontend(...)` to the controller. It writes knobs itself with
  readback, and the flowgraph gets its samples from the frontend's sink (ZMQ,
  a pipe). This is the only mode with true readback *and* a raw-sample overload
  veto.

Examples: `examples/loopback_stock.grc` (no GUI), `examples/loopback_qtgui.grc`
(with QT GUI Range, Time, Frequency and Number sinks, Message Debug and the
dashboard), `examples/headless_loopback.py`.

## 6. Sharing one radio

`RXTUNE_LOCK=/path/to/your_lock.py` plugs in a cooperative lock module exposing
`acquire(owner, purpose, priority, wait_s)`, `heartbeat()`, `release(owner)` and
optionally `should_yield()` / `stop_requested(owner)`. rxtune heartbeats while
measuring, checks for a yield between cells, stops cleanly (`Yielded`) and
always releases. It never kills a process that is streaming from a radio;
`attach.graceful_stop()` asks first.

## 7. Device profiles

YAML, data only, validated (unknown keys are an error). See
`src/rxtune/profiles/sdrplay_rspdx.yaml`: per-knob `sense`, `settle_s`, `role`
(`regime` vs `level`), knobs to `ignore`, `known_bad` combinations (masked, not
scored — e.g. a DAB notch over TV band III), threshold overrides, and `quirks`
that are printed for the user. The loop works with no profile; send yours.
