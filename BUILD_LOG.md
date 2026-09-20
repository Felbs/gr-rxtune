# Build log

Newest at the bottom. One entry per working session: what was done, what
was verified and how, what is open.

## 2026-09-20 — session 1: name check, prior art, design gate

- Directory created. No git repo yet, nothing pushed, no GitHub repo.
- Name check `gr-rxtune` / `rxtune`: GitHub repos 0, CGRAN none, PyPI 404,
  conda-forge 404. Note: `RXTUNE` is an osmo-trx/OpenBTS protocol verb.
- Prior art read from source -> `docs/PRIOR_ART.md`. Correction to the
  brief: readsb `--gain=auto` is a noise/loud-event servo; the 7% / 0.5%
  strong-message rule is the external autogain1090 script only.
- Source-block command handlers read from GNU Radio maint-3.10 (identical
  to the local 3.10.12.0) and gr-osmosdr master -> `docs/DESIGN.md` §5.
  Headline: gr-soapy takes per-element gain by message; gr-uhd overall
  only; gr-osmosdr source has no message port (its GRC yml draws one).
- Existing loop surveyed read-only in the two source projects (no changes
  made there). Noted for migration: tuning tools do not take the device
  lock; `rfgain_sel` writes are never read back; base-env block is
  duplicated in five files.
- `docs/DESIGN.md` written. **STOPPED for approval** — no library code yet.

Unverified, carried forward: scheduler behaviour when the gr-soapy command
handler throws; whether SoapySDRPlay3 treats channel-level `writeSetting`
the same as device-level; RSPdx `RFGR` vs `rfgain_sel` aliasing; whether a
null UHD device can be constructed for handler tests.
