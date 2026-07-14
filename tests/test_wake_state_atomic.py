"""wake_state atomicity + lock tests: _save is atomic (temp + os.replace), the
sibling .lock exists, and concurrent bump_wait_count under the flock never loses
an update (serialised RMW). Also lie_down --next-wake-min is required at the CLI."""
from __future__ import annotations

import threading

import pytest

from cortex import config, lie_down, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    return c


def test_save_is_atomic_no_tmp_left(cfg):
    wake_state.update(cfg, awake=True, wait_count=3)
    p = wake_state.wake_state_path(cfg)
    assert p.exists()
    # No stray temp files from the atomic replace.
    leftovers = list(p.parent.glob("*.tmp.*"))
    assert leftovers == []


def test_lock_file_path_is_sibling(cfg):
    lp = wake_state.lock_path(cfg)
    assert lp == wake_state.wake_state_path(cfg).with_suffix(".lock")


def test_concurrent_bump_no_lost_update(cfg):
    wake_state.set_awake(cfg, 1, None)  # wait_count = 0
    N = 50

    def worker():
        for _ in range(N):
            wake_state.bump_wait_count(cfg)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Every bump under the flock is serialised -> no lost updates.
    assert wake_state.get_wait_count(cfg) == 4 * N


def test_commit_wait_writes_audit_line(cfg):
    """An accepted wait bumps gen (a new cancellation epoch) — it must leave a
    commit_wait audit line (old->new gen) so the bump is visible in forensics,
    mirroring lie_down_claim. A refused wait writes nothing."""
    wake_state.set_awake(cfg, 1, None)  # awake, gen bumped, wait_count 0
    gen_before = wake_state.current_epoch(cfg)[0]
    res = wake_state.commit_wait(cfg, "2099-01-01T00:00:00+00:00", cap=0)
    assert res["ok"] is True
    lines = wake_state.config.wake_audit_log_path(cfg).read_text().splitlines()
    commits = [ln for ln in lines if "\tcommit_wait\t" in ln]
    assert len(commits) == 1
    assert f"gen {gen_before}->{gen_before + 1}" in commits[0]


def test_commit_wait_refused_writes_no_audit(cfg):
    """A refused wait (not awake) does not bump gen -> no commit_wait audit line."""
    wake_state.update(cfg, awake=None)  # not awake
    res = wake_state.commit_wait(cfg, "2099-01-01T00:00:00+00:00", cap=0)
    assert res["ok"] is False
    p = wake_state.config.wake_audit_log_path(cfg)
    lines = p.read_text().splitlines() if p.exists() else []
    assert not any("\tcommit_wait\t" in ln for ln in lines)


def test_sentinel_pid_self_guarded_clear(cfg):
    wake_state.set_sentinel_pid(cfg, 500)
    # Clearing with a mismatched pid is a no-op (a newer arm owns the record).
    wake_state.clear_sentinel_pid(cfg, only_if_pid=999)
    assert wake_state.get_sentinel_pid(cfg) == 500
    # Matching pid clears it.
    wake_state.clear_sentinel_pid(cfg, only_if_pid=500)
    assert wake_state.get_sentinel_pid(cfg) is None


def test_lie_down_cli_requires_next_wake_min(cfg, monkeypatch):
    monkeypatch.setenv("CORTEX_CONFIG", "/no/such/file.toml")
    # argparse required=True -> missing --next-wake-min exits non-zero.
    with pytest.raises(SystemExit) as exc:
        lie_down.main([])
    assert exc.value.code != 0
