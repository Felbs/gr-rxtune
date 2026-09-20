# gr-rxtune design (DRAFT for approval — no bulk code written yet)

Status 2026-09-20: name check and prior-art read done (`PRIOR_ART.md`).
This document is the gate. Nothing below is built.

## 1. What it is

A receiver tuning loop that a stranger can point at their own decoder:

1. Find the decoder's continuous truth-dial and surface it live.
2. Search every available knob against it (grid -> fine -> hill-climb).
3. Classify the failure from the SHAPE of the curve and say so in dB,
   including "this is physical; software cannot fix it".
4. Refuse to call anything good without liveness proof.
5. Treat every result as perishable: recalibrate on RF-path change and time.

**The loop owns the radio; decoders are consumers that report a dial.**
Attachment modes, in order of preference:
a) loop owns the SDR and feeds IQ to the decoder (stdin / file / ZMQ /
   flowgraph); b) decoder exposes a runtime control, tiny adapter;
c) restart the decoder per grid cell (slow calibration only).

## 2. Layout

```
gr-rxtune/
  README.md  COPYING(GPL-3.0-or-later)  CHANGELOG.md  MANIFEST.md  CITATION.cff
  HANDOFF.md  BUILD_LOG.md
  pyproject.toml                 # Layer 1: `pip install .` -> package `rxtune`
  CMakeLists.txt                 # Layer 2: gr_modtool scaffold, python-only
  src/rxtune/                    # LAYER 1 — no GNU Radio, no SoapySDR import at module top
    dial.py        # DialSpec, Reading, Dial protocol, Liveness protocol
    knob.py        # Knob protocol, KnobSpec, Axis, readback/verify
    frontend.py    # Frontend protocol (owns device; apply(setting), describe())
    soapy.py       # SoapyFrontend: runtime discovery -> knobs (lazy import)
    profile.py     # YAML device profiles (data, not code) + loader/validator
    profiles/sdrplay_rspdx.yaml
    measure.py     # settle -> discard -> average (median/p10/p90), min-evidence guard
    overload.py    # raw-sample veto: clip fraction / rail count (dial-independent)
    optimize.py    # coarse grid, fine sweep, hill-climb, ridge + island handling, pick policies
    shape.py       # curve-shape classifier
    verdict.py     # Verdict: class, margin dB, confidence, advice, liveness gate
    stages.py      # stage pipeline (usb/rate probe ... verdict + profile)
    store.py       # per-antenna result profiles (JSON), staleness, recal triggers, JSONL log
    lock.py        # DeviceLock protocol; NullLock default; optional adapter hook
    attach.py      # decoder attachment: PipeFeed (a), ControlAdapter (b), RestartPerCell (c)
    sim.py         # simulated receiver for tests + `selftest`
    cli.py         # `rxtune discover | sweep | tune | selftest | report`
  python/rxtune_gr/ (OOT python module `gnuradio.rxtune`)   # LAYER 2
    controller.py  dial_probe_mpsk.py  dial_from_tag.py  dial_adapters.py
    msg_setter.py  dashboard.py
  grc/  rxtune_*.block.yml
  examples/  loopback_stock.grc  loopback_qtgui.grc  soapy_live.grc  headless_loopback.py
  tests/  (pytest, Layer 1)      python/rxtune_gr/qa_*.py (Layer 2, ctest)
  docs/  PRIOR_ART.md DESIGN.md MIGRATION.md TEST_REPORT.md USAGE.md
```

Layer 1 depends on numpy + PyYAML only. SoapySDR is imported lazily inside
`soapy.py` so the core, the simulator and all tests run with no radio stack.

## 3. Interfaces (Layer 1)

```python
@dataclass(frozen=True)
class DialSpec:
    name: str                    # "MER", "CNR", "BER", "crc_rate"
    units: str                   # "dB", "ratio", "msgs/s"
    higher_is_better: bool
    cliff: float | None          # decode threshold in dial units, if known (ATSC MER 15.2)
    settle_s: float              # discard this long after any knob write
    window_s: float              # then average this long
    continuous_below_cliff: bool # True: MER/CNR/Es-N0/Viterbi BER. False: CRC-rate dials.
    comparable_across_configs: bool = True  # False when a config knob changes the dial's meaning
    min_samples: int = 5         # minimum-evidence guard (autogain1090's lesson)

@dataclass
class Reading:
    value: float | None          # None = dial produced nothing in the window
    n: int; p10: float; p90: float; spread: float
    overload: float              # 0..1 from the raw-sample veto, NaN if unavailable
    alive: bool | None           # liveness verdict for this window; None = no liveness source
    t: float

class Dial(Protocol):
    spec: DialSpec
    def read(self) -> float | None: ...        # latest instantaneous value, non-blocking

class Liveness(Protocol):                      # TS packets, headers, audio frames, CRC-ok count
    def count(self) -> int: ...                # monotone counter of decoded-content units
```

`continuous_below_cliff=False` changes behaviour, not just a label: rescue
search is refused ("this dial is zero until decode; use a continuous dial or
an RF proxy to find the signal first"), and only range optimisation runs.
`comparable_across_configs=False` makes the config-shootout stage score by
liveness rate instead of the dial (the FS_AVG lesson).

```python
@dataclass(frozen=True)
class KnobSpec:
    name: str                    # "gain:IFGR", "antenna", "setting:biasT_ctrl", "freq", "config", "hour"
    kind: Literal["range", "choice"]
    lo: float | None; hi: float | None; step: float | None
    choices: tuple | None        # antenna names, RTL gain table, config names
    sense: Literal["gain", "reduction", "unknown"]   # unknown -> learned empirically
    settle_s: float              # knob-specific (bias-T 1 s, retune 0.15 s, gain ~0.1 s)
    cost: Literal["fast", "slow", "restart"]          # orders the search; "restart" = mode (c)

class Knob(Protocol):
    spec: KnobSpec
    def set(self, value) -> None: ...
    def get(self): ...           # READBACK. set() then get() mismatch -> KnobWriteError
```

Knobs come from three places, all through the same protocol:
discovered (`SoapyFrontend.discover()`: gain elements, antennas, sample
rates, settings from `getSettingInfo`), supplied by the user's decoder
(config/env knobs, `cost="restart"`), and virtual (`hour`, which the loop
cannot set, only record — the overnight-cube axis).

Discovery rules (from `PRIOR_ART.md` §7): never use overall gain; AGC forced
off and verified by behaviour, not `hasGainMode`; element sign is `unknown`
unless a profile says otherwise, and is learned in the first sweep from the
raw level vs value; negative ranges and discrete steps are just `lo/hi/step`
or `choices`; every write is read back.

**Device profile** (YAML, optional, data only): per-knob `sense`,
`settle_s`, `known_bad` combos (masked, not scored), `bias_t` settings key,
`agc_blocks` (knobs ignored while AGC on), `quirks` (free text surfaced in
the report), `regime_knobs` vs `level_knobs` (our AGC finding: RF-stage
selection is a regime knob the search owns; IF level may be delegated to
hardware AGC with a setpoint). Ships with `sdrplay_rspdx.yaml`. Blind
operation must work; the profile only makes it faster and better-worded.

```python
class Verdict:
    shape: Shape           # see below
    best: Setting | None   # None when nothing may be recommended
    dial: float; margin_db: float | None      # vs cliff
    alive: bool            # False forces best=None unless spec has no liveness source -> "UNPROVEN"
    physical: bool         # True = software cannot fix it
    headline: str          # one honest sentence with numbers
    evidence: dict         # the curve, the rule that fired, thresholds used
    stale_after: ...       # path fingerprint + max age
```

Shapes (each = a named rule over the curve, thresholds in one table,
every one proven against `sim.py`):

| Shape | Signature | Says |
|---|---|---|
| `HEALTHY` | peak above cliff, liveness flowing | margin in dB |
| `APERTURE_LIMITED` | dial flat vs gain (< eps over the span), below cliff | physical; N dB short |
| `OVERLOAD` | dial falls at high gain and/or overload veto rises | back off; ridge location |
| `OVERLOAD_STAIRCASE` | monotone rise into the low-gain edge | extend grid toward attenuation (auto) |
| `ISLAND` | isolated optimum separated from the main ridge | keep it, fine-sweep it, warn it is narrow |
| `PLUMBING` | dial healthy, liveness zero | not RF; look downstream |
| `FADING` | dial oscillates over time at fixed setting (p10 < cliff <= median) | multipath; physical |
| `IMPULSE` | dial high + persistent rail transients | impulse noise; physical / move antenna |
| `NO_SIGNAL` | no dial and no level change anywhere | nothing there, or a phantom carrier |
| `STARVED` | raw level near zero at every cell | span max gain before judging (grid-span lesson) |
| `UNPROVEN` | good dial, no liveness source given | will not call it good |

Optimizer: coarse grid over fast knobs -> staircase auto-extension -> fine
sweep around the top-K *distinct* regions (so an island is not lost to the
main ridge) -> hill-climb with half-step hysteresis -> pick policy (`max`,
`knee` = lowest gain within X dB of best, `headroom` = near-tie goes to fewer
rails). Slow/restart knobs are outer loops. Near-miss rule: coarse result
within 0.5 dB under the cliff always gets a fine sweep before any verdict.
Every move logs a reason string.

Stage order (from the MER-dial campaign): USB/rate probe -> interferer
census -> port + carrier scan -> dial gain grid -> channel survey ->
config shootout -> verdict + per-antenna profile. Each stage is optional and
skippable; a stranger with one knob and one dial still gets grid + verdict.

Staleness: results are keyed by an RF-path fingerprint (device, port,
bias-T, user-supplied antenna label) and never reused across a change;
recal triggers = path change, dial sag vs stored, age, explicit.

Device sharing: `DeviceLock` protocol, `NullLock` by default. On this rig an
adapter to the existing cooperative lock is supplied from *outside* the repo
(local config, not committed), with heartbeat in the measure loop,
yield checks between cells, and a stop-file check per cell.

## 4. Message-port contract (Layer 2)

Blocks: **Controller** (headless), **Dial probes/adapters**, **Dashboard**
(optional Qt), **Msg Setter** (tiny shim, see §5).

| Port | Dir | Payload |
|---|---|---|
| `dial` | in | bare `pmt double` **or** pair `(key . double)` **or** dict `{value, [alive], [overload]}` — all three accepted, because the stock SNR probe emits a bare double and qtgui widgets emit pairs |
| `liveness` | in | `pmt long` counter or pair; optional |
| `ctrl` | in | dict: `{start}`, `{stop}`, `{recal}`, `{hold: setting}` |
| `cmd` | out | **one PMT dict per knob write**, in the target dialect (below) |
| `status` | out | dict: state, cell index, current setting, last reading, reason string |
| `verdict` | out | dict form of `Verdict` + the curve (dashboard and Message Debug read this) |

`cmd` dialects (controller parameter `dialect`):
- `soapy`: `{"gain": {"name": "IFGR", "gain": 40.0}}`, `{"antenna": "..."}`,
  `{"freq": f}`, `{"setting": {"key": k, "value": "<string>"}}`, `gain_mode`.
  Always dicts; values for `setting` always stringified (see §5).
- `uhd`: `{"gain": g}`, `{"antenna": ...}`, `{"freq": ...}`, optional `chan`.
- `generic`: `{"knob": name, "value": v}` for Msg Setter and user blocks.

The controller runs the Layer-1 optimizer as a state machine driven by dial
messages and a wall-clock settle timer; it never blocks the scheduler thread.
It knows its knobs either from parameters (GRC: list of axes) or, in
**device-handle mode**, from a `SoapyFrontend` it opens itself (no source
block owning the radio; IQ enters the flowgraph through our own thin source
or ZMQ). Device-handle mode is the route to readback, discovery and the
raw-sample overload veto; message mode is the route to "drops into any
existing flowgraph".

## 5. What the source blocks actually accept (verified in source, GR 3.10.12 = maint-3.10)

| | gr-soapy source | gr-uhd usrp_source | gr-osmosdr source |
|---|---|---|---|
| Port | `cmd` | `command` | **none** |
| Format | **dict only** (`"commands must be pmt::dict"`) | dict, pair, legacy tuple | - |
| Overall gain | `gain`: number | `gain`: double | setter only |
| **Per-element gain by message** | **YES** - `gain`: `{name, gain}` -> `set_gain(ch, name, g)` | **NO** - `to_double` only (API has `set_gain(g, name, chan)`) | no port (API has `set_gain(g, name, chan)`) |
| Antenna / freq / rate / bw | yes | yes (+ `lo_offset`, `tune`, `time`) | setters only |
| AGC | `gain_mode`: bool | - | `set_gain_mode` |
| Device settings | `setting`: `{key, value}` | - | - |

Findings that shape the design:

1. **gr-soapy supports per-element gain by message.** Good news; message
   mode is fully capable there.
2. **gr-soapy `setting` handler bug:** it type-tests the outer dict instead
   of the value, so only *string* values work; a PMT bool/number throws
   `wrong_type`. We always send strings. Worth an upstream report (later,
   with your say-so).
3. **gr-soapy does not catch exceptions in the handler**; an unknown gain
   name throws. Message mode therefore validates names against the
   configured axis list before publishing. Behaviour of the scheduler on
   that throw is UNVERIFIED - to be tested.
4. **gr-osmosdr has no command port at all**, although its GRC yml draws
   one (`id: command`). Verified on the local 0.2.6 install:
   `message_ports_in()` is empty. For osmosdr (and UHD named gains) the
   answer is **Msg Setter**: a 30-line block that takes `generic` commands
   and calls setter methods on a block object passed by id - the same
   pattern the stock `soapy_sdrplay_source` yml uses internally.
5. **No readback exists over any message port.** Message mode is open-loop
   on the knob side; "readback or it did not happen" is only enforceable in
   device-handle mode or via Msg Setter (which can call getters). The
   status output says which mode is in force; the verdict's confidence is
   reduced in open-loop mode.
6. `channels.channel_model` has **no message ports** - the stock-block
   loopback closes through Msg Setter -> `set_noise_voltage`. Strictly:
   all DSP is stock; the one non-stock block in the loop besides the
   controller is the setter shim. I will say so plainly in the test report.
7. `probe_mpsk_snr_est_c` publishes a bare double in dB on `snr` - wires
   straight into `dial`. `mpsk_snr_est_cc` emits *tags*, hence
   `dial_from_tag`. These estimators are M-PSK only; examples use QPSK.
8. Qt: in-tree Python widgets (`GrLevelGauge`, `MsgPushButton`...) give the
   pattern - inherit `gr.sync_block` + a QWidget, update via `pyqtSignal`
   (handlers run on a scheduler thread), yml `gui_hint` param + `${gui_hint()
   % win}`. The dashboard follows it exactly; PyQt5 5.15 is what radioconda
   ships. Heatmap drawn with QPainter (no matplotlib dependency).

Local stack found: radioconda at the user profile, GNU Radio 3.10.12.0,
Python 3.12, PyQt 5.15, SoapySDR 0.8.1 with the SDRplay module, UHD 4.9,
gr-osmosdr 0.2.6. `gnuradio-companion` is not on PATH (use the radioconda
env).

## 6. Test plan (detail in TEST_REPORT.md as it is executed)

1. **Core, no radio** - pytest against `sim.py` (noise floor, overload
   ridge, island, staircase, fading, impulse, plumbing, starved, inverted
   and discrete and negative-range knobs, a knob that ignores writes, a lying
   AGC flag). Every shape verdict has a positive and a negative control.
   `rxtune selftest` = lettered numeric stages, PASS/FAIL exit code.
2. **GR loopback, stock DSP** - QPSK source -> channel_model -> probe SNR
   -> controller -> Msg Setter -> channel_model, headless, asserting the
   loop converges to the known optimum of an injected synthetic ridge.
3. **GR + qtgui** - same, with QT GUI Range, Time/Freq/Number sinks,
   Message Debug and our dashboard docked; run N seconds, assert no
   exceptions, widget parented, clean shutdown. Also `.grc` -> `grcc`
   compile check for every example.
4. **Command-handler compatibility** - real `soapy.source` on a null/file
   device if one constructs, real `uhd` only if a null device constructs
   (likely not - then dialect unit tests against the documented table, and
   reported as "not run against the block"), osmosdr via Msg Setter on a
   `file=` source.
5. **Hardware** - RSPdx via Soapy: one ATSC channel (MER dial), one FM HD
   station (CNR/BER dial). Cooperative lock taken at lab priority, yields
   honoured, nothing killed. If the radio is not on this machine or is busy
   it is reported as not run. *(Open question 1.)*

## 7. Open questions for you

1. **Where is the RSPdx right now?** Last note I have says it moved to the
   Pi. Hardware tests run wherever it is; if on the Pi, that also gives the
   headless test for free. Tell me which box.
2. **RFGR vs `rfgain_sel`.** SoapySDRPlay3 exposes the RSPdx LNA state as
   gain element `RFGR` 0-27, while our tools write the setting `rfgain_sel`
   (0-9 in our notes). I will treat both as discovered knobs and find out
   on hardware which is live and whether they alias; the profile records it.
3. **Package names:** Python `rxtune` (core) and `gnuradio.rxtune` (OOT).
   OK?
4. **Upstream bug reports** (gr-soapy `setting` type test; gr-osmosdr yml
   phantom port): I will write them up in `docs/` only; filing is your call.

## 8. Not in this pass

Migrating STVT/radiotuna (described in `MIGRATION.md` only), conda recipe,
CGRAN listing, C++ blocks, GitHub repo creation, any push.
