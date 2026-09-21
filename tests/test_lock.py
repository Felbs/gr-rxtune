# SPDX-License-Identifier: GPL-3.0-or-later
"""The site-lock adapter."""
from rxtune.lock import ModuleLock

SITE_LOCK = '''
log = []
def acquire(owner, purpose, priority, wait_s=0.0):
    log.append("acquire")
    return True
def release(owner=None):
    log.append("release")
def heartbeat():
    log.append("beat")
'''


def test_site_lock_is_reentrant(tmp_path):
    """Found on hardware: a script held the lock, passed it to tune(), and tune()'s exit
    released the radio while the script was still streaming from it."""
    mod = tmp_path / "site_lock.py"
    mod.write_text(SITE_LOCK)
    lk = ModuleLock(str(mod))
    with lk:
        with lk:
            lk.heartbeat()
        assert lk.mod.log == ["acquire", "beat"], "the inner exit must not release"
        lk.heartbeat()
    assert lk.mod.log == ["acquire", "beat", "beat", "release"]
