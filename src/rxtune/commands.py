# SPDX-License-Identifier: GPL-3.0-or-later
"""Knob writes as command dictionaries, in the dialect each GNU Radio source
block actually accepts. Verified against the GNU Radio 3.10 source (see
docs/DESIGN.md section 5). Plain Python here; the controller block converts
with pmt.to_pmt, so this is testable with no GNU Radio."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

DIALECTS = ("soapy", "uhd", "generic")


class Unsupported(ValueError):
    """The target block cannot do this by message."""


SOAPY_SETTING_WARNING = (
    "gr-soapy applies a 'setting' command PER CHANNEL and validates the key against the "
    "channel's setting list. Drivers whose settings are device-level (SoapySDRPlay: biasT_ctrl, "
    "rfnotch_ctrl, ...) reject it with 'Invalid setting', and because gr-soapy does not catch "
    "exceptions in its command handler, that STOPS THE SOURCE BLOCK (measured on an RSPdx). "
    "Use the Message Setter block -> write_setting(key, value), or pass allow_unsafe=True if "
    "your driver's settings are per-channel.")


def command(dialect: str, knob: str, value: Any, chan: int | None = None,
            allow_unsafe: bool = False) -> Dict[str, Any]:
    """One command dict for one knob write."""
    kind, _, key = knob.partition(":")
    if dialect == "generic":
        return {"knob": knob, "value": value}
    if dialect == "soapy":
        # gr-soapy: dict ONLY. Per-element gain = {"gain": {"name", "gain"}}.
        if kind == "gain":
            body: Dict[str, Any] = {"gain": {"name": key, "gain": float(value)} if key else float(value)}
        elif kind == "antenna":
            body = {"antenna": str(value)}
        elif kind in ("freq", "rate", "bandwidth"):
            body = {kind: float(value)}
        elif kind == "agc":
            body = {"gain_mode": bool(value)}
        elif kind == "setting":
            if not allow_unsafe:
                raise Unsupported(SOAPY_SETTING_WARNING)
            # gr-soapy's handler type-tests the wrong variable, so only STRING
            # values survive; a PMT bool or number throws wrong_type.
            body = {"setting": {"key": key, "value": _as_setting_string(value)}}
        else:
            raise Unsupported(f"gr-soapy has no command for '{knob}'")
    elif dialect == "uhd":
        if kind == "gain":
            if key:
                raise Unsupported(f"gr-uhd sets only OVERALL gain by message; '{knob}' names an "
                                  "element. Use the Message Setter block (set_gain(g, name, chan)).")
            body = {"gain": float(value)}
        elif kind == "antenna":
            body = {"antenna": str(value)}
        elif kind in ("freq", "rate", "bandwidth", "lo_offset"):
            body = {kind: float(value)}
        else:
            raise Unsupported(f"gr-uhd has no command for '{knob}'")
    else:
        raise ValueError(f"unknown dialect '{dialect}' (one of {DIALECTS})")
    if chan is not None:
        body["chan"] = int(chan)
    return body


def _as_setting_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def commands(dialect: str, setting: Dict[str, Any], previous: Dict[str, Any] | None = None,
             chan: int | None = None, allow_unsafe: bool = False) -> List[Tuple[str, Dict[str, Any]]]:
    """Commands for everything in `setting` that differs from `previous`."""
    out = []
    for knob, value in setting.items():
        if previous is not None and previous.get(knob) == value:
            continue
        out.append((knob, command(dialect, knob, value, chan, allow_unsafe)))
    return out


def check(dialect: str, knobs, allow_unsafe: bool = False) -> List[str]:
    """Problems that would only show up as an exception inside someone else's
    message handler. Call before the first publish."""
    problems = []
    for k in knobs:
        try:
            command(dialect, k, 0, allow_unsafe=allow_unsafe)
        except Unsupported as e:
            problems.append(str(e))
    return problems
