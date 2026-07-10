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
                                                daybrief.md (marrow render, symlinked → NY) · watchdog (per-wake)
```

- Own repo/venv `~/CC-Lab/cortex/`; ct_ tables live on shared marrow DB (`~/.config/marrow/marrow.db`).
- DB contract: journal_mode owned by marrow (DELETE, marrow/storage.py:399); cortex sets busy_timeout=30000 only (db.py:100). Comment-only contract, no runtime assert.
- migrate() (9 CREATE IF NOT EXISTS + 3 guarded ALTERs on ct_wake_log: tokens/force_slept/net_tokens) runs on every connect; each tick process connects once (db.py:98-129).
- Config: TOML `~/.config/marrow/cortex.toml` (env CORTEX_CONFIG), tolerant deep-merge over _DEFAULTS; legacy [bulletin]→note; [[schedule]] passed raw (config.py:165-193).

## 2. Collectors (`collect_tick.py`, `collectors/`)

- Entry collect_tick.py (launchd com.cortex.collect-tick): run_all → _run_usage_snapshot → _render_daybrief (marrow venv subprocess) → exit 1 if any run_all source failed (usage/daybrief not in exit code).
- run_all: registry {knowledgec, geofence, health}, per-source try/except, result → ct_collector_log (collectors/__init__.py:15-34). daybrief render logged as source='daybrief'.
- knowledgec (always on): read-only URI open of macOS knowledgeC.db, full re-scan of ZOBJECT stream '/app/usage' per tick (indexed, OS retention-capped ~3wk), aggregate per (local_date, bundle_id) + category map → upsert ct_app_usage/ct_category_usage; aged-out dates freeze at last-seen totals (knowledgec.py:25-96).
- geofence (default off): byte-offset cursor in ct_geofence_cursor, truncation resets to 0; parses `HH:MM event` complete lines only; date stamped = today-at-tick (assumes near-zero Shortcut latency); PK (date,time,event) ON CONFLICT DO NOTHING (geofence.py:28-111).
- health (default off, skeleton): full-file JSON flatten to dot-path rows, date from payload date/export_date/day else file mtime; upsert ct_health; no consumer reads ct_health yet (health.py:21-66).
- usage_snapshot: marrow venv subprocess `-m marrow.usage_snapshot`, 15s timeout, gated tick.usage_snapshot; redundant with marrow watcher's own 5-min loop (collect_tick.py:16-35).
- activity.py: read-only helper over ct_activity (written by marrow Stop hook). read_activity() has zero production callers.

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

- run_wake (wake.py:281-382): schedule wakes short-circuit first (only when duties present AND mode='window' AND real caller, wake.py:313); then symlinks.ensure_all, assemble_note, window path (mode='window' AND real caller) else marrow subprocess. No date comparison in run_wake — freshness comes from the rotate flag (night_close sets it once/night; next-morning first wake respawns fresh = the rebirth).
- WakeTimer latency probe always-on: wake_id + CORTEX_WAKE_ID / CORTEX_WAKE_TIMING_LOG env; marks tick_fire→gate_eval→symlinks→note→window_injected/wake_complete into wake_timing log; marrow subprocess shares origin via env (timing.py, wake.py:297-305).
- Schedule path _schedule_wake (wake.py:155-187): per duty spawn_fresh (never resident, sid unpersisted) → inject_note(note + duty prompt_path) → say() ping → mark_schedule_fired; window failure audited + skipped.
- Window path _window_wake (wake.py): write note file → fresh-brain (respawn flag from _window_rotated: rotate flag | sid dead | claude pid gone | newest transcript ≠ recorded; None recorded hint → NOT rotated when flag absent + window alive) → _spawn_wake (respawn window with emoji-only first prompt via note_read_line, no ear/signal, no notification — silent wake) → set_awake + watchdog.spawn; else alive resident → append WAKE line to signal log → _signal_landed polls transcript mtime 3s up to ear_timeout 90s → on miss: _spawn_wake delivers note directly (no re-append). Rotate assembles a second note with fresh=True/wake_kind='rotate' — compat args; handoff (碎碎念) injects at marrow SessionStart, not via note.
- P0 spawn-hint timing (wake.py _spawn_wake): record the NEW session transcript only after _wait_new_transcript (bounded poll ~8s for a jsonl newer than pre-spawn newest or mtime ≥ spawn_ts); timeout → record None, never the stale pre-spawn path (which drove an endless respawn loop every tick). None hint + alive window + no flag → _window_rotated returns False.
- Headless fallback: window path returning None falls through to call_marrow_cortex — marrow venv subprocess, inner timeout marrow.call_timeout_s 600s, outer +30s margin; non-zero rc or bad JSON tail → WakeError (wake.py:71-99, 349-350).
- Token-cap breach (result.capped) → _force_fresh_next (clear sid keep date) + audit + _render_daybrief, no sid persist.
- _audit_wake best-effort inserts audit_log rows on shared DB, swallows all (wake.py:102-112). CLI: --force (bypass gates) | --print-note (wake.py:385-409).

### window.py — iTerm control

- Focus discipline: say() is the sole allowed focus-taker (sound + bring-to-front, window.py:476-483). All typing paths wrap _frontmost_bid/_guard_focus (conditional: restores only if iTerm actually stole front, window.py:67-73). _spawn has internal save/restore (window.py:167-185). _relaunch (window.py:210-216) has no own guard but is only reachable via inject_note's guarded frame, and is unreached in production (sole caller passes explicit sid).
- launch_command(cfg, initial_prompt=None): `cd <cortex_home> && MARROW_CORTEX=1 MARROW_CHANNEL=ct claude --model <wake.window_model, default opus> [--effort] [--dangerously-skip-permissions, default true] [initial_prompt]`. initial_prompt = the emoji-only wake prompt baked in for a fresh window (zero readable text). note_read_line(cfg, path) returns wake.wake_prompt (default ☀️; legacy {note} still substituted for back-compat). Full wake instructions injected by marrow UserPromptSubmit hook on the emoji. Arm-ear mechanism retired (arm.md + config.arm_prompt_path gone).
- _wait_ready polls session text for wake.ready_marker ('accept edits') up to 30s.
- respawn(cfg, initial_prompt=None) = fresh-brain path (rotate/rebirth/dead/ear-miss): SIGTERM old claude, close session, _spawn(initial_prompt baked in), persist sid. Silent — no notification on spawn (spawn_greeting/_notify removed; say() front+sound is the sole attention-getter, no osascript display notification anywhere).
- find_claude_pid: session tty → ps exact-match, fallback pgrep -x + cwd filter; 0 or >1 candidates → None, never guess (window.py:388-459). hard_interrupt = SIGINT on discovered pid only (window.py:462-473).

### wake_state.json (`wake_state.py`)

- Keys: awake/awake_since/wake_log_id/transcript (cleared as a set), session id, wait_count (reset on set_awake), silence_wait_until (one-shot), rotated (read-and-clear via take_rotated), night_wrap_key/night_rotated_key (once-per-night dedup, pacemaker_tick.py:38/47).
- load tolerates missing/corrupt → {}; _save/update = whole-file read-modify-write, no lock, no atomic rename (wake_state.py:34-54 — confirmed cross-process lost-update window, see priority queue).

### watchdog (`watchdog.py`)

- Per-wake detached subprocess spawned at set_awake; pidfile self-guarded (only unlinks own pid) (watchdog.py:29-40, 169-181).
- Poll loop 60s: retires when awake cleared externally; publishes NET tokens via store_window_tokens each poll (own short DB conn); fuse: window_tokens >= wake.watchdog.fuse_tokens (150k) → _fuse then exit; timeout: silent >= silent_max_min (10) honoring one-shot wait_until extension → proxy lie_down(timeout) (watchdog.py:126-166).
- _fuse: esc → inject fuse_handoff_prompt (summarize + append handoff + lie_down(rotate=True)) → poll awake up to 300s grace; only proxy-lie_down if session didn't do it itself; catchup reason only when handoff NOT written (watchdog.py:76-109). esc verify: transcript still growing after grace → hard_interrupt SIGINT, gated wake.watchdog.hard_interrupt_enabled (watchdog.py:43-66).

## 5. lie_down / wait / say

- Exposed as env-gated MCP tools in marrow daemon (MARROW_CORTEX=1), subprocess `-m cortex.<mod>`; also CLI mains for watchdog proxy use.
- lie_down (lie_down.py:86-111): record tokens+net into ct_wake_log (sole writer of those columns; bare `except: pass` — known silent-drop finding) → clear due self_schedule entries → integration.lie_down floor redraw from now (explicit next_wake_min clamped, or dice) → publish net via store_window_tokens → kill watchdog (SIGTERM, skip if self) → optional set_rotated → clear_awake.
- wait (wait.py:23-35): one-shot watchdog silence extension; cap wake.wait_max_per_wake (2) per wake; minutes clamped via triggers floor_min_min/floor_max_min (10/55 defaults — shared with the wake-window bounds, retuning one retunes both); writes deadline + bumps counter (two separate RMW writes).
- say (say.py, window.py:476-483): sound + front resident window — urgent-only attention ping (marrow 4c6209a), everything else stays silent; --note flag accepted but ignored (CLI symmetry only).

## 6. Wakeup note (`note.py`)

- gather (note.py:311-340): every section behind _safe() — assembly never crashes. render pure, sections omit cleanly when absent (note.py:386-446).
- Sections: header Now/Plan Used/Active [+ force_slept_catchup_text] · Pending self-schedule (due within note.pending_window_min 15) · Replay (last note.replay_events 4 user/assistant events, excl channels ('ct',), marker-stripped, 300ch each) · note.turn_end_text trailer · note.title prefix. "Wake:" reason line retired (wander-only, no signal; _wake_parts/_reason_kind_detail gone).
- Budget line (note.py:449-475): `Plan Used: 5h X% | 7d Y% | Cortex Today Nk/Mk | Net Session Token: Wk` — 5h/7d from ct_rate_limit kv (marrow usage_snapshot); Cortex Today = ct_wake_log local-day sum (dup of integration._today_tokens, must stay in sync — note.py:132); Net Session = window_tokens key from ct_pacemaker_state (published by watchdog/lie_down).
- _last_wake skips rows younger than 90s to avoid self-reporting the current wake (note.py:106-127).
- _strip_markers = deliberate local copy of marrow strip_media_markers (marrow not importable from cortex venv), regexes byte-identical today (note.py:189-204 vs marrow/transcript.py:97-117) — drift risk tracked in priority queue.
- Handoff (碎碎念) injection happens at marrow SessionStart, not note.py; cal/rem lines retired pending global inject (note.py:9-10).

## 7. transcript.py — token/liveness probe

- _munge replicates CC cwd→projects dirname; transcript_dir overridable via paths.transcript_dir (transcript.py:14-26). newest() = latest-mtime top-level *.jsonl (subagent files live in subagents/ subdir, naturally excluded).
- window_tokens = LAST usage line's input+cache_read+cache_creation+output → current context occupancy; drives watchdog fuse only (rotate is flag/liveness-based, not token) (transcript.py:42-64, watchdog.py:153).
- net_tokens = SUM of cache_creation+output across session → real spend; drives budget gate + note (transcript.py:67-91).
- Both return 0 silently on read errors; mtime() drives rotation detection + ear polling.

## 8. daybrief.md (retired day_log)

- day_log retired; replaced by marrow-owned daybrief (marrow/daybrief.py) reusing the same SessionStart render functions. Cortex triggers a render via marrow venv subprocess (`python -m marrow.daybrief`) at collect_tick.py:_render_daybrief and wake.py:_render_daybrief; symlinks it into NY.

## 9. Symlinks, install, deploy

- symlinks.py: daybrief.md (marrow-owned source, may dangle until first marrow render) + wishlist.md → NY db-pages; wishlist header from config; refuses non-symlink clobber (FileExistsError propagates); ensure_all safe per-wake (symlinks.py:11-42).
- install.py: `python -m cortex.install [remove]` — writes 2 plists (collect-tick, pacemaker-tick) with 6 __TOKEN__ replacements from config, launchctl bootout+bootstrap into gui/<uid>; no rollback on partial failure (self-healing on re-run); zero test coverage (install.py:16-97).
- No pyproject.toml — package importable only because plists set WorkingDirectory=repo root (`-m` resolves cortex/ from cwd); any other cwd needs PYTHONPATH.
- Plists: RunAtLoad + StartInterval, no KeepAlive/backoff — a crashing tick just re-fires next interval.
- Safety default: pacemaker.dry_run=true shipped in example config; live config also true today.

## 10. Tests

- Per-module test files under tests/; pure cores (pacemaker, note, geofence cursor) well covered.
- Known gaps: install.py (untested entirely), dry_run schedule-dedup path, geofence same-minute-same-text dup.

## 11. Status

- Live: collectors (knowledgec) · pacemaker ticks (dry_run) · wake window path + watchdog + fuse · note · daybrief render (marrow subprocess) · MCP lie_down/wait/say · symlinks.
- Unwired: event triggers · cal_busy/at_home real data · health/geofence collectors (flagged off, no export producer).
- Duties ([[schedule]]): mechanism shipped, both live duties enabled=false pending prompts.

## 12. Marrow-side organs

> Marrow's half of the bridge — ONE module, marrow/cortex_bridge.py, behind [cortex].enabled (false = none of it exists). Details live in marrow/MAP.md §6; this is the index only.

- Six MCP tools via cortex_bridge.register(): wish (one-way append → wishlist.md) · first (tick/untick Cortex First nudges → ct_first_tick) · goal (set/list/delete → goals table) — all sessions when enabled; lie_down · wait · say — cortex session only, each shells `-m cortex.<mod>` into this repo.
- Hook organs (bodies in cortex_bridge, one-line gated call sites in marrow hooks.py): SessionStart handoff page-turn (stale-date archive + fresh template, fresh cortex window only) · lie_down deny (rotate / fuse-line lie_down blocked until handoff written this window) · turn_inject 100k 亮牌 (window-occupancy nudge at [cortex_rotate].show_tokens) · kickout immunity (is_cortex_session(), env-only — identity check, deliberately not behind enabled).
- Session runner: LLMClient.call_cortex (llm.py, thin delegate kept as the stable cross-repo entry — wake.py calls it by exact name) → cortex_bridge.call_cortex / run_claude_cortex — full-env resumed session, origin of MARROW_CORTEX=1 + MARROW_CHANNEL=ct, per-wake token cap + audit; _cortex_stream_timer probe (CORTEX_WAKE_TIMING_LOG).

Two gates, distinct semantics (full model in marrow/MAP.md §6.1): `[cortex].enabled` = organs installed at all (config, default false). `MARROW_CORTEX` (env) = this session IS the cortex session, set at origin by call_cortex.

Not in the bridge module, still marrow-side (marrow/MAP.md §6.5): storage.py migrations v29 (events.flag)/v30 (goals)/v31 (ct_rate_limit)/v32+v34 (ct_first_tick) · config sections [cortex]/[cortex_rotate]/[cortex_usage]/[llm.claude_cli_cortex] in marrow/config.default.toml · deploy/commands/ct-clear.md (slash command wrapping lie_down(rotate=True)) · _window_tokens_from_transcript in hooks.py (shared, not cortex-specific).
