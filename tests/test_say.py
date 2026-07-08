from cortex import config, say, window


def test_say_cli_invokes_window_say(monkeypatch):
    called = {}
    monkeypatch.setattr(window, "say", lambda cfg, note=None: called.update(note=note, hit=True))
    assert say.main([]) == 0
    assert called.get("hit") is True
    assert called.get("note") is None


def test_say_cli_passes_note(monkeypatch):
    called = {}
    monkeypatch.setattr(window, "say", lambda cfg, note=None: called.update(note=note))
    assert say.main(["--note", "come here"]) == 0
    assert called["note"] == "come here"


# --- say = front the window + play a sound (the only focus-taking path) --------

def _cfg(tmp_path):
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "home")
    return c


def test_say_fronts_window_and_plays_sound(tmp_path, monkeypatch):
    """say() plays the configured sound and fronts the resident window — and no
    longer posts a display-notification (that path is deleted)."""
    cfg = _cfg(tmp_path)
    cfg["wake"]["say_sound"] = "Glass"
    calls = {}
    monkeypatch.setattr(window.wake_state, "get_session_id", lambda c: "SID-1")
    monkeypatch.setattr(window, "_play_sound", lambda name: calls.update(sound=name))
    monkeypatch.setattr(window, "_bring_to_front", lambda sid: calls.update(front=sid))

    window.say(cfg)
    assert calls == {"sound": "Glass", "front": "SID-1"}


def test_play_sound_uses_afplay(monkeypatch):
    """_play_sound spawns afplay on the named system sound; empty name = silent."""
    seen = {}
    monkeypatch.setattr(window.subprocess, "Popen",
                        lambda cmd, **kw: seen.update(cmd=cmd))
    window._play_sound("Glass")
    assert seen["cmd"][0] == "afplay"
    assert seen["cmd"][1].endswith("/Glass.aiff")

    seen.clear()
    window._play_sound("")  # empty -> no spawn
    assert seen == {}
