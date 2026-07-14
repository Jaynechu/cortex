2026-07-11

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
                                                daybrief.md (marrow render, real file in NY) · watchdog (per-wake)
```

- Own repo/venv `~/CC-Lab/cortex/`; ct_ tables on shared marrow DB `~/.config/marrow/marrow.db`.
- DB contract: journal_mode owned by marrow (DELETE, marrow/storage.py:399); cortex sets busy_timeout=30000 only, comment-only, no runtime assert (db.py:100).
- migrate() = 9 CREATE + 3 guarded ALTERs on ct_wake_log every connect, one connect/tick (db.py:98-129).
- Config: TOML `~/.config/marrow/cortex.toml` (env CORTEX_CONFIG), tolerant deep-merge over _DEFAULTS; legacy [bulletin]→note (config.py).
## 2. Collectors (`collect_tick.py`, `collectors/`)
- Entry collect_tick.py (launchd com.cortex.collect-tick): run_all → usage_snapshot → _render_daybrief; exit 1 only on run_all source failure.
- run_all: registry {knowledgec, geofence, health}, per-source try/except → ct_collector_log (collectors/__init__.py:15-34).
- knowledgec (always on): read-only re-scan macOS knowledgeC ZOBJECT '/app/usage' → upsert ct_app_usage/ct_category_usage; aged-out dates freeze at last-seen (knowledgec.py:25-96).
- geofence (default off): byte-offset cursor ct_geofence_cursor (truncation resets 0); complete `HH:MM event` lines; date = today-at-tick; PK ON CONFLICT DO NOTHING (geofence.py:28-111).
- health (default off, skeleton): JSON flatten → dot-path rows → upsert ct_health; no consumer yet (health.py:21-66).
- usage_snapshot: marrow venv `-m marrow.usage_snapshot`, 15s, gated tick.usage_snapshot; redundant with marrow watcher 5-min loop (collect_tick.py:16-35).
- activity.py: read-only helper over ct_activity (marrow Stop hook writes it); read_activity() zero production callers.
## 3. Pacemaker (`pacemaker/`)
- core.tick(state, context, config, now, rng) pure — no I/O/wall-clock (core.py:1-8). PacemakerState frozen; legacy desire/expect_reply dropped on load (core.py:19-32, integration.py:61-90).
- Triggers (triggers.py): event (unwired) · affect_flag · self_scheduled · floor. Collision: any real reason silences plain floor this tick. reschedule_floor = uniform [floor_min_min,floor_max_min] (10/55) or explicit clamped.
- Floor redraws on fire BEFORE gates from tick time; real wakes re-anchored to lie-down time by integration.lie_down; gated ticks keep tick-time anchor (core.py:56-59, verified intended).
- Gates (gates.py): night window (wrap-capable, night_key = window-start date) cap vs gates.night.cap; daily_budget vs today_tokens (<=0 disables). No bypass, no short-circuit; wake = reasons and not gated_by.
- NB triggers.py local `pierce` = real-reason set silencing floor (floor-collision only, NOT a gate bypass) — unrelated to any other pierce concept.
- integration.py = sole I/O owner. State = single-row JSON ct_pacemaker_state id=1; side-channel window_tokens survives independently of dataclass saves.
- build_context: active_session = ct_activity within 5min; cal_busy/at_home = config defaults (unwired); affect_flag/self_schedule from JSON; today_tokens = Cortex Today (integration._today_tokens); events [].
- run_tick: load → build_context → tick → save_state + write_wake_log (every tick incl dry_run) → decision (integration.py).
- Entry pacemaker_tick.py: _night_close (wrap-up once/night if awake, else set_rotated once/night) runs BEFORE awake-guard; live wake → run_wake then lie_down only for headless ('window' owns its own).
- Awake gate (_handle_awake, pacemaker_tick.py:54-81): wake-in-progress NEVER emits new signal — runs shared watchdog.silence_action (0.0 mtime = hold).
- Stale reap (idle>=wake.stale.threshold_min 15) uses 1e9 so a gone transcript still reaped. Late sentinel into active wake silent here.
## 4. Wake runner (`wake.py`)
- run_wake: symlinks.ensure_all → assemble_note → window path (mode='window' AND real caller) else marrow subprocess. Freshness from rotate flag, no date compare; next-morning first wake = rebirth.
- WakeTimer latency probe always-on: wake_id + CORTEX_WAKE_ID/CORTEX_WAKE_TIMING_LOG env; marks tick_fire→gate_eval→symlinks→note→injected/complete; marrow subprocess shares origin via env (timing.py).
- _window_wake_plan classifier: fresh (rotate flag | newest transcript ≠ recorded = deliberate /clear) | resume (sid dead/gone, no flag) | ear (alive+unrotated; None recorded hint stays ear). Consumes rotate flag once/wake.
- _window_wake path: fresh → _spawn_wake(resume=False) emoji; dead+no-flag → _resume_or_fresh_dead (sid → --resume same convo no catchup; absent → fresh + died_no_handoff catchup if handoff unwritten).
- Alive → append bell → _signal_landed polls mtime 3s up to ear_timeout 90s.
- _ear_miss_ladder (alive): type_wake_signal rearm → poll → land=ear; claude dead → _resume_or_fresh_dead; rearmed-unconfirmed → set_awake anyway.
- Respawn failure (WindowError) = SOLE alert point → _alert_respawn_failed → marrow alerts row (audit_log fallback).
- Signal line = BELL ONLY: append_wake_signal writes `<wake_signal_marker '[CORTEX-WAKE]'> HH:MM`; marrow UserPromptSubmit hook detects marker → injects full note. type_wake_signal adds rearm_suffix.
- _spawn_wake P0 timing: record NEW transcript only after _wait_new_transcript (~8s poll for jsonl newer than pre-spawn or mtime≥spawn_ts); timeout → record None never stale (stale drove endless respawn loop).
- None hint + alive + no flag → ear.
- Headless fallback: window path None → call_marrow_cortex (marrow venv, inner marrow.call_timeout_s 600s, outer +30s); bad rc/JSON tail → WakeError (wake.py:71-99,349-350).
- Token-cap breach (result.capped) → _force_fresh_next (clear sid keep date) + audit + _render_daybrief. _audit_wake best-effort inserts audit_log, swallows all (wake.py:102-112). CLI: --force (bypass gates) | --print-note.
### window.py — iTerm control
- Focus discipline: say() sole allowed focus-taker (window.py:476-483). Typing paths wrap _frontmost_bid/_guard_focus (restore only if iTerm stole front, 67-73).
- _spawn internal save/restore (167-185). _relaunch (210-216) reachable only via inject_note's guarded frame, unreached in production.
- launch_command: `cd <cortex_home> && MARROW_CORTEX=1 MARROW_CHANNEL=ct claude --model <wake.window_model opus> [--effort] [--resume <sid>] [--dangerously-skip-permissions] [prompt]`.
- initial_prompt = emoji-only (window.wake_prompt ☀️); marrow hook injects full note.
- claude_session_id(cfg) = recorded transcript jsonl stem = conversation UUID for --resume (NOT iTerm session_id); None when no hint. _wait_ready polls session text for wake.ready_marker ('accept edits') up to 30s.
- respawn(cfg, initial_prompt, resume_sid): SIGTERM old claude, close session, _spawn, persist sid. Silent — say() is sole attention-getter.
- find_claude_pid: session tty → ps exact-match, fallback pgrep -x + cwd filter; 0 or >1 → None never guess (window.py:388-459). hard_interrupt = SIGINT on that pid only (462-473).
### wake_state.json (`wake_state.py`)
- Keys: awake set = awake/awake_since/wake_log_id/transcript/silence_wait_until/wait_count/user_replied_this_wake/tuck_pending/last_note_ts (cleared together by clear_awake/claim_lie_down).
- Also: session id; rotated (read-and-clear via take_rotated); sentinel_pid.
- night_wrap_key/night_rotated_key = once/night dedup (pacemaker_tick.py:38/47).
- load tolerates missing/corrupt → {}. Writes via _flock (blocking exclusive on sibling .lock, best-effort) + _save (temp + os.replace, no half-written read); cross-process lost-update fixed (wake_state.py:50-127).
- claim_lie_down (wake_state.py:157-172): atomic read-and-clear of awake marker under the flock; pre-clear snapshot to single winner, None to later callers.
- Guards watchdog poll vs tick awake-branch racing silence_action same window (lie_down.py:98-106).
- lock_path (wake_state.py:40-47): sibling `.lock` of wake_state_file. COUPLED with marrow's `_wake_state_lock` (marrow/MAP.md §6.3) — each resolves from own config; overriding one without the other silently splits the lock.
### watchdog (`watchdog.py`)
- Per-wake detached subprocess spawned at set_awake; pidfile self-guarded (unlinks only own pid, watchdog.py:29-40,169-181).
- Poll 60s: retires when awake cleared externally; publishes occupancy via store_window_tokens each poll.
- Fuse: window_tokens>=fuse_tokens (150k) → _fuse then exit; else silence_action (watchdog.py:202-232).
- silence_action (watchdog.py:151-199, shared by watchdog.run + _handle_awake): two-tier, live wait_until (cortex.wait) holds both.
- No-user tier → no_user_gate_min (5) silent proxy lie_down.
- Chat tier → silent_max_min (15) tuck_in_text marker once (stamps tuck_pending), then tuck_grace_min (5) more → proxy lie_down.
- Wait-expiry tier → wait(N) deadline past: epoch-guarded free-round injection immediately, bypasses silent_min (watchdog.py:194-289).
- Every free-round injection (both tiers) appends a diff-mode wakeup note below the marker (D6, wait_expiry_note toggle, watchdog.py:133-177).
- force_slept="auto" = routine silence marker (note.py neutral, no catchup line), distinct from "timeout" (retired) and real incidents (fuse/stale).
- _fuse: esc → inject fuse_handoff_prompt (summarize + handoff + lie_down(rotate=True)) → poll awake 300s grace; proxy-lie_down only if session didn't; catchup only when handoff unwritten (watchdog.py:76-109).
- esc verify: still growing → hard_interrupt SIGINT, gated hard_interrupt_enabled (43-66).
### sentinel (`sentinel.py`) — exact-time wake
- One-shot detached (start_new_session): sleeps `--seconds N` then one pacemaker_tick.main() — wakes fire on the second. launchd 5-min tick stays self-heal fallback.
- Armed at every lie_down (_arm_sentinel, lie_down.py:135-153): kills recorded predecessor pid, spawns fresh for redrawn next_floor, records pid. Gated [wake].sentinel (default true = spawn, false = tick-only).
- Self-guarded clear (sentinel.run, sentinel.py:40-48): on fire clears own sentinel_pid via clear_sentinel_pid(only_if_pid=self) BEFORE the tick, only if record still own pid (same self-guard as watchdog pidfile).
- Killed on re-arm (_kill_sentinel) + user-wake reset (marrow _cortex_user_wake_reset, marrow/MAP.md §6.3).
## 5. lie_down / wait / say
- Env-gated MCP tools in marrow daemon (MARROW_CORTEX=1) via `-m cortex.<mod>`; also CLI mains for watchdog proxy use.
- lie_down (lie_down.py): next_wake_min REQUIRED at MCP/CLI, clamped triggers.clamp_next_wake_minutes to [1, wake.next_wake_max] (240); proxy callers may pass None for uniform floor dice.
- claim_lie_down (§4) = atomic awake-claim, only winner runs body, later gets `{"skipped":"not awake"}`.
- lie_down body: record occupancy `tokens` into ct_wake_log (sole writer; bare `except:pass` = known silent-drop; net_tokens column historical/unwritten) → clear due self_schedule → integration.lie_down floor redraw.
- Then: store_window_tokens → kill watchdog (skip if self) → optional set_rotated → _arm_sentinel; result adds next_wake=HH:MM.
- wait (wait.py:23-35): one-shot watchdog silence extension; cap wake.wait_max_per_wake (2)/wake.
- Clamped triggers.clamp_window_minutes to [wake.wait_min, wake.wait_max] (1/55) — OWN bounds, decoupled from floor draw window (floor_min_min/floor_max_min 10/55).
- say (say.py, window.py:476-483): sound + front resident window — urgent-only ping, else silent; --note accepted but ignored (CLI symmetry).
## 6. Wakeup note (`note.py`)
- gather (note.py:311-340): every section behind _safe(), render pure, omit cleanly when absent (386-446).
- Sections: header Now/Plan Used/Active [+ force_slept | died_no_handoff catchup] · Pending self-schedule (note.pending_window_min 15).
- Replay (note.replay_events 4, excl channels ('ct',), marker-stripped, 300ch) · turn_end_text · title prefix. "Wake:" reason line retired.
- Diff mode: replay filters events newer than wake_state.last_note_ts (baseline = wake's initial note); every gather() advances it to the newest eligible event (note.py:359-374).
- Budget line (note.py:449-475): `Plan Used: 5h X% | 7d Y% | Cortex Today Nk/Mk | Net Session Token: Wk`. 5h/7d from ct_rate_limit kv.
- Cortex Today via note._today_tokens (delegates to integration._today_tokens, parity by construction).
- Net Session = window_tokens key from ct_pacemaker_state (label kept for marrow parity, not net spend).
- _last_wake skips rows <90s to avoid self-reporting current wake (note.py:106-127). Handoff injection at marrow SessionStart not note.py; cal/rem lines retired pending global inject (note.py:9-10).
- _strip_markers = deliberate local copy of marrow strip_media_markers (not importable from cortex venv), byte-identical today (note.py:189-204 vs marrow/transcript.py:97-117) — drift tracked.
## 7. transcript.py — token/liveness probe
- _munge replicates CC cwd→projects dirname; transcript_dir overridable via paths.transcript_dir (transcript.py:14-26). newest() = latest-mtime top-level *.jsonl (subagents/ excluded).
- window_tokens = LAST usage line input+cache_read+cache_creation+output → occupancy; drives watchdog fuse only; 0 on read error (transcript.py:42-64, watchdog.py:153). mtime() drives rotation + ear polling.
- net_tokens helper retired; Cortex Today sums per-window final occupancy not per-turn spend.
## 8. daybrief.md (retired day_log)
- marrow-owned (marrow/daybrief.py), real file in NY db-pages via marrow paths.daybrief (no cortex symlink); Cortex triggers render via marrow venv `-m marrow.daybrief` at collect_tick._render_daybrief + wake._render_daybrief.
## 9. Symlinks, install, deploy
- symlinks.py: wishlist.md → NY db-pages; existing real file at target = no-op (never clobbers, guards daybrief migration); ensure_all safe per-wake (symlinks.py:11-39).
- install.py: `python -m cortex.install [remove]` — writes 2 plists with 6 __TOKEN__ replacements, launchctl bootout+bootstrap gui/<uid>; no rollback (self-heals on re-run); zero test coverage (install.py:16-97).
- pyproject.toml (setuptools, no third-party deps); plists set WorkingDirectory=repo root so `-m` resolves cortex/ without install/PYTHONPATH.
- Plists: RunAtLoad + StartInterval, no KeepAlive/backoff — crashing tick re-fires next interval.
- pacemaker.dry_run=true in example config; live dry_run=false since 07-11.
## 10. Tests
- Per-module test files under tests/; pure cores (pacemaker, note, geofence cursor) well covered. Gaps: install.py (untested), geofence same-minute-same-text dup.
## 11. Status
- Live: collectors (knowledgec) · pacemaker (dry_run=false) · wake window + watchdog + fuse + sentinel · note · daybrief render (real file in NY) · MCP lie_down/wait/say · wishlist symlink.
- Unwired: event triggers · cal_busy/at_home real data · health/geofence collectors (flagged off, no producer).
## 12. Marrow-side organs
> Marrow's half of the bridge — ONE module marrow/cortex_bridge.py, behind [cortex].enabled. Details marrow/MAP.md §6; index only.
- Six MCP tools via cortex_bridge.register(): wish (append → wishlist.md) · first (tick/untick → ct_first_tick) · goal (set/list/delete → goals table) — all sessions when enabled.
- lie_down · wait · say — cortex session only, shell `-m cortex.<mod>`.
- Hook organs (bodies in cortex_bridge, gated call sites in marrow hooks.py): SessionStart handoff page-turn (fresh cortex window only) · lie_down deny (rotate/fuse-line blocked until handoff written).
- turn_inject 100k 亮牌 ([cortex_rotate].show_tokens) · kickout immunity (is_cortex_session(), env-only, not behind enabled).
- Session runner: LLMClient.call_cortex (llm.py, stable cross-repo entry, wake.py calls by name) → cortex_bridge.call_cortex / run_claude_cortex.
- Full-env resumed session, origin of MARROW_CORTEX=1 + MARROW_CHANNEL=ct, per-wake token cap + audit.
- Gates (marrow/MAP.md §6.1): `[cortex].enabled` = organs installed at all (default false); `MARROW_CORTEX` env = this session IS the cortex session.
- Still marrow-side (marrow/MAP.md §6.5): storage.py migrations v29/v30/v31/v32+v34 · config [cortex]/[cortex_rotate]/[cortex_usage]/[llm.claude_cli_cortex].
- deploy/commands/ct-clear.md (lie_down(rotate=True)) · _window_tokens_from_transcript in hooks.py (shared).
