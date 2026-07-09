2026-07-10

# Cortex — MAP

> How each part works today. Not SoT — code wins. Refs `file:line` (package files under cortex/).
> Goals → DESIGN.md. Plan → CC-Lab/docs/plans/ct-plan.md.

## 1. Architecture

```
collectors (launchd 1800s) ──▶ ct_ tables (marrow.db)
                                    │
pacemaker (launchd 300s) ──tick()──▶ decision ──▶ wake.run_wake
                             ct_pacemaker_state       │
                             ct_wake_log        note → iTerm window (resident claude) | marrow subprocess fallback
                                                      │
                                                day_log.md (symlinked → NY) · watchdog (per-wake)
```

- Own repo/venv `~/CC-Lab/cortex/`; ct_ tables live on shared marrow DB (`~/.config/marrow/marrow.db`).
- DB contract: journal_mode owned by marrow (DELETE, marrow/storage.py:399); cortex sets busy_timeout=30000 only (db.py:100). Comment-only contract, no runtime assert.
- migrate() (8 CREATE IF NOT EXISTS + 3 guarded ALTERs on ct_wake_log: tokens/force_slept/net_tokens) runs on every connect; each tick process connects once (db.py:98-129).
- Config: TOML `~/.config/marrow/cortex.toml` (env CORTEX_CONFIG), tolerant deep-merge over _DEFAULTS; legacy [bulletin]→note; [[schedule]] passed raw (config.py:165-193).

## 2. Collectors (`collect_tick.py`, `collectors/`)

- Entry collect_tick.py:48-59 (launchd com.cortex.collect-tick): run_all → _run_usage_snapshot → _render_day_log → exit 1 if any run_all source failed (usage not in exit code).
- run_all: registry {knowledgec, geofence, health}, per-source try/except, result → ct_collector_log (collectors/__init__.py:15-34). day_log Status renders latest row per source, so failures are visible there.
- knowledgec (always on): read-only URI open of macOS knowledgeC.db, full re-scan of ZOBJECT stream '/app/usage' per tick (indexed, OS retention-capped ~3wk), aggregate per (local_date, bundle_id) + category map → upsert ct_app_usage/ct_category_usage; aged-out dates freeze at last-seen totals (knowledgec.py:25-96).
- geofence (default off): byte-offset cursor in ct_geofence_cursor, truncation resets to 0; parses `HH:MM event` complete lines only; date stamped = today-at-tick (assumes near-zero Shortcut latency); PK (date,time,event) ON CONFLICT DO NOTHING (geofence.py:28-111).
- health (default off, skeleton): full-file JSON flatten to dot-path rows, date from payload date/export_date/day else file mtime; upsert ct_health; no consumer reads ct_health yet (health.py:21-66).
- usage_snapshot: marrow venv subprocess `-m marrow.usage_snapshot`, 15s timeout, gated tick.usage_snapshot; redundant with marrow watcher's own 5-min loop (collect_tick.py:16-35).
- activity.py: read-only helper over ct_activity (written by marrow Stop hook). read_activity() has zero production callers; its LIKE date filter is the pattern day_log.py:76 deliberately rejects.

## 3. Pacemaker (`pacemaker/`)

- core.tick(state, context, config, now, rng) pure — no I/O/wall-clock (core.py:1-8). PacemakerState frozen: next_floor_due_at, last_wake_at, last_lie_down_at, night_cap_key/count, cortex_session_id/date (core.py:19-32). Desire/expect_reply retired; legacy keys dropped on load (integration.py:61-90).
- Triggers (triggers.py): kinds event (unwired, always []) · affect_flag · self_scheduled (due_at <= now) · schedule · floor (due when now >= next_floor_due_at). Facts only, no motive strings. Collision: any real reason silences plain floor for that tick (triggers.py:81-108). reschedule_floor = uniform draw [floor_min_min, floor_max_min] (10/55) or explicit clamped minutes (triggers.py:111-131).
- Floor redraw on fire happens BEFORE gates, from tick time; real wakes get re-anchored to lie-down time later by integration.lie_down — gated ticks correctly keep the tick-time anchor (core.py:56-59, verified intended).
- Gates (gates.py): night window (wrap-capable, night_key = ISO date window started) cap night_wake_count vs gates.night.cap; daily_budget vs context today_tokens (1M default, <=0 disables). PIERCE_KINDS = {schedule} only — event/affect_flag/self_scheduled/floor all gated. run_gates evaluates all gates, no short-circuit; wake = reasons and not gated_by (gates.py:26-111, core.py:65-67).
- NB "pierce" is overloaded: triggers.py:96 local `pierce` = real-reason set (4 kinds) silencing floor; gates.PIERCE_KINDS = gate bypass (1 kind). Two unrelated concepts.
- integration.py = sole I/O owner. State = single-row JSON ct_pacemaker_state id=1; side-channel keys window_tokens (store_window_tokens) + schedule_fired {duty: local_date} survive independently of dataclass saves (integration.py:61-142).
- build_context: active_session = ct_activity within 5min; cal_busy/at_home = config defaults (unwired); affect_flag + self_schedule from JSON files; schedule = due_duties(entries, now, fired); today_tokens = SUM COALESCE(net_tokens,tokens) local-day from ct_wake_log; events [] (integration.py:164-238).
- run_tick: load → build_context → tick → save_state + write_wake_log (every tick, incl dry_run) → decision (integration.py:278-292).
- schedule.due_duties pure: skips disabled/malformed/future/already-fired-today; a passed duty stays due till fired recorded or midnight (schedule.py:22-43).
- Entry pacemaker_tick.py:70-104: _night_close (inject wrap-up prompt once/night if awake, else set_rotated once/night) runs BEFORE awake-guard; _handle_awake reaps stale wake (transcript idle >= 15min → proxy lie_down(stale)) else skips tick; wake fired: dry_run → integration.lie_down only (run_wake never called → schedule duties never marked fired under dry_run — known bug); live → wake.run_wake, then integration.lie_down only for headless mode ('schedule'/'window' own their lie_down).

## 4. Wake runner (`wake.py`)

- run_wake (wake.py:281-382): schedule wakes short-circuit before resident/day_log; then daily rebirth (session_date≠today → archive intent, fresh session), symlinks.ensure_all, assemble_note, window path (cfg wake.mode='window' default AND real caller) else marrow subprocess.
- Schedule path _schedule_wake (wake.py:155-187): per duty spawn_fresh (never resident, sid unpersisted) → inject_note(note + duty prompt_path) → say() ping → mark_schedule_fired; window failure audited + skipped.
- Window path _window_wake (wake.py:240-268): write note file → respawn if _window_rotated (rotate flag | sid dead | claude pid gone | newest transcript ≠ recorded) → append WAKE line to signal log (resident's armed Monitor tails it) → _signal_landed polls transcript mtime 3s up to ear_timeout 90s → on miss: respawn + re-append once → set_awake(wake_log_id, transcript) + watchdog.spawn. Rotate assembles a second fresh note (wake_kind='rotate' → 碎碎念).
- Headless fallback: window path returning None falls through to call_marrow_cortex — marrow venv subprocess, inner timeout marrow.call_timeout_s 600s, outer +30s margin, CORTEX_WAKE_ID/TIMING_LOG via env; non-zero rc or bad JSON tail → WakeError (wake.py:71-99, 349-350).
- Token-cap breach (result.capped) → _force_fresh_next (clear sid keep date) + audit + day_log.update, no sid persist (wake.py:362-370).
- _audit_wake best-effort inserts audit_log rows on shared DB, swallows all (wake.py:102-112). CLI: --force (bypass gates) | --print-note (wake.py:385-409).

### window.py — iTerm control

- Focus discipline: say() is the sole allowed focus-taker (sound + bring-to-front, window.py:476-483). All typing paths wrap _frontmost_bid/_guard_focus (conditional: restores only if iTerm actually stole front, window.py:67-73). _spawn has internal save/restore (window.py:167-185). _relaunch (window.py:210-216) has no own guard but is only reachable via inject_note's guarded frame, and is unreached in production (sole caller passes explicit sid).
- launch_command: `cd <cortex_home> && MARROW_CORTEX=1 MARROW_CHANNEL=ct claude --model <wake.window_model, default opus> [--effort] [--dangerously-skip-permissions, default true] [arm prompt]` (window.py:119-138). arm prompt from deploy/prompts/arm.md with {signal_log} substituted: arm a persistent Monitor tailing the signal log, lie_down, stay silent (window.py:106-116).
- _wait_ready polls session text for wake.ready_marker ('accept edits') up to 30s (window.py:284-293).
- respawn = fresh-brain path (rotate/rebirth/ear-recovery): SIGTERM old claude, close session, _spawn, persist sid (window.py:236-254).
- find_claude_pid: session tty → ps exact-match, fallback pgrep -x + cwd filter; 0 or >1 candidates → None, never guess (window.py:388-459). hard_interrupt = SIGINT on discovered pid only (window.py:462-473).

### wake_state.json (`wake_state.py`)

- Keys: awake/awake_since/wake_log_id/transcript (cleared as a set), session id, wait_count (reset on set_awake), silence_wait_until (one-shot), rotated (read-and-clear via take_rotated).
- load tolerates missing/corrupt → {}; _save/update = whole-file read-modify-write, no lock, no atomic rename (wake_state.py:34-54 — confirmed cross-process lost-update window, see priority queue).

### watchdog (`watchdog.py`)

- Per-wake detached subprocess spawned at set_awake; pidfile self-guarded (only unlinks own pid) (watchdog.py:29-40, 169-181).
- Poll loop 60s: retires when awake cleared externally; publishes NET tokens via store_window_tokens each poll (own short DB conn); fuse: window_tokens >= wake.watchdog.fuse_tokens (150k) → _fuse then exit; timeout: silent >= silent_max_min (10) honoring one-shot wait_until extension → proxy lie_down(timeout) (watchdog.py:126-166).
- _fuse: esc → inject fuse_handoff_prompt (summarize + append handoff + lie_down(rotate=True)) → poll awake up to 300s grace; only proxy-lie_down if session didn't do it itself; catchup reason only when handoff NOT written (watchdog.py:76-109). esc verify: transcript still growing after grace → hard_interrupt SIGINT, gated wake.watchdog.hard_interrupt_enabled (watchdog.py:43-66).

## 5. lie_down / wait / say

- Exposed as env-gated MCP tools in marrow daemon (MARROW_CORTEX=1), subprocess `-m cortex.<mod>`; also CLI mains for watchdog proxy use.
- lie_down (lie_down.py:86-111): record tokens+net into ct_wake_log (sole writer of those columns; bare `except: pass` — known silent-drop finding) → clear due self_schedule entries → integration.lie_down floor redraw from now (explicit next_wake_min clamped, or dice) → publish net via store_window_tokens → kill watchdog (SIGTERM, skip if self) → optional set_rotated → clear_awake.
- wait (wait.py:23-35): one-shot watchdog silence extension; cap wake.wait_max_per_wake (2) per wake; minutes clamped via triggers floor_min_min/floor_max_min (10/55 defaults — shared with the wake-window bounds, retuning one retunes both); writes deadline + bumps counter (two separate RMW writes).
- say (say.py, window.py:476-483): sound + front resident window; --note flag accepted but ignored (CLI symmetry only).

## 6. Wakeup note (`note.py`)

- gather (note.py:311-340): every section behind _safe() — assembly never crashes. render pure, sections omit cleanly when absent (note.py:386-446).
- Sections: header Wake/Now/Plan Used/Active [+ force_slept_catchup_text] · Pending self-schedule (due within note.pending_window_min 15) · Replay (last note.replay_events 4 user/assistant events, excl channels ('ct',), marker-stripped, 300ch each) · note.turn_end_text trailer · note.title prefix.
- Budget line (note.py:449-475): `Plan Used: 5h X% | 7d Y% | Cortex Today Nk/Mk | Net Session Token: Wk` — 5h/7d from ct_rate_limit kv (marrow usage_snapshot); Cortex Today = ct_wake_log local-day sum (dup of integration._today_tokens, must stay in sync — note.py:132); Net Session = window_tokens key from ct_pacemaker_state (published by watchdog/lie_down).
- _last_wake skips rows younger than 90s to avoid self-reporting the current wake (note.py:106-127).
- _strip_markers = deliberate local copy of marrow strip_media_markers (marrow not importable from cortex venv), regexes byte-identical today (note.py:189-204 vs marrow/transcript.py:97-117) — drift risk tracked in priority queue.
- Handoff (碎碎念) injection happens at marrow SessionStart, not note.py; cal/rem lines retired pending global inject (note.py:9-10).

## 7. transcript.py — token/liveness probe

- _munge replicates CC cwd→projects dirname; transcript_dir overridable via paths.transcript_dir (transcript.py:14-26). newest() = latest-mtime top-level *.jsonl (subagent files live in subagents/ subdir, naturally excluded).
- window_tokens = LAST usage line's input+cache_read+cache_creation+output → current context occupancy; drives rotate/fuse (transcript.py:42-64).
- net_tokens = SUM of cache_creation+output across session → real spend; drives budget gate + note (transcript.py:67-91).
- Both return 0 silently on read errors; mtime() drives rotation detection + ear polling.

## 8. day_log.md (`day_log.py`)

- Six zones by HTML-comment markers: First + Notes preserved byte-for-byte each re-render; Status/Flow/Tasks(Reminders)/Track fully rebuilt from DB, no reconcile (day_log.py:37-58, 219-253).
- Status: last-seen (ct_activity via _utc_day_bounds — UTC-correct local day), top usage category, collector health lines (day_log.py:86-132).
- Flow: geofence rows (exact local-date match) + tl rows (events role='tl', UTC bounds), merged sorted HH:mm (day_log.py:140-176).
- Reminders/Track: placeholders by design (day_log.py:179-191).
- update() = read-existing → render → bare write_text (non-atomic — torn-read window, file is iCloud-symlinked; finding). Callers: collect_tick.py:45, wake.py:367/379.
- new_day()/archive() unwired (zero callers). new_day overwrites unconditionally (docstring-only contract — finding); archive() protects via FileNotFoundError + -N suffix (day_log.py:297-314).

## 9. Symlinks, install, deploy

- symlinks.py: day_log.md + wishlist.md → NY db-pages; creates wishlist w/ hardcoded WISHLIST_HEADER (persona text in .py — finding); refuses non-symlink clobber (FileExistsError propagates); ensure_all safe per-wake (symlinks.py:11-42).
- install.py: `python -m cortex.install [remove]` — writes 2 plists (collect-tick, pacemaker-tick) with 6 __TOKEN__ replacements from config, launchctl bootout+bootstrap into gui/<uid>; no rollback on partial failure (self-healing on re-run); zero test coverage (install.py:16-97).
- No pyproject.toml — package importable only because plists set WorkingDirectory=repo root (`-m` resolves cortex/ from cwd); any other cwd needs PYTHONPATH.
- Plists: RunAtLoad + StartInterval, no KeepAlive/backoff — a crashing tick just re-fires next interval.
- Safety default: pacemaker.dry_run=true shipped in example config; live config also true today.

## 10. Tests

- 24 per-module test files under tests/; pure cores (pacemaker, day_log render, note, geofence cursor) well covered.
- Known gaps: install.py (untested entirely), dry_run schedule-dedup path, geofence same-minute-same-text dup, day_log concurrent write.

## 11. Status

- Live: collectors (knowledgec) · pacemaker ticks (dry_run) · wake window path + watchdog + fuse · note · day_log render · MCP lie_down/wait/say · symlinks.
- Unwired: event triggers · cal_busy/at_home real data · Tasks/Track zones · day rollover (new_day/archive callers) · health/geofence collectors (flagged off, no export producer).
- Duties ([[schedule]]): mechanism shipped, both live duties enabled=false pending prompts.
