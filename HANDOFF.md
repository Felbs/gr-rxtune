# gr-rxtune handoff — 2026-09-20 (no branch yet; not a git repo)

Standalone receiver tuning loop: a decoder-truth dial drives a search over
runtime-discovered SDR knobs, and the curve's shape becomes an honest
verdict. Extracted in spirit from two existing projects, which are NOT
modified by this work. State: **design gate — awaiting approval of
`docs/DESIGN.md`. No library code exists yet.**

## Read first
1. `docs/DESIGN.md` — layout, Dial/Knob/Verdict, message contract, §5 source-block findings, §7 open questions
2. `docs/PRIOR_ART.md` — what exists, what we borrow, licensing
3. `BUILD_LOG.md` — what was done and what is still unverified

## Rules for whoever continues
- Do not create the GitHub repo or push without the owner's explicit word.
- No station data, locations, call signs or personal info in this tree.
- Layer 1 (`src/rxtune`) must import with no GNU Radio and no SoapySDR.
- Any SDR touch goes through the `DeviceLock` protocol; the rig-specific
  lock adapter lives outside this repo. Never kill a process that is
  streaming from the radio.
- Every knob write is read back, or the mode is labelled open-loop.
- GNU Radio: radioconda, 3.10.12, Python blocks only.

## Next (after approval)
Scaffold (`pyproject.toml`, `gr_modtool`) -> `sim.py` + tests first ->
optimizer/shape/verdict -> Soapy discovery -> GR blocks -> example
flowgraphs -> hardware runs -> `docs/TEST_REPORT.md`, `docs/MIGRATION.md`.
