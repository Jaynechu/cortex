"""Config loader: ~/.config/marrow/cortex.toml (override via CORTEX_CONFIG env).

Tolerant: missing file or missing keys fall back to defaults. Never raises
on a missing config file.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "marrow" / "cortex.toml"
DEFAULT_MARROW_DB = Path.home() / ".config" / "marrow" / "marrow.db"
DEFAULT_KNOWLEDGEC_DB = (
    Path.home() / "Library" / "Application Support" / "Knowledge" / "knowledgeC.db"
)
DEFAULT_HANDOFF = Path.home() / ".config" / "marrow" / "cortex" / "handoff.md"
DEFAULT_CORTEX_HOME = Path.home() / ".config" / "marrow" / "cortex"
DEFAULT_NY_DB_PAGES = Path.home() / "Desktop" / "NY" / "db-pages"
DEFAULT_MARROW_REPO = Path.home() / "CC-Lab" / "marrow"
DEFAULT_WAKE_TIMING_LOG = Path.home() / ".config" / "marrow" / "logs" / "wake_timing.log"

# Single source of truth for the machine-line marker family (wake bell /
# free-round / fuse / ctl / slash-command). Referenced by _DEFAULTS below AND
# by transcript._line_markers' fallback so the two can never drift.
DEFAULT_MACHINE_LINE_MARKERS = ["[TUCK-IN]", "[NEW ROUND]",
                                "[FUSE]", "[CTL]", "[CMD"]

_DEFAULTS: dict[str, Any] = {
    # shells = the shell ids running as cortex shells (mirror of marrow
    # [cortex].shells). "cli" absent = this repo's heartbeat stays down.
    "core": {"timezone": "Australia/Melbourne", "shells": ["cli"]},
    "paths": {
        "marrow_db": "",
        "knowledgec_db": "",
        "geofence_file": "",
        "health_export": "",
        "affect_flag_file": "",
        "self_schedule_file": "",
        "handoff_file": "",
        "cortex_home": "",
        "wishlist_file": "",
        "ny_db_pages": "",
        "wake_timing_log": "",
        "wake_audit_log": "",
    },
    # Per-wake safety valve: cap tokens spent in one wake; breach or the marrow
    # wall-clock timeout (marrow.call_timeout_s) forces a fresh session next wake.
    # signal_log = the ear's tail-followed wake signal file (alive-resident wake);
    # ear_timeout_sec = how long the pacemaker waits for an alive-window wake to
    # land before respawning fresh; wake_prompt = the first prompt baked into a
    # freshly spawned window — JUST an emoji so nothing readable shows in the
    # user's face; the full wake instructions are injected by marrow's
    # UserPromptSubmit hook when this exact emoji is submitted in a cortex
    # window (the note path itself is read from config, not this prompt);
    # say_sound = the sound say() plays when it fronts the window.
    "wake": {
        "token_cap": 150_000,
        # Auto-adopt a cortex window the user opened `claude` in herself (in
        # cortex_home) but never registered: the tick reconcile records it as the
        # resident (under the spawn lock) instead of firing a duplicate window.
        "auto_adopt": True,
        "signal_log": "",
        "ear_timeout_sec": 90,
        # Bounded wait after a RESUMED window comes up for the resumed model to
        # take a turn on its own (the CC harness's background-shell notice usually
        # drives it). Minutes-scale (a resume replays a long conversation first),
        # NOT the 90s ear timeout. If no NEW assistant-role line appears in the
        # resumed transcript within this window, cortex types one ordinary bell
        # line so the resumed window still gets its wake note (Fix 3).
        "resume_turn_timeout_sec": 180,
        "wake_prompt": "☀️",
        # Visible bell line = human text ONLY (no machine marker, no epoch token).
        # The machine data (marker/gen/state_id/rearm) is written to the
        # wake_state receipt sidecar instead (window.write_wake_receipt); the
        # marrow hook matches the on-screen line against that receipt. {hm} =
        # local HH:MM. This static PREFIX (text before {hm}) is persisted into
        # wake_state so the consumer can shape-match it without cortex config.
        "wake_bell_template": "☀️ {hm}",
        # Receipt time-to-live (minutes): the consumer ignores a pending receipt
        # older than this, and the producer overwrites it on every new bell.
        "receipt_ttl_min": 15,
        "rearm_suffix": " (ear died — rearm)",
        "say_sound": "Glass",
        # lie_down(next_wake_min=N) clamp (minutes): [0, next_wake_max], every
        # hour (0 = immediate re-wake).
        "next_wake_max": 240,
        # Exact-time wake: arm cortex.sentinel (one-shot detached sleep-then-tick)
        # at every lie_down. false = tick-only (launchd 5-min fallback).
        "sentinel": True,
        # Line written to wake_signal.log at every free-round injection (silence
        # cycle or kick carrier): the free-round note above it, this line last as
        # the final cue. MUST contain the tuck_in_marker family string
        # ("[NEW ROUND]") — that machine-line detection is what keeps the line
        # from resetting the silence timer; without it every injection would
        # re-arm its own cycle (perpetual loop trap). {mins}/{user} optional
        # placeholders; {user} = marrow user_name.
        "tuck_in_text": "⏳ [NEW ROUND] Time for a new turn — try something else "
                         "you fancy, go talk to {user} if you miss her, or "
                         "lie_down for a rest.",
        # Markers that identify a NON-user turn (wake bell / free-round / fuse /
        # ctl / slash-command line), so they never reset the silence timer
        # and downstream memory drops them. The bell prefix (lineage_marker) is
        # added automatically. Substring match, so "[CMD" catches every ⚙️ [CMD ct-*].
        "machine_line_markers": list(DEFAULT_MACHINE_LINE_MARKERS),
        # Every free-round injection (silence cycle or kick carrier) appends a
        # freshly rendered wakeup note above the marker line (note content only,
        # no behavioural nudge).
        "free_round_note": True,
        # Covert-delivery markers written to wake_signal.log (bell via the ear
        # Monitor; typed only if the ear is dead). Only the marker line reaches
        # the window; the full instruction body is injected invisibly by the
        # marrow hook keyed on the marker (fuse -> [cortex].fuse_prompt_text;
        # ctl -> [cortex].ctl_sleep_text, rendered from the mins/rotate args the
        # ctl marker line carries).
        "fuse_marker": "⚙️ [FUSE]",
        "ctl_sleep_marker": "⚙️ [CTL]",
        # Refusal returned by lie_down(rotate=True) while THIS window's own
        # wake-signal ear tail is still alive: a live monitor task replays its
        # completion notification when the rotated window resumes. Stop it first,
        # then rotate. Plain (non-rotate) lie_down never refuses.
        "rotate_refuse_text":
            "Rotate refused: your wake-signal monitor is still running. "
            "TaskStop it first, then call lie_down(rotate=true) again.",
    },
    # marrow repo invocation for the wake call (separate venv/deps, C3).
    "marrow": {
        "repo_dir": str(DEFAULT_MARROW_REPO),
        "venv_python": str(DEFAULT_MARROW_REPO / ".venv" / "bin" / "python"),
        # Inner claude-call budget (s), passed down to marrow; the outer
        # subprocess kill = this + margin. Must match marrow's own default.
        "call_timeout_s": 600,
    },
    "knowledgec": {"stream_name": "/app/usage"},
    "knowledgec.categories": {"default": "uncategorized"},
    "geofence": {"enabled": False},
    "health": {"enabled": False},
    # launchd tick cadence (seconds). Baked into plists at install time.
    "tick": {
        "collect_interval_sec": 1800,
        "pacemaker_interval_sec": 60,
        # OAuth usage % snapshot (marrow subprocess) each collect tick.
        "usage_snapshot": True,
    },
    # Pacemaker integration knobs (numbers the integration layer computes with).
    "pacemaker": {
        "dry_run": True,
        "active_window_min": 5,
        "at_home_default": True,
        "cal_busy_default": False,
    },
    "gates": {
        # Daily wake-token budget: once Cortex Today (sum of today's finished-
        # window final context occupancies + the current live window occupancy)
        # reaches this, self-wakes stop; resets at local midnight.
        "daily_budget": {"tokens": 1_000_000},
    },
    "triggers": {
        # Wake-window draw (minutes) from lie-down. lie_down picks the next wake:
        # an explicit choice clamped to [min, max] (max = cache-TTL guard, min =
        # anti-thrash), or a uniform "dice" draw within the window when omitted.
        # Also the clamp for a model-declared watchdog silence window.
        "floor_min_min": 10,
        "floor_max_min": 55,
    },
    # External-wake (cortex.kick) reason lines rendered as plain lines into the
    # wakeup note (no section header), then cleared on delivery. A bridge/cli
    # poke appends one; note.py renders + consumes it. {id} = outbox note id;
    # {text} = her reply body (truncated by the bridge); {minutes} = silence min.
    "kick": {
        "reason_reply": 'Msg #{id} replied: "{text}"',
        "reason_timeout": "Msg #{id} no reply in {minutes}min",
        "reason_morning": "She's up — day mode",
        "reason_note": "New note #{id}",
        # Cap the pending-flag list so a stuck bridge can't grow it unbounded.
        "max_reasons": 8,
    },
    # Wakeup note knobs. Every field is deterministic now, so the old whole-note
    # max_chars cap is gone; per-source limits below keep each line bounded.
    # OSS: identity/display strings stay in config, never hardcoded in .py.
    "note": {
        # Machine-origin tag prepended as the VERY FIRST line of every wakeup
        # note (before title), so the model treats the ☀️ bell/wake-prompt turn
        # that carried this note as an automated scheduler signal, NOT a message
        # the user personally typed (live-observed: cortex replied to the bell as
        # if the user spoke it, Fix 5). Note-copy only -- does NOT change the bell
        # text or receipt matching. "" omits it.
        "wake_machine_tag":
            "[AUTOMATED WAKE SIGNAL — this note and the ☀️ line that delivered it "
            "come from the scheduler, not from the user. The user has NOT spoken; "
            "do not reply as if answering them.]",
        # Optional first line of the wakeup note (e.g. a nickname for the
        # note), followed by a blank line then the usual content. "" omits it.
        "title": "",
        # Trailing conversation events force-appended to the Replay block
        # (cross-session, uniform, no decay). 4 = two round-trips.
        "replay_events": 4,
        # Per-event truncation inside the Replay block.
        "replay_event_chars": 300,
        # Daily wake-token (NET spend) budget the "Cortex Today X/Y" segment
        # renders against — must match gates.daily_budget.tokens (display=gate).
        "daily_budget": 1_000_000,
        # Pending self-schedule entries surface only when due within this window.
        "pending_window_min": 15,
        # Prior window force-slept without a handoff -> backfill hint line.
        "force_slept_catchup_text":
            "Prior window was force-slept — catchup by recall all events from DB "
            "(do not read raw jsonl) and append to handoff.md",
        # Prior window DIED (crash/manual close) mid-wake without writing its
        # handoff -> the fresh respawn recovers context from its transcript.
        "died_no_handoff_catchup_text":
            "Previous window died without a handoff — recover context from its "
            "transcript, then write the handoff.",
        # Reply-receipt line (C11): one per sent note she has replied to since the
        # last note. {id}/{channel}/{sent_hm}/{replied_hm}/{text} render from the
        # marrow outbox row at note time. "" omits receipts entirely.
        "receipt_line": 'Note #{id} ({channel} {sent_hm}): she replied {replied_hm} "{text}"',
        # One-line turn-end reminder appended at the very end of every rendered
        # note. "" omits it (default: the silence cycle + free-round injection
        # already carries this information, no reminder needed).
        "turn_end_text": "",
        # Header written into a freshly-created wishlist.md (append-only file,
        # never overwritten). Display text — customise freely.
        "wishlist_header":
            "# Wishlist\n\n(owed treats / wants / self-rewards — append-only)\n",
    },
    # Cross-channel note delivery into the cortex window. Header for a ct-targeted
    # outbox note rendered by the wakeup note (mirror of marrow [outbox].inject_header
    # — must stay in sync so hook-delivered and note-delivered notes read the same).
    # {channel}/{sid4}/{time} render from the outbox row. "" = body only.
    "outbox": {
        "inject_header": "📮 Message from {channel}·{sid4} {time}",
    },
}

_SECTIONS = (
    "core", "paths", "knowledgec", "geofence", "health",
    "tick", "pacemaker", "gates", "triggers", "marrow",
    "wake", "note", "kick", "outbox",
)


def shell_enabled(cfg: dict, shell: str = "cli") -> bool:
    """Is `shell` listed in [core].shells? The cli heartbeat entries exit early
    when its own shell is switched off."""
    raw = cfg.get("core", {}).get("shells")
    if not isinstance(raw, list):
        raw = ["cli"]
    return shell.strip().lower() in [str(s).strip().lower() for s in raw]


def wake_clamps(cfg: dict) -> dict[str, int]:
    """The wake-clamp numbers rendered into note/tool text (never hardcoded).
    lie_down bounds [0, next_wake_max] from [wake]; idle bar from
    [wake.watchdog].silent_max_min."""
    w = cfg.get("wake", {})
    wd = w.get("watchdog", {})
    return {
        "next_wake_min": 0,
        "next_wake_max": int(w.get("next_wake_max", 240)),
        "silent_max_min": int(wd.get("silent_max_min", 20)),
    }


def _config_path() -> Path:
    override = os.environ.get("CORTEX_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _merge(defaults: dict, loaded: dict) -> dict:
    merged = dict(defaults)
    for key, val in loaded.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load(path: Path | None = None) -> dict[str, Any]:
    """Load cortex config with tolerant fallback to defaults."""
    cfg_path = path or _config_path()
    loaded: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as f:
            loaded = tomllib.load(f)

    cfg = {section: copy.deepcopy(_DEFAULTS[section]) for section in _SECTIONS}
    for section in _SECTIONS:
        if section in loaded:
            cfg[section] = _merge(cfg[section], loaded[section])

    # Legacy fallback: an old config.toml may still carry the pre-rename
    # [bulletin] section (renamed to [note]) — merge it in if present.
    if "bulletin" in loaded:
        cfg["note"] = _merge(cfg["note"], loaded["bulletin"])

    categories = dict(_DEFAULTS["knowledgec.categories"])
    loaded_categories = loaded.get("knowledgec", {}).get("categories", {})
    categories.update(loaded_categories)
    cfg["knowledgec"]["categories"] = categories

    return cfg


def marrow_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("marrow_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_MARROW_DB


def user_name(cfg: dict, default: str = "the user") -> str:
    """The user's name, read from marrow's config.toml (sibling of cortex.toml in
    the shared config dir) — cortex inherits it, never carries its own copy (OSS:
    no hardcoded persona in code). `default` when marrow config is absent/blank."""
    db = marrow_db_path(cfg)
    marrow_cfg = db.parent / "config.toml"
    try:
        if marrow_cfg.exists():
            with marrow_cfg.open("rb") as f:
                data = tomllib.load(f)
            name = str(data.get("persona", {}).get("user_name") or "").strip()
            if not name:
                name = str(data.get("user_name") or "").strip()  # legacy top-level layout
            if name:
                return name
    except (OSError, ValueError):
        pass
    return default


def knowledgec_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("knowledgec_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_KNOWLEDGEC_DB


def geofence_file_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("geofence_file") or ""
    return Path(raw).expanduser() if raw else None


def health_export_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("health_export") or ""
    return Path(raw).expanduser() if raw else None


def affect_flag_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("affect_flag_file") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "affect_flag.json"


def self_schedule_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("self_schedule_file") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "self_schedule.json"


def handoff_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("handoff_file") or ""
    return Path(raw).expanduser() if raw else DEFAULT_HANDOFF


def cortex_home(cfg: dict) -> Path:
    """cwd for the resumed full-env marrow cortex session (Decided 07-03 pm)."""
    raw = cfg["paths"].get("cortex_home") or ""
    return Path(raw).expanduser() if raw else DEFAULT_CORTEX_HOME


def state_dir(cfg: dict) -> Path:
    """Runtime state subdirectory (json/log/lock/pid files). Lives under
    cortex_home/state/ to keep runtime artefacts out of the notebook root."""
    d = cortex_home(cfg) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def wishlist_path(cfg: dict) -> Path:
    """Pure append-only md, fixed path (Decided 07-03 eve). Mirrors marrow's
    own [cortex].wishlist_path default ('' -> <home>/wishlist.md)."""
    raw = cfg["paths"].get("wishlist_file") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "wishlist.md"


def ny_db_pages_dir(cfg: dict) -> Path:
    raw = cfg["paths"].get("ny_db_pages") or ""
    return Path(raw).expanduser() if raw else DEFAULT_NY_DB_PAGES


def wishlist_header(cfg: dict) -> str:
    """Header written into a freshly-created wishlist.md. Display text; config-driven."""
    return cfg.get("note", {}).get("wishlist_header") or _DEFAULTS["note"]["wishlist_header"]


def wake_timing_log_path(cfg: dict) -> Path:
    """Shared wake-latency probe log (cortex marks + marrow stream-event marks).
    Default: ~/.config/marrow/logs/wake_timing.log."""
    raw = cfg["paths"].get("wake_timing_log") or ""
    return Path(raw).expanduser() if raw else DEFAULT_WAKE_TIMING_LOG


def wake_signal_log_path(cfg: dict) -> Path:
    """The ear's wake-signal file: the persistent Monitor `tail -f`s it, the
    pacemaker appends WAKE/NUDGE lines to it (alive-resident wake only).
    Default: <cortex_home>/state/wake_signal.log."""
    raw = cfg["wake"].get("signal_log") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "wake_signal.log"


def wake_audit_log_path(cfg: dict) -> Path:
    """Wake-state audit trail (alarm epoch/generation events). Byte-shared with
    marrow's [cortex].wake_audit_log_file so both sides append to one file.
    Default: <cortex_home>/state/wake_audit.log. Override via [paths].wake_audit_log."""
    raw = cfg["paths"].get("wake_audit_log") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "wake_audit.log"
