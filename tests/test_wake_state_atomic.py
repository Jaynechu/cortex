"""wake_state atomicity + lock tests: _save is atomic (temp + os.replace) and the
sibling .lock exists. Also lie_down --next-wake-min is required at the CLI."""
from __future__ import annotations

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
    wake_state.update(cfg, awake=True, wait_spent=True)
    p = wake_state.wake_state_path(cfg)
    assert p.exists()
    # No stray temp files from the atomic replace.
    leftovers = list(p.parent.glob("*.tmp.*"))
    assert leftovers == []


def test_lock_file_path_is_sibling(cfg):
    lp = wake_state.lock_path(cfg)
    assert lp == wake_state.wake_state_path(cfg).with_suffix(".lock")


# --- night flag lifecycle (P8) -----------------------------------------------

def test_night_flag_survives_wake_cycle(cfg):
    """The night flag persists across set_awake / clear_awake (it is NOT an
    awake-key), so it outlives individual wakes until the morning clear."""
    wake_state.update(cfg, mode="night")
    wake_state.set_awake(cfg, 1, None)  # a wake begins
    assert wake_state.is_night_mode(cfg) is True
    wake_state.clear_awake(cfg)         # the wake ends
    assert wake_state.is_night_mode(cfg) is True  # flag still set


def test_clear_night_mode_returns_true_once(cfg):
    wake_state.update(cfg, mode="night")
    assert wake_state.clear_night_mode(cfg) is True
    assert wake_state.is_night_mode(cfg) is False
    assert wake_state.clear_night_mode(cfg) is False  # no-op second call


def test_lie_down_night_mode_sets_flag_under_lock(cfg):
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, mode="night")
    assert r["mode"] == "night"
    assert wake_state.is_night_mode(cfg) is True


def test_lie_down_night_mode_via_cli(cfg, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: cfg)
    wake_state.set_awake(cfg, 1, None)
    rc = lie_down.main(["--next-wake-min", "150", "--mode", "night"])
    assert rc == 0
    assert wake_state.is_night_mode(cfg) is True


def test_commit_wait_writes_audit_line(cfg):
    """An accepted wait bumps gen (a new cancellation epoch) — it must leave a
    commit_wait audit line (old->new gen) so the bump is visible in forensics,
    mirroring lie_down_claim. A refused wait writes nothing."""
    wake_state.set_awake(cfg, 1, None)  # awake, gen bumped, wait_spent False
    gen_before = wake_state.current_epoch(cfg)[0]
    res = wake_state.commit_wait(cfg, "2099-01-01T00:00:00+00:00")
    assert res["ok"] is True
    lines = wake_state.config.wake_audit_log_path(cfg).read_text().splitlines()
    commits = [ln for ln in lines if "\tcommit_wait\t" in ln]
    assert len(commits) == 1
    assert f"gen {gen_before}->{gen_before + 1}" in commits[0]


def test_commit_wait_refused_writes_no_audit(cfg):
    """A refused wait (not awake) does not bump gen -> no commit_wait audit line."""
    wake_state.update(cfg, awake=None)  # not awake
    res = wake_state.commit_wait(cfg, "2099-01-01T00:00:00+00:00")
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


# --- single-active-window registration handshake (P14 Fix 3) -----------------

def test_start_registration_handshake_marks_pending_and_bumps_gen(cfg):
    gen_before = wake_state.current_epoch(cfg)[0]
    gen, state_id = wake_state.start_registration_handshake(cfg)
    d = wake_state.load(cfg)
    assert d["cortex_registration_pending"] is True
    assert gen == gen_before + 1
    assert d["gen"] == gen and d["state_id"] == state_id


def test_start_registration_handshake_token_matches_current_epoch(cfg):
    token = wake_state.start_registration_handshake(cfg)
    assert wake_state.token_current(cfg, token) is True


def test_registration_pending_survives_until_claimed_elsewhere(cfg):
    """cortex only STARTS the handshake — the claim itself lives in marrow's
    cortex_bridge (marrow can't import cortex); this side just leaves the
    pending flag + token for that claim to consume."""
    wake_state.start_registration_handshake(cfg)
    assert wake_state.load(cfg).get("cortex_registration_pending") is True
    # cortex_claude_sid is untouched by the cortex-side handshake start —
    # only the marrow-side claim ever writes it.
    assert "cortex_claude_sid" not in wake_state.load(cfg)


def test_rotate_retires_registration_immediately(cfg):
    """/ct-clear (lie_down rotate=True) drops cortex_claude_sid the instant the
    rotate claim lands — the old window loses registration before the new one
    ever spawns/claims (lie_down._mark_rotated)."""
    wake_state.update(cfg, cortex_claude_sid="old-sid-1234")
    wake_state.set_awake(cfg, 1, "/x/old-sid-1234.jsonl")
    lie_down.lie_down(cfg, rotate=True, next_wake_min=30)
    assert wake_state.load(cfg).get("cortex_claude_sid") is None


# --- P17 stage-then-promote: pending_claim (/ct-wake) --------------------------
# Opposite direction from start_registration_handshake above: marrow STAGES the
# claim (only it knows the caller's own sid); cmd_wake here is the sole
# promoter/discarder (only it knows the grant-vs-refuse outcome).

def test_promote_pending_claim_writes_registration_and_clears_staging(cfg):
    wake_state.update(cfg, pending_claim={"sid": "newsid", "ts": "2026-01-01T00:00:00+00:00"})
    ok = wake_state.promote_pending_claim(cfg, resident_pid=7777)
    assert ok is True
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "newsid"
    assert d["cortex_resident_pid"] == 7777  # own claude pid recorded
    assert "pending_claim" not in d
    assert "cortex_registered_at" in d


def test_promote_pending_claim_no_staged_claim_is_noop(cfg):
    wake_state.update(cfg, cortex_claude_sid="already-here")
    ok = wake_state.promote_pending_claim(cfg, resident_pid=7777)
    assert ok is False
    assert wake_state.load(cfg)["cortex_claude_sid"] == "already-here"


def test_promote_pending_claim_sid_mismatch_discards_without_promoting(cfg):
    """A staged claim for a DIFFERENT sid than the one the caller expects is
    discarded, not promoted — stale staging from an unrelated window."""
    wake_state.update(cfg, cortex_claude_sid="untouched",
                      pending_claim={"sid": "staged-sid", "ts": "2026-01-01T00:00:00+00:00"})
    ok = wake_state.promote_pending_claim(cfg, resident_pid=7777, sid="different-sid")
    assert ok is False
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "untouched"
    assert "pending_claim" not in d  # still cleared — no further use


def test_discard_pending_claim_drops_staging_leaves_registration(cfg):
    wake_state.update(cfg, cortex_claude_sid="true-resident",
                      pending_claim={"sid": "foreign", "ts": "2026-01-01T00:00:00+00:00"})
    wake_state.discard_pending_claim(cfg)
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "true-resident"
    assert "pending_claim" not in d


def test_discard_pending_claim_no_staged_claim_is_harmless_noop(cfg):
    wake_state.update(cfg, cortex_claude_sid="whatever")
    wake_state.discard_pending_claim(cfg)  # must not raise
    assert wake_state.load(cfg)["cortex_claude_sid"] == "whatever"
