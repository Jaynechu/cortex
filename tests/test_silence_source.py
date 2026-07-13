"""Silence source = last REAL user message (transcript.user_silent_min).

The single silence timer must count from the user's last message only. Assistant
turns, system writes and the ear-delivered injections (wake bell / free-round /
night lines) must NOT reset it — the "永远睡不到alarm一直窜出来" bug family.
"""
from __future__ import annotations

import json
import time

import pytest

from cortex import config, transcript


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


def _write(cfg, entries):
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text("\n".join(json.dumps(e) for e in entries))


def _iso(ago_min):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=ago_min)).isoformat()


def _user(text, ago_min):
    return {"type": "user", "timestamp": _iso(ago_min),
            "message": {"role": "user", "content": text}}


def _assistant(ago_min):
    return {"type": "assistant", "timestamp": _iso(ago_min),
            "message": {"role": "assistant", "content": "reply"}}


def test_last_user_message_drives_silence(cfg):
    _write(cfg, [_user("hey", 30), _user("still here", 10)])
    assert 9.5 < transcript.user_silent_min(cfg) < 10.5


def test_no_user_message_returns_none(cfg):
    _write(cfg, [_assistant(5)])
    assert transcript.last_user_message_mtime(cfg) is None
    assert transcript.user_silent_min(cfg) is None


# --- the three non-user write types must NOT reset the timer ------------------

def test_assistant_turn_does_not_reset(cfg):
    # User spoke 20 min ago; the assistant answered 1 min ago -> silence stays ~20.
    _write(cfg, [_user("q", 20), _assistant(1)])
    assert 19.0 < transcript.user_silent_min(cfg) < 21.0


def test_free_round_injection_does_not_reset(cfg):
    # The free-round line lands down the ear channel as a USER-role turn; it must
    # be ignored so it never resets its own timer.
    _write(cfg, [
        _user("q", 20),
        _user("⏳ [NEW ROUND] 20 min since ... Choose again ...", 1),
    ])
    assert 19.0 < transcript.user_silent_min(cfg) < 21.0


def test_wake_bell_and_night_lines_do_not_reset(cfg):
    _write(cfg, [
        _user("q", 25),
        _user("[CORTEX-WAKE] 14:03", 2),
        _user("⏳ [NIGHT] Night window is open ...", 1),
    ])
    assert 24.0 < transcript.user_silent_min(cfg) < 26.0


def test_tuck_in_marker_line_does_not_reset(cfg):
    _write(cfg, [_user("q", 18), _user("⏳ [TUCK-IN] legacy marker", 1)])
    assert 17.0 < transcript.user_silent_min(cfg) < 19.0


def test_content_block_form_user_message(cfg):
    # Content-block (list) form is concatenated to text and honoured.
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": _iso(8),
        "message": {"role": "user",
                    "content": [{"type": "text", "text": "hi there"}]}}))
    assert 7.5 < transcript.user_silent_min(cfg) < 8.5


def test_tail_read_ignores_head_on_large_file(cfg):
    # A user message far in the past (head) + a recent one (tail): tail-read still
    # finds the recent one; performance stays flat regardless of file size.
    filler = [_assistant(5) for _ in range(4000)]
    _write(cfg, [_user("old", 500)] + filler + [_user("recent", 3)])
    t = time.perf_counter()
    s = transcript.user_silent_min(cfg)
    assert (time.perf_counter() - t) < 0.1  # sub-100ms even on a big file
    assert 2.5 < s < 3.5
