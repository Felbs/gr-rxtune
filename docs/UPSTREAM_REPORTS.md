# Draft bug reports for other projects (NOT filed)

While testing gr-rxtune against the stock SDR source blocks we found three
defects that are not ours. These are drafts, written so that a maintainer can
reproduce each one in a minute. Whether and when to file them is the repository
owner's decision. Versions: GNU Radio 3.10.12.0 (conda-forge / radioconda,
Windows), SoapySDR 0.8.1, SoapySDRPlay3, gr-osmosdr 0.2.6, SDRplay RSPdx.

---

## 1. gr-soapy: a `setting` command message stops the source block

**Where:** `gnuradio/gnuradio`, `gr-soapy/lib/block_impl.cc`, `cmd_handler_setting`
and `msg_handler_cmd`.

**What happens.** Posting `{"setting": {"key": "rfnotch_ctrl", "value": "true"}}` to
a Soapy source's `cmd` port on an SDRplay device logs

```
thread_body_wrapper :error: ERROR thread[thread-per-block[0]: <block source(0)>]: Invalid setting: rfnotch_ctrl
```

and the source block's thread ends: the flowgraph keeps "running" but the source
delivers **0 samples/s** and ignores every later command. Measured by counting
items downstream (2.10 MS/s before, 0.000 after) and by a following `freq`
command not being applied.

**Why (three separate things, by reading the source):**
1. The handler only ever calls the **per-channel** `write_setting(channel, key, ...)`
   (there is a `// TODO: any way to call channel-less version?`). Drivers whose
   settings are device-level - SoapySDRPlay's `biasT_ctrl`, `rfnotch_ctrl`,
   `dabnotch_ctrl`, `agc_setpoint` ... - have no such channel setting, so the
   validation throws `Invalid setting`.
2. `msg_handler_cmd` does not catch exceptions from handlers, so any throw (this
   one, or `Unknown gain <name>` from a misspelt gain element) terminates the block
   thread. gr-uhd's equivalent catches `pmt::wrong_type` per key and logs it.
3. The handler type-tests the wrong variable: `if (pmt::is_bool(val)) ... else if
   (pmt::is_number(val))` where `val` is the outer dict rather than `value_pmt`.
   Both tests are therefore always false, and only **string** values are accepted;
   a PMT bool or number raises `pmt::wrong_type` (which, by 2, also kills the block).

**Reproduce:** `util/hw_soapy_cmd.py --setting rfnotch_ctrl=true --try-setting-message`
in this repository, or any flowgraph with a Message Strobe into a Soapy SDRplay
source's `cmd` port.

**Suggested fix:** try the channel-level write and fall back to the device-level
one; test `value_pmt`; wrap handler dispatch in try/catch and log.

**What works, for contrast:** per-element gain (`{"gain": {"name": "IFGR", "gain": 45.0}}`),
`antenna` and `freq` by message all work and were read back correctly on hardware.

---

## 2. gr-soapy (Python): `read_setting()` returns True for a boolean in every state

**What happens.** On an SDRplay device:

```
write_setting("rfnotch_ctrl", "false") -> measured power  -7.9 dB   read_setting -> True
write_setting("rfnotch_ctrl", "true")  -> measured power -49.1 dB   read_setting -> True
write_setting("rfnotch_ctrl", False)   -> measured power  -7.8 dB   read_setting -> True
write_setting("rfnotch_ctrl", True)    -> measured power -49.1 dB   read_setting -> True
```

The write works (a 41 dB change at 100 MHz with the FM notch); the read does not
reflect it. The same device through the raw SoapySDR Python binding
(`readSetting`) returns `'false'` / `'true'` correctly.

**Probable cause (not verified in source):** the string `"false"` being converted
to a boolean by truthiness (a non-empty string is true) somewhere between
`SoapySDR::Device::readSetting` and the returned PMT/Python object.

**Impact:** any flowgraph that verifies a setting by reading it back is told it
succeeded, always.

---

## 3. gr-osmosdr: GRC shows a `command` message port the source does not have

**Where:** `osmocom/gr-osmosdr`, `grc/gen_osmosdr_blocks.py` (and the generated
`osmosdr_source.block.yml`, `rtlsdr_source.block.yml`) versus `lib/source_impl.cc`.

**What happens.** The GRC block definition declares

```yaml
inputs:
- domain: message
  id: command
  optional: true
```

but the source block registers no message port: `source_impl.cc` contains no
`message_port_register_in`, and at run time

```python
osmosdr.source(args="numchan=1 file=...").message_ports_in()   # -> #()
```

So GRC draws a port that cannot be connected; users reasonably assume the block
accepts the same `freq` / `gain` commands as the UHD and Soapy sources.

**Suggested fix:** either implement the command handler (the setters already
exist: `set_center_freq`, `set_gain(gain, name, chan)`, `set_antenna` ...) or drop
the port from the GRC definitions.

---

## Not a bug, but worth knowing

`channels.channel_model.noise_voltage()` returns the value that was set divided by
sqrt(2) (it reports the internal noise source amplitude), so a naive
`set_noise_voltage(x)` / `noise_voltage()` readback appears to fail.
