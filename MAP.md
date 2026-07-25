2026-07-26

# Cortex — MAP

> How each part works today. Not SoT — code wins. Refs `file:line` (package files under cortex/).
> Goals → DESIGN.md. Plan → CC-Lab/docs/plans/ct-plan.md.
## 1. Architecture
```
collectors (launchd 1800s) ──▶ ct_ tables (marrow.db)
                                    │
wake daemon (launchd KeepAlive, always on) ──reconcile (60s cadence)──▶ decision ──▶ wake.run_wake
     Scheduler-hosted (synapse_core); 2 deadlines:       ct_pacemaker_state       │
     <shell>.reconcile (fixed) / <shell> (business)      ct_wake_log        note → iTerm window (resident claude) | marrow subprocess fallback
     socket kick (lie_down/kick.py) fires early                                 │
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
## 3. Wake daemon + reconcile (`daemon.py`, `reconcile.py`)
> Pacemaker package (core/triggers/gates/integration/pacemaker_tick/sentinel) deleted whole, T11 P4. The floor/trigger decision engine (event/affect_flag/self_scheduled triggers, gate collision) is retired — the durable next_wake_at ledger is the only wake source. Floor redraw survives as a plain dice draw (occupancy.reschedule_floor, §7).
- daemon.py WakeDaemon embeds synapse_core.Scheduler over an AF_UNIX kick socket (config.daemon_socket_path, <=104 bytes). Two deadlines per scheduler shell key (Scheduler holds ONE per key; firing consumes it): `<shell>.reconcile` (fixed [daemon].reconcile_interval_sec cadence, self-rearms) and `<shell>` (business = min(next_wake_at, silence-round due); safety_horizon_sec when idle; retry_interval_sec after a held/failed fire) (daemon.py:93-126).
- Every callback re-arms on entry AND in a `finally` (daemon.py:147-161) so a consumed Scheduler entry or a raised exception never leaves a key unarmed; `kick()` no-ops on a key with no entry, so the business key must always stay armed.
- Blocking work (reconcile_once/business_once) runs via `asyncio.to_thread` so the kick socket stays responsive; an `asyncio.Lock` serialises the two callback bodies against each other (daemon.py:109,150,158).
- reconcile.py `_reconcile`/`_handle_awake`: ledger reconcile relocated verbatim from the retired pacemaker_tick entry point (T11 P4) — DND hold (wake_state.is_paused), manual adopt (`[wake].auto_adopt`, under the spawn lock), dead+due fire (rotated?fresh:resume via `_fire_dead_window`), accidental-close resume, watchdog heal (respawn if pidfile dead), silence backup (delegates to watchdog.silence_action), stale-suspect debounce (`confirm_ticks` consecutive dead verdicts, epoch-guarded against a lie_down/reset mid-pass) (reconcile.py:21-100,227-264).
- Fire path: synthesize a decision dict (`reasons`/`wake_reasons` kick|next_wake_at|silence|reconcile, ctl.py precedent) → `wake.run_wake`. Both `daemon._fire_wake` and `reconcile._fire_dead_window` gate on `gates.daily_budget.tokens` (hold, ledger un-consumed, retried next pass) and `[pacemaker].dry_run` (log-only floor redraw, no real wake) (daemon.py:207-224, reconcile.py:142-182).
- kick.py `_notify_daemon` → `synapse_core.send_kick(socket_path, shell)`: state writes (reason queue / gen bump / floor clear) land first, then the kick makes the daemon re-read instantly instead of waiting for the next reconcile cadence. Unreachable socket → wake_audit row + stderr, wake deferred to reconcile (kick.py:73-100).
- No more one-shot exact-time process (sentinel.py, deleted): the business deadline itself fires at the exact computed time via the Scheduler; lie_down's `persist_next_wake_at` (lie_down.py:196-207) writes the ledger then kicks the socket instead of spawning/killing a `sleep N` subprocess.
- `[daemon]` config: enabled, shell, socket_path, kick_timeout_sec, reconcile_interval_sec, safety_horizon_sec, retry_interval_sec, lock_path (config.py:224-241). Singleton lock = `daemon.lock` (daemon.py:229-253).
## 4. Wake runner (`wake.py`)
- run_wake: symlinks.ensure_all → assemble_note → window path (mode='window' AND real caller) else marrow subprocess. Freshness from rotate flag, no date compare; next-morning first wake = rebirth.
- WakeTimer latency probe always-on: wake_id + CORTEX_WAKE_ID/CORTEX_WAKE_TIMING_LOG env; marks tick_fire→gate_eval→symlinks→note→injected/complete; marrow subprocess shares origin via env (timing.py).
- _window_wake_plan classifier: fresh (rotate flag | newest transcript ≠ recorded = deliberate /clear) | resume (sid dead/gone, no flag) | ear (alive+unrotated; None recorded hint stays ear). Consumes rotate flag once/wake.
- _window_wake path: fresh → _spawn_wake(resume=False) emoji; dead+no-flag → _resume_or_fresh_dead (sid → --resume same convo; absent → fresh, plain).
- Alive → type bell (type_wake_signal) → _signal_landed polls mtime 3s up to ear_timeout 90s.
- _ear_miss_ladder (alive): type_wake_signal rearm → poll → land=ear; claude dead → _resume_or_fresh_dead; rearmed-unconfirmed → set_awake anyway.
- Respawn failure (WindowError) = SOLE alert point → _alert_respawn_failed → marrow alerts row (audit_log fallback).
- Signal line = BELL ONLY: type_wake_signal writes `wake_bell_template` human text (e.g. '☀️ HH:MM') via write_wake_receipt; machine data (gen/state_id/rearm) goes to wake_state receipt sidecar; marrow UserPromptSubmit hook matches bell line against receipt → injects full note.
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
- respawn(cfg, initial_prompt, resume_sid): _spawn + persist sid. Old window left OPEN, old claude NOT killed (rotate: predecessor stays for user to close; resume: nothing to kill). Silent — say() is sole attention-getter.
- find_claude_pid: session tty → ps exact-match, fallback pgrep -x + cwd filter; 0 or >1 → None never guess (window.py:388-459). hard_interrupt = SIGINT on that pid only (462-473).
### wake_state.json (`wake_state.py`)
- Keys: awake set = awake/awake_since/wake_log_id/transcript/user_replied_this_wake/tuck_pending/last_note_ts/kick_round (cleared together by clear_awake/claim_lie_down).
- tuck_pending = "last free-round injection at" ISO ts (silence-cycle carrier; legacy field name).
- Also: session id; rotated (read-and-clear via take_rotated).
- cli shell only. Non-cli shells keep their own ledger at `<paths.shell_state_dir>/<shell>.json` (config.py:373, mirror of marrow [cortex].shell_state_dir), written by that shell's host — this file is never shared.
- load tolerates missing/corrupt → {}. Writes via _flock (blocking exclusive on sibling .lock, best-effort) + _save (temp + os.replace, no half-written read); cross-process lost-update fixed (wake_state.py:50-127).
- claim_lie_down (wake_state.py:157-172): atomic read-and-clear of awake marker under the flock; pre-clear snapshot to single winner, None to later callers.
- Guards watchdog poll vs tick awake-branch racing silence_action same window (lie_down.py:98-106).
- lock_path (wake_state.py:40-47): sibling `.lock` of wake_state_file. COUPLED with marrow's `_wake_state_lock` (marrow/MAP.md §6.3) — each resolves from own config; overriding one without the other silently splits the lock.
### watchdog (`watchdog.py`)
- Per-wake detached subprocess spawned at set_awake; pidfile self-guarded (unlinks only own pid, watchdog.py:29-40,169-181). Not started when `[core].shells` omits "cli" (watchdog.py:514).
- Poll 60s: retires when awake cleared externally; publishes occupancy via store_window_tokens each poll.
- Fuse: window_tokens>=fuse_tokens (150k) → _fuse then exit; else silence_action (watchdog.py:458-511).
- Silence loop (silence_action, watchdog.py:355-444, shared by watchdog.run + _handle_awake): every silent_max_min (20) of user silence → inject one free-round block → re-arm the SAME timer from that instant → repeat forever. The session stays up until it calls lie_down itself.
- Free-round block = diff-mode wakeup note (events since wake_state.last_note_ts) then the tuck_in_text `[NEW ROUND]` line LAST, so the whole block reads machine-tagged (D6, `[wake].free_round_note` toggle, watchdog.py:220-302).
- tuck_in_text MUST carry the `[NEW ROUND]` marker: an unmarked line counts as user speech and resets the cycle it just armed (perpetual-loop trap).
- No-user wake → silent_min timed from awake_since (no user-message ts to derive it from), same bar.
- Kick carrier (kick.py mark_kick_round) short-circuits the silent_min gate: inject now, then re-arm the same cycle.
- Commit is epoch-guarded (conditional_mutate): a lie_down/user reset between build and write drops the injection (BUG B).
- watchdog._log = timestamped heartbeat to watchdog.log (start/retire/fuse/silence_action) — proves the dedicated watchdog is live vs riding only the tick backup (watchdog.py:336-345).
- force_slept="auto" = routine silence marker (note.py neutral, no catchup line), distinct from "timeout" (retired) and real incidents (fuse/stale).
- _fuse: esc → write `⚙️ [FUSE]` marker (body = marrow [cortex].fuse_prompt_text: update own handoff + lie_down(rotate=True)) → poll awake 300s grace; proxy-lie_down only if session didn't; force_slept set only when handoff unwritten (watchdog.py:166-205).
- esc verify: still growing → hard_interrupt SIGINT, gated hard_interrupt_enabled (43-66).
## 5. lie_down / say
- Env-gated MCP tools in marrow daemon (MARROW_CORTEX=<shell>) via `-m cortex.<mod>`; also CLI mains for watchdog proxy use. lie_down = every shell; say = cli only (marrow/MAP.md §6.2).
- lie_down (lie_down.py): next_wake_min REQUIRED at MCP/CLI, clamped lie_down.clamp_next_wake_minutes to [0, wake.next_wake_max] (240) at EVERY hour — 0 = immediate re-wake; proxy callers may pass None for uniform floor dice.
- claim_lie_down (§4) = atomic awake-claim, only winner runs body, later gets `{"skipped":"not awake"}`.
- lie_down body: record occupancy `tokens` into ct_wake_log (sole writer; bare `except:pass` = known silent-drop; net_tokens column historical/unwritten) → clear due self_schedule → occupancy.lie_down floor redraw.
- Then: store_window_tokens → kill watchdog (skip if self) → optional set_rotated → persist_next_wake_at (ledger write + daemon socket kick, §3); result adds next_wake=HH:MM.
- say (say.py, window.py:476-483): sound + front resident window — urgent-only ping, else silent; --note accepted but ignored (CLI symmetry).
- Handoff: per-shell rolling log `<cortex_home>/handoff-<shell>.md` (cli default config.DEFAULT_HANDOFF, override paths.handoff_file). Read via the session's own CLAUDE.md memory import; page-turn is marrow-side (marrow/MAP.md §6.3). Cortex reads its mtime only — fuse "handoff written?" (watchdog.py:206).
## 6. Wakeup note (`note.py`)
- gather (note.py:311-340): every section behind _safe(), render pure, omit cleanly when absent (386-446).
- Sections: header Now/Plan Used/Active [+ force_slept] · Pending self-schedule (note.pending_window_min 15).
- Replay (note.replay_events 4, marker-stripped, 300ch) · turn_end_text (default "") · title prefix. "Wake:" reason line retired.
- Replay excludes the rendering shell's OWN channel: `note_render --shell <id>` picks note.shell_replay_exclude[id] (cli→ct, tg→tg); no --shell falls back to note.replay_exclude_channels.
- Diff mode: replay filters events newer than wake_state.last_note_ts (baseline = wake's initial note); every gather() advances it to the newest eligible event (note.py:359-374).
- Budget line (note.py:449-475): `Plan Used: 5h X% | 7d Y% | <label> Today Nk/Mk P% | Net Session Token: Wk`. 5h/7d from ct_rate_limit kv. One line per shell, label-first, note.shell_labels (Cortex-Cli / Cortex-Tg).
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
- install.py: `python -m cortex.install [remove]` — writes 2 plists (collect-tick, wake-daemon) with __TOKEN__ replacements, launchctl bootout+bootstrap gui/<uid>; also boots out the retired com.cortex.pacemaker-tick label (resolved plist left on disk, T11 P4 rollback path); no rollback for the live plists (self-heals on re-run); zero test coverage (install.py:32-38,68-96).
- pyproject.toml (setuptools, no third-party deps); plists set WorkingDirectory=repo root so `-m` resolves cortex/ without install/PYTHONPATH.
- Plists: collect-tick = RunAtLoad + StartInterval (tick.collect_interval_sec), no KeepAlive — a crash re-fires next interval. wake-daemon = RunAtLoad + KeepAlive + ThrottleInterval 10s — always on, launchd revives a crash.
- pacemaker.dry_run=true in example config; live dry_run=false since 07-11 (reconcile.py:171 gates whether a fired ledger wake actually spawns).
## 10. Tests
- Per-module test files under tests/; pure cores (reconcile, daemon, note, geofence cursor) well covered. Gaps: install.py (untested), geofence same-minute-same-text dup.
## 11. Status
- Live: collectors (knowledgec) · wake daemon (reconcile, dry_run=false) · wake window + watchdog + fuse · note · daybrief render (real file in NY) · MCP lie_down/say · wishlist symlink · shells cli+tg.
- Unwired: health/geofence collectors (flagged off, no producer).
## 12. Marrow-side organs
> Marrow's half of the bridge — ONE module marrow/cortex_bridge.py, behind [cortex].enabled. Details marrow/MAP.md §6; index only.
- MCP tools via cortex_bridge.register(): wish (append → wishlist.md) · first (tick/untick → ct_first_tick) · goal (set/list/delete → goals table) — all sessions when enabled.
- lie_down (every shell in marrow [cortex].shells) · say (cli shell only) — shells `-m cortex.<mod>`.
- Shell id rides `MARROW_CORTEX` (cli/tg; legacy "1" = cli); a channel absent from [cortex].shells runs plain (no cortex tools, no heartbeat). Mirror in this repo: `[core].shells`.
- Hook organs (bodies in cortex_bridge, gated call sites in marrow hooks.py): SessionStart handoff page-turn, line-count (fresh cortex window only) · lie_down deny (rotate/fuse-line blocked until handoff written) · lie_down nudge (non-blocking additionalContext, rotate arg picks its copy) · FUSE/CTL covert bodies.
- Non-cli shell host = the synapse tg bridge (synapse/MAP.md): owns the scheduler loop, feed turns, token ledger → `<shell_state_dir>/tg.json`, directed kick.
- turn_inject 100k 亮牌 ([cortex_rotate].show_tokens) · kickout immunity (is_cortex_session(), env-only, not behind enabled).
- Session runner: LLMClient.call_cortex (llm.py, stable cross-repo entry, wake.py calls by name) → cortex_bridge.call_cortex / run_claude_cortex.
- Full-env resumed session, origin of MARROW_CORTEX=cli + MARROW_CHANNEL=ct, per-wake token cap + audit.
- Gates (marrow/MAP.md §6.1): `[cortex].enabled` = organs installed at all (default false); `MARROW_CORTEX` env = this session IS the cortex session.
- Still marrow-side (marrow/MAP.md §6.5): storage.py migrations v29/v30/v31/v32+v34 · config [cortex]/[cortex_rotate]/[cortex_usage]/[llm.claude_cli_cortex].
- deploy/commands/ct-clear.md (lie_down(rotate=True)) · _window_tokens_from_transcript in hooks.py (shared).
