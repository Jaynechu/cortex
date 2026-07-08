2026-07-04

# Cortex — MAP

> How each part works. Not SoT — code wins. Refs are `file:function`.
> Goals → DESIGN.md. Plan → CC-Lab/docs/plans/ct-cortex-v1.md.

## 1. Architecture

```
collectors (launchd ~30min) ──▶ ct_ tables (marrow.db)
                                     │
pacemaker (launchd ~5min) ──tick()──▶ decision ──▶ wake.run_wake
                              ct_pacemaker_state        │
                              ct_wake_log         bulletin.assemble → LLMClient.call_cortex
                                                        │
                                                  day_log.md v2 (symlinked → NY)
```

Own repo/venv (`~/CC-Lab/cortex/`), sibling of marrow/synapse. ct_ tables + events/audit_log on marrow shared DB (`~/.config/marrow/marrow.db`). DB contract (`db.py`): journal mode owned by marrow (DELETE) — cortex sets busy_timeout=30000 only. Wake call via marrow subprocess (`wake.py:call_marrow_cortex` → `LLMClient.call_cortex`), no in-process import.

## 2. Collectors (`collectors/`)

Registry COLLECTORS = {knowledgec, geofence, health}; `run_all` catches per-source, logs ct_collector_log. Entry: `collect_tick.py:main` (1800s). Also calls `day_log.update` post-run.
- knowledgeC: macOS ZOBJECT app usage → ct_app_usage + ct_category_usage (config category map). Always on.
- geofence: byte-offset cursor, `HH:MM event` lines → ct_geofence. Gated (default false).
- health: tolerant JSON flatten → ct_health. Gated (default false), fields pending.
- activity (`activity.py`): read-only over ct_activity (written by marrow Stop hook — cortex consumes only).

## 3. Pacemaker (`pacemaker/`)

Pure decision core — no I/O, no wall-clock; `now`/`rng` injected.
- State (`core.py`): desire 4-float, expect_reply, next_floor_due_at, last_wake_at, last_lie_down_at, night_cap_key/count, cortex session fields (sid+date, opaque to tick).
- Desire (`desire.py`): attachment/curiosity/worry/duty, base_rate-decay [0,1]. Attachment modulated: cal_busy→0; home+free+gap→2x; else 1x.
- Triggers (`triggers.py`): event (always [] — unwired) · affect_flag · desire threshold · self_scheduled · floor (10-55min uniform from lie-down). Facts only, no pre-written motive. Collision: floor governs desire+floor only (desire held behind floor, accrues meanwhile); event/affect_flag(trigger)/self_scheduled(schedule) pierce anytime, trigger>schedule; coincident→one wake; plain floor silent when any other source fires.
- Expect-reply (`expect_reply.py`): pending→check 30min→escalate tone+worry. Unwired (no outbound, C5).
- Gates (`gates.py`): night mode 00-06 cap 1 (desire/floor/expect_reply consume cap; event/affect_flag/self_scheduled pierce) is the SOLE gate. No cooldown/daily-cap/token-budget/fatigue/active-suspend — spend protection = 150k per-wake fuse + bulletin battery gauge.
- Integration (`integration.py`): I/O owner. build_context: active_session (5min window), cal_busy/at_home (config defaults), affect_flag + self_schedule files. State = ct_pacemaker_state single-row JSON. run_tick→tick→save→ct_wake_log (always, even dry_run). lie_down redraws floor from lie-down time.
- Entry: `pacemaker_tick.py` (300s); wake.run_wake only when wake=true AND dry_run=false.

## 4. Wake runner (`wake.py`)

run_wake: daily rebirth (session_date≠today) → archive day_log → new_day → persist date/None sid BEFORE call (retry-safe). symlinks.ensure_all every wake. assemble_bulletin → call_marrow_cortex (subprocess, inner timeout 600s config, outer=inner+30s). After: save sid+date → day_log.update.
Handoff (碎碎念) round-trip: fresh window gets handoff.md. Rebirth is fresh (wins). Window path also detects rotate (`_window_rotated`): rotate flag (lie_down set_rotated on /clear, read-and-clear) OR newest transcript ≠ recorded set_awake hint (a /clear starts a new session jsonl — verified) OR window dead/never-spawned. wake_kind=rotate → 碎碎念; un-rotated resume stays non-fresh (continuity in window context). Gated by note.handoff_wake_kinds.
CLI: --print-note | --force (bypass gates).
Marrow side (`llm.py:call_cortex`): NO isolation (full persona/rules/MCP/agents), MARROW_CORTEX=1, bypassPermissions, --resume. Tier top (opus).

## 5. day_log.md v2 (`day_log.py`)

Six zones, stable HTML-comment markers:
- First: cortex action lines, preserved byte-for-byte on re-render.
- Status: render-only — last-seen (ct_activity, _utc_day_bounds), top usage, collector health.
- Flow: geofence + tl rows (events role='tl'), sorted HH:mm, self-heals position. DB→render only, no reconcile.
- Tasks: placeholder + subnote "task pool, not nag triggers — coax only".
- Track: placeholder (category/sleep inference future).
- Stellan's Notes: cortex free text, carried byte-for-byte.
new_day overwrites (caller archives first). archive → date.md, -N suffix fallback (no clobber).

## 6. Bulletin (`bulletin.py`)

gather: ct_activity · ct_category_usage · events count · decision facts · ct_rate_limit kv · last N events pairs.
render sections: Now · Trigger · Last activity · Calendar "none" · Usage · Budget gauge (5h/7d% + reset, "no data" if missing) · Counts · Expect-reply · forced replay (3 pairs, tool-stripped, 240ch/msg). Cap 2000ch config.

## 7. Config, symlinks, install

Config (`config.py`): TOML ~/.config/marrow/cortex.toml, deep-merge over _DEFAULTS.
Symlinks (`symlinks.py`): day_log.md + wishlist.md → ~/Desktop/NY/db-pages. Creates wishlist if missing, refuses non-symlink clobber. ensure_commands → <home>/.claude/commands: __VENV_PYTHON__ templates (say/lie-down) land as token-resolved copies (no personal path in repo, migrates legacy symlink); rest symlink.
Install (`install.py`): 2 plists (com.cortex.collect-tick, com.cortex.pacemaker-tick), template tokens from config.
Safety: pacemaker.dry_run=true default — flip at C5.

## 8. Status

Shipped: C1 collectors · C2 pacemaker · C3 wake+bulletin+day_log+symlinks · C4-Block1 (Flow/Tasks renames, tick-path render, budget gauge, forced replay).
Unwired: event triggers · cal_busy/at_home from real data · expect-reply start() · Tasks/Track data.
Flagged off: health + geofence collectors (no export shape).
