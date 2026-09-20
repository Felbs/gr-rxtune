# Prior art

Read 2026-09-20, before any design work. Everything here was checked against
the project's actual source or document unless marked UNVERIFIED. URLs are at
the end. "Dial" = the quantity the loop tries to improve.

**Summary.** Nobody ships the whole loop. What exists is (a) single-knob gain
servos welded into one decoder, driven by raw-sample proxies, (b) generic
power AGC, (c) a manual procedure that needs a human and a waterfall, and
(d) academic decision engines that adapt the *transmit waveform* or an
*equaliser*, in simulation. None of them searches several front-end knobs
against a decoder-truth dial, and none records or classifies the
dial-vs-gain curve. Those two things are what gr-rxtune adds. Almost every
*control-loop hygiene* idea we need, though, has already been worked out by
these projects, and we borrow those ideas with credit.

## Name check

`gr-rxtune` / `rxtune`: no GitHub repository, no CGRAN entry, no PyPI or
conda-forge package (checked 2026-09-20). Avoided on purpose: `gr-autotune`
(dodgymike, an FFT strong-signal finder) and `gr-adapt` (karel, adaptive
filters). One note: `RXTUNE` is a command verb in the OpenBTS / osmo-trx
transceiver control protocol ("set RX frequency"). It is a protocol word, not
a project, but it is why a code search for the string is noisy.

## 1. dump1090-fa adaptive gain (FlightAware) — GPL-2.0-or-later

`README.adaptive-gain.md`, `adaptive.c`. RTL-SDR only, one scalar gain.

- **Dial:** raw sample magnitudes. The decoder is used only as a *mask*
  (samples inside a decoded message are excluded).
- **Dynamic-range mode** (default): noise = 40th-percentile magnitude per 1 s
  block, EMA-smoothed (alpha 1/3); hold `-20*log10(noise/65536)` at a 30 dB
  target. Scan up until range < target, remember that as a gain ceiling,
  step down until it is met, idle. Re-probe upward only after 3600 s.
- **Burst mode** (off by default, "more experimental"): a 40 us window is
  loud if >25% of samples exceed -3 dBFS; 2-5 consecutive loud windows that
  did *not* decode = one overload burst. Gain down after 10 consecutive
  blocks above 5 bursts/block. Gain up only after 10 quiet blocks and never
  past the range-mode ceiling.
- **Settle:** control inhibited for 10 blocks (~10 s) after a range-mode
  change, 5 blocks after a burst-mode change; run counters reset on change.
- **Hysteresis:** down-scan only if `range + (next_down_step_dB / 2) <
  target` - half a gain step, computed from the *actual* dB size of the next
  step because the RTL table is non-uniform.
- **Arbitration:** modes vote `up` / `down` / `not_up`; down always wins.
- **CPU:** whole 50 ms sub-blocks are processed or skipped on a duty cycle
  (default 50%, README says 10% on armv6); burst rates are rescaled.
  Per-sample work is one histogram increment and a SIMD threshold count.
- **Borrow (ideas):** per-mode settle timers; half-step hysteresis in real
  dB; the "down wins" vote; duty-cycled measurement for the Pi; rare upward
  re-probe; a human-readable *reason string* on every change; time-at-gain
  statistics. Also the key insight behind burst mode, generalised: **a
  decoder dial cannot tell "no signal" from "clipped", so a cheap raw-sample
  overload detector belongs beside it as a veto.**
- **Differs:** proxy dial, one knob, local +/-1 stepping, no curve.

## 2. autogain1090 (wiedehopf/adsb-scripts) — MIT

- **Dial:** percentage of *decoded* messages stronger than -3 dBFS, from the
  decoder's stats JSON, differenced between runs.
- **Rule:** > 7% -> one gain step down; < 0.5% -> one step up; exit if fewer
  than 1000 messages (minimum-evidence guard). One step per night via a
  timer; applies by editing config and **restarting the decoder** (our
  attachment mode c). Its own wiki calls it "somewhat deprecated".
- **Borrow:** the minimum-evidence guard; the wide dead band.

## 3. readsb `--gain=auto` (wiedehopf) — GPL-3.0-or-later

**Not** the 7% / 0.5% heuristic (that is autogain1090 only - a common
conflation). Options `auto,<lowestGain>,<noiseLow>,<noiseHigh>,<loud>`,
defaults `0,34,36,243` on a 0-256 magnitude scale.

- **Dial:** raw-sample noise and loud-event counts over 56-sample windows,
  gathered inline in the preamble scan.
- **Rule** (every 0.5 s): loud or noise-high -> down 1; very loud (>5
  events) -> down 2; noise-low -> up 1 but only after a 15 s rise time
  (**fast attack, slow release**); a faster 1.5 s "rebound" rise when recent
  drops were caused by loud events. Startup uses a step multiplier of 8,
  halved each interval - a coarse-to-fine sweep in miniature. The jump to
  the RTL AGC pseudo-gain is rate-limited to once per 30 s "to avoid
  oscillations due to the large step".
- **Borrow:** asymmetric attack/release; halving step multiplier (maps
  onto our coarse grid -> fine sweep); rate-limiting any large step.

## 4. radiosonde_auto_rx, "Gain Optimization Notes" (Project Horus) — GPL-3.0, prose only

A **manual** procedure needing a human, a GUI and often moving the SDR to
another computer. Method 1: watch a sonde and raise gain until SNR stops
rising ("you've probably hit your system's performance limit"). Method 2, no
signal: raise gain from 0; the noise floor stays flat for ~15 dB, and the
knee where it starts to rise is the setting.

This is direct evidence of demand - an auto-tracking station with no way to
tune its own gain - and both methods are informal descriptions of exactly the
curve shapes our classifier names (rising slope, plateau, rollover;
flat-then-knee). Nobody has automated them.

## 5. wizardyesterday/AutomaticGainControl — GPL-3.0

Hardware-agnostic C library, Harris & Smith power AGC: `g += alpha * (R -
y)` in dB toward an operating point. The example of what we are **not**
doing (power says nothing about decodability), but its API seam is right:
`agc_init(..., setGainCallback, getGainCallback)` - the library never
touches hardware. It also has a **blanking counter** (ignore N measurements
after a gain write), a **dead band**, and **rail anti-windup**.

- **Borrow:** the get/set callback seam -> our `Knob` protocol; blanking ->
  settle; dead band. No code (pure-Python core; nothing to link).

## 6. simonholliday/substation — AGPL-3.0 (facts only, no text or code)

No tuning loop; static per-device gain defaults. Valuable for its documented
device facts, which become our **discovery test matrix**: HackRF has no AGC
and steps LNA in 8 dB / VGA in 2 dB; Airspy R2's driver *reports* an AGC it
does not really have; Airspy HF+ is an LNA on/off plus an RF **attenuator
ranged -48..0 dB** where 0 is maximum signal.

- **Licence:** AGPL cannot be relicensed to GPL-3. We cite facts only.
- **Lesson:** `hasGainMode()` can lie -> verify AGC-off by behaviour.

## 7. SoapySDR (BSL-1.0) and SoapySDRPlay3 (MIT)

The Knob contract. `listGains` ("in order RF to baseband"),
`getGainRange(name)` -> min/max/step, `setGain(name, value)`,
`has/setGainMode`, `listAntennas`, `getSampleRateRange`,
`getSettingInfo` -> `ArgInfo{key, value, type, range, options}`,
`read/writeSetting`. DriverGuide: attenuators "typically... a negative range
such as [-18 to 0 dB]".

Things the headers do not warn you about, found in source:

- The default **overall** `setGain` is a greedy RF-first fill that assumes
  every element is monotone-increasing. SoapySDRPlay3 does not override it,
  and both of its elements are *reductions* (`IFGR` 20-59 = gain reduction;
  `RFGR` = LNA state index, 0-27 on the RSPdx; higher = less gain). So
  overall gain on an SDRplay is, by inspection, inverted. **Rule: never use
  the overall path; drive elements; learn each element's sign empirically.**
- `IFGR` writes are ignored (warning only) while AGC is on.
- `ArgInfo.value` is a declared default, not device state (`biasT_ctrl`
  reports "true"). **Rule: readback or it did not happen.**
- `setGain` blocks on the API's gain-changed callback - a real, driver-known
  settle signal where available.

## 8. Cognitive-radio decision engines

- **Rondeau, Le, Rieser, Bostian (2004)**, "Cognitive Radios with Genetic
  Algorithms: Intelligent Control of Software Defined Radios", SDR Forum
  Technical Conference. Multi-objective GA over a chromosome of radio
  parameters; run-time weighted objectives (BER, PER, power, rate);
  out-of-limits settings get fitness 0. Adapts the *transmit waveform*.
  Also Rondeau's 2007 Virginia Tech dissertation.
- **Hill-climbing GA** (IEEE Xplore 6820357): refines each generation's best
  individual by hill climbing. UNVERIFIED beyond the abstract snippet.
- **Elkadi, M. M. (2020)**, "Cognitive and Adaptive Equalizer Implementation
  in GNU Radio", MS thesis, University of Arizona. Closest in spirit to our
  config shootout: an epsilon-greedy multi-armed bandit chooses equaliser
  tap count / step size; reward = running mean BER per arm; **engine and
  equaliser talk over GNU Radio message ports because stream feedback loops
  are impractical**; Python OOT blocks. Simulation only, BER against a
  *known* transmitted sequence, no over-the-air test.
- **Borrow:** message-port feedback (validates our Layer-2 shape); per-arm
  running statistics and pull counts for the shootout stage; constraint
  masking (known-bad combos score as invalid, not as low).
- **Differs:** receive front-end knobs, a *blind* dial from a real decoder on
  real RF, deterministic search with a physical interpretation of the curve.

## 9. igorauad/gr-dvbs2rx — GPL-3.0-or-later (layout model)

Standard gr_modtool tree plus `apps/`, plain-markdown `docs/` published via
Pages, Keep-a-Changelog `CHANGELOG.md`, `CITATION.cff`, `MANIFEST.md` (needed
for CGRAN later), CI on a matrix of official GNU Radio containers running
`ctest`, a formatting gate, 3.10-only with no maintenance branches. We copy
the structure. With Python-only blocks we have no ABI to manage, so Layer 1
is a plain pip-installable package and CMake exists only for Layer 2.

## Licensing conclusion

GPL-3.0-or-later for gr-rxtune stands. Everything we might borrow *code* from
is GPL-2+/GPL-3+/MIT/BSL (compatible); the one AGPL project is cited for
facts only. In practice we borrow ideas, not code, so no foreign copyright
headers are expected in the tree. If that changes, the file keeps its
original header.

## What is ours

| | dump1090 / readsb | autogain1090 | power AGC | CR engines | gr-rxtune |
|---|---|---|---|---|---|
| Dial | raw-sample proxy | decoded RSSI share | power | BER vs known data | decoder truth (MER/CNR/BER) + liveness |
| Knobs | 1 gain | 1 gain | 1 gain | waveform / EQ | N, discovered: gain elements, port, channel, config, hour |
| Search | +/-1 servo | +/-1 nightly | servo | GA / bandit | grid -> fine -> hill-climb, islands |
| Curve kept? | no | no | no | no | yes - and classified |
| Says "it's physical"? | no | no | no | no | yes, in dB |
| Any SDR / any decoder | no / no | no / 2 | yes / n.a. | n.a. | yes / yes |

## URLs read

- github.com/flightaware/dump1090: `README.adaptive-gain.md`, `adaptive.c`, `dump1090.c`, `sdr_rtlsdr.c`, `LICENSE`
- github.com/wiedehopf/adsb-scripts: `autogain-install.sh`; wiki "Automatic gain optimization for readsb and dump1090-fa"
- github.com/wiedehopf/readsb (dev): `readsb.c`, `demod_2400.c`, `sdr_rtlsdr.c`, `README.md`
- github.com/projecthorus/radiosonde_auto_rx wiki: "Gain Optimization Notes"
- github.com/wizardyesterday/AutomaticGainControl: `README.txt`, `AutomaticGainControl.h/.c`
- github.com/simonholliday/substation: `README.md`
- github.com/pothosware/SoapySDR: wiki DriverGuide + FAQ, `Device.hpp`, `Types.hpp`, `lib/Device.cpp`
- github.com/pothosware/SoapySDRPlay3: `Settings.cpp`, `SoapySDRPlay.hpp`, `CMakeLists.txt`
- Rondeau et al. 2004 (SDR Forum proceedings, archived copy); hdl.handle.net/10919/29199
- repository.arizona.edu/handle/10150/650872 (Elkadi 2020)
- github.com/igorauad/gr-dvbs2rx: tree, `docs/`, `.github/workflows/test.yml`, `CHANGELOG.md`, `MANIFEST.md`, `CMakeLists.txt`
- github.com/gnuradio/gnuradio (maint-3.10 = v3.10.12.0) and osmocom/gr-osmosdr - see `DESIGN.md` section 5
