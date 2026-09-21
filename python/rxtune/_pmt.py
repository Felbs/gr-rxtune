# SPDX-License-Identifier: GPL-3.0-or-later
"""PMT unpacking that survives PMT's own ambiguity.

pmt.is_dict() is true for ANY pair (a PMT dict is an association list), so
`(key . 17.2)` and a PDU `(nil . u8vector)` both claim to be dicts. A real dict
is a list whose elements are themselves pairs."""
import pmt


def is_real_dict(msg):
    return pmt.is_pair(msg) and pmt.is_pair(pmt.car(msg))


def unpack(msg):
    """-> ("dict", {..}) | ("value", python_value) where a (key . value) pair or a
    PDU yields its value, and a u8vector is decoded as text."""
    if is_real_dict(msg):
        return "dict", pmt.to_python(msg)
    if pmt.is_pair(msg):
        msg = pmt.cdr(msg)
    if pmt.is_u8vector(msg):
        return "value", bytes(pmt.u8vector_elements(msg)).decode("utf-8", "replace")
    return "value", pmt.to_python(msg)
