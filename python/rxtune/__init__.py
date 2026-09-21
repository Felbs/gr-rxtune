#
# Copyright 2026 gr-rxtune authors.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
'''
gr-rxtune: GNU Radio blocks for the rxtune receiver tuning loop.

The blocks are thin. The search, the curve-shape classifier and the verdict
live in the pure-Python `rxtune` package, which has no GNU Radio dependency.
'''
try:
    # python-only module: there are no compiled bindings
    from .rxtune_python import *
except ModuleNotFoundError:
    pass

from .controller import controller
from .dial_probe import dial_probe
from .dial_from_tag import dial_from_tag
from .dial_adapter import dial_adapter
from .msg_setter import msg_setter

def __getattr__(name):
    # Qt is optional and LAZY: the controller must run headless (a Pi with no
    # display, or no PyQt at all), so nothing here imports Qt until the
    # dashboard is actually asked for.
    if name == "dashboard":
        from .dashboard import dashboard
        # importing the submodule binds the MODULE to this name; rebind the class,
        # as the eager `from .x import x` lines above do for the other blocks
        globals()["dashboard"] = dashboard
        return dashboard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
