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
    lk = ModuleLock(str(mod), beat_s=0.0)
    with lk:
        with lk:
            lk.heartbeat()
        assert lk.mod.log == ["acquire", "beat"], "the inner exit must not release"
        lk.heartbeat()
    assert lk.mod.log == ["acquire", "beat", "beat", "release"]


def test_heartbeats_are_throttled(tmp_path):
    """The Measurer beats every poll (5/s); a site lock that rewrites a file gets one
    write per beat_s."""
    mod = tmp_path / "site_lock.py"
    mod.write_text(SITE_LOCK)
    lk = ModuleLock(str(mod), beat_s=3600.0)
    with lk:
        for _ in range(50):
            lk.heartbeat()
    assert lk.mod.log.count("beat") == 1


STATUS_LOCK = SITE_LOCK + '''
holder = {"owner": "rxtune", "purpose": "receiver tuning"}
def status():
    return holder
'''


def test_lost_lock_is_a_yield(tmp_path):
    """Found on the radio: another process took the site lock while we streamed (the
    warden misread its own file mid-rewrite); our heartbeats then wrote nothing and the
    run went on unprotected.  With status() exposed, the holder notices and stops."""
    mod = tmp_path / "site_lock.py"
    mod.write_text(STATUS_LOCK)
    lk = ModuleLock(str(mod), owner="rxtune")
    with lk:
        assert lk.should_yield() is None, "our own hold is not a reason"
        lk.mod.holder = {"owner": "sonde-hunt", "purpose": "pass"}
        assert "lock lost" in lk.should_yield()
        assert "sonde-hunt" in lk.should_yield()
        lk.mod.holder = None
        assert "lock lost" in lk.should_yield()
    assert lk.should_yield() is None, "not held: nothing to lose"


def test_from_env_wait(monkeypatch, tmp_path):
    from rxtune import lock
    mod = tmp_path / "site_lock.py"
    mod.write_text(SITE_LOCK)
    monkeypatch.setenv("RXTUNE_LOCK", str(mod))
    monkeypatch.setenv("RXTUNE_LOCK_WAIT", "600")
    assert lock.from_env().wait_s == 600.0
    monkeypatch.delenv("RXTUNE_LOCK_WAIT")
    assert lock.from_env().wait_s == 10.0
