# SPDX-License-Identifier: GPL-3.0-or-later
"""Sharing one radio politely. The library never assumes it is alone: every
SDR touch happens inside a DeviceLock. The default does nothing; a site with
several programs competing for one radio plugs its own lock in from outside
this repository (see ModuleLock)."""
from __future__ import annotations

import importlib.util
import os
from typing import Optional, Protocol


class Yielded(RuntimeError):
    """A higher-priority user asked for the radio; the run stopped cleanly."""


class DeviceLock(Protocol):
    def __enter__(self): ...
    def __exit__(self, *exc): ...
    def heartbeat(self) -> None: ...
    def should_yield(self) -> Optional[str]: ...


class NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def heartbeat(self) -> None:
        pass

    def should_yield(self) -> Optional[str]:
        return None


class ModuleLock:
    """Adapter for a site-local cooperative lock module exposing
    acquire(owner, purpose, priority, wait_s) -> bool, heartbeat(),
    release(owner), and optionally should_yield() / stop_requested(owner).
    The module's path is configuration, not part of this repository."""

    def __init__(self, path: str, owner: str = "rxtune", purpose: str = "receiver tuning",
                 priority: int = 50, wait_s: float = 10.0):
        spec = importlib.util.spec_from_file_location("rxtune_site_lock", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load lock module {path}")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.owner, self.purpose, self.priority, self.wait_s = owner, purpose, priority, wait_s
        self.held = False

    def __enter__(self):
        if not self.mod.acquire(self.owner, self.purpose, self.priority, wait_s=self.wait_s):
            holder = getattr(self.mod, "status", lambda: "?")()
            raise Yielded(f"radio is busy and did not free up in {self.wait_s:g}s: {holder}")
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            self.mod.release(self.owner)
            self.held = False
        return False

    def heartbeat(self) -> None:
        if self.held:
            self.mod.heartbeat()

    def should_yield(self) -> Optional[str]:
        stop = getattr(self.mod, "stop_requested", None)
        if stop and stop(self.owner):
            return "stop requested"
        fn = getattr(self.mod, "should_yield", None)
        return fn() if fn else None


def from_env(owner: str = "rxtune", priority: int = 50) -> DeviceLock:
    """RXTUNE_LOCK=/path/to/lock_module.py selects a site lock."""
    path = os.environ.get("RXTUNE_LOCK")
    return ModuleLock(path, owner=owner, priority=priority) if path else NullLock()
