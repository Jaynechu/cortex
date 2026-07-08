from cortex import say, window


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
