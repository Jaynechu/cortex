2026-07-03

# Cortex — MAP

> Speed-read for a new session: how each part works, without opening code. Not SoT — code wins.
> Refs are `file:function`. Params inline are live config.example.toml defaults.
> Written after C1 collectors + C2 pacemaker + C3 wake runner/day_log v2 landed (commit 08703dd, 111 tests green).

## 0. Contents

§1 architecture · §2 collectors · §3 pacemaker · §4 wake runner · §5 day_log · §6 bulletin · §7 config/install · §8 status

## 1. Architecture

```
collectors (launchd ~30min) ──▶ ct_ tables (marrow.db) ◀── A1 Stop hook (ct_activity)
                                     │
pacemaker (launchd ~5min tick) ──pure tick()──▶ decision ──dry_run=false──▶ wake.run_wake
                                     │                                          │
                              ct_pacemaker_state                     bulletin.assemble
                              ct_wake_log                                      │
                                                                    marrow LLMClient.call_cortex
                                                                    (subprocess, own venv, full-env)
                                                                                 │
                                                                    day_log.md v2 zones (symlinked → NY)
```

Cortex is its own repo/venv (`~/CC-Lab/cortex/`), sibling of marrow. It reads/writes
`ct_` prefixed tables plus `events`/`audit_log` on marrow's shared DB
(`~/.config/marrow/marrow.db` default). The wake call to a real claude session goes
through marrow's `LLMClient.call_cortex`, invoked as a subprocess against marrow's
own venv python (`cortex/wake.py:call_marrow_cortex`) — no in-process import, keeps
the two repos decoupled.

DB connection contract (`cortex/db.py:connect_path`, fixed 08703dd): journal mode is
owned by marrow (DELETE convention, marrow/storage.py) — cortex must never set
`PRAGMA journal_mode`. An earlier version set `journal_mode=WAL` on every
collector/pacemaker connect, which fought marrow's DELETE mode and caused marrow
CLI/MCP connects to fail with "database is locked"; cortex now only sets
`PRAGMA busy_timeout=30000` and leaves journal mode alone.

## 2. Collectors (`cortex/collectors/`, `cortex/db.py`)

- Registry `collectors/__init__.py:COLLECTORS` = `{knowledgec, geofence, health}`;
  `run_all` catches per-source exceptions, logs to `ct_collector_log`, one failure
  never blocks the others. Entry point `collect_tick.py:main` (launchd, config
  `tick.collect_interval_sec` = 1800s).
- **knowledgeC** (`collectors/knowledgec.py:collect`) — reads
  `ZOBJECT WHERE ZSTREAMNAME='/app/usage'` from macOS `knowledgeC.db` (read-only URI
  open), CoreData epoch offset 978307200. Per-run recompute + upsert into
  `ct_app_usage` (date, bundle_id) and `ct_category_usage` (date, category via
  `config[knowledgec.categories]` map, default `"uncategorized"`). Always enabled,
  raises if `knowledgec_db` path missing.
- **geofence** (`collectors/geofence.py:collect`) — gated by `config[geofence].enabled`
  (default false). Byte-offset cursor per source file (`ct_geofence_cursor`), reads
  only newly-appended complete lines (`HH:MM event text` via `LINE_RE`), stamps with
  local date at ingest time (no per-line date in source — documented assumption:
  backlog >1 collector-down-day mis-dates to catch-up day). Truncated file → cursor
  resets to 0. Rows keyed `(date, time, event)` via `ON CONFLICT DO NOTHING`.
- **health** (`collectors/health.py:collect`) — gated by `config[health].enabled`
  (default false). Tolerant flatten: every leaf key in the JSON export becomes one
  `ct_health` row (`_flatten`), no field-specific parsing — fields are still unknown
  (waits on Lumi's export shape). Date from payload's own `date`/`export_date`/`day`
  key, else file mtime. Upsert on `(date, source, key)`.
- **activity** (`collectors/activity.py`) — NOT a collector (not in `COLLECTORS`);
  read-only helper over `ct_activity`, which is written elsewhere by A1's marrow Stop
  hook (per-turn ts/sid/channel) — cortex only consumes it.

## 3. Pacemaker (`cortex/pacemaker/`)

Pure decision core, no I/O, no wall-clock reads — `now` and `rng` always injected.

- **State** `pacemaker/core.py:PacemakerState` — `desire` (4-float `DesireState`),
  `expect_reply` (`ExpectReplyState`), `next_floor_due_at`, `last_wake_at`, plus
  cortex-session-resume fields (`cortex_session_id`, `cortex_session_date`) that
  `tick()` passes through opaquely — only `cortex/wake.py` reads/writes them.
- **Desire** `pacemaker/desire.py:tick` — 4 dims (attachment/curiosity/worry/duty),
  each `base_rate_per_min - decay_rate_per_min`, clamped [0,1]. Only `attachment`
  is context-modulated (`_attachment_multiplier`, Decided 07-03): `cal_busy` →
  `busy_multiplier` (0.0 = no accrual); `at_home` + gap since `last_real_chat_at`
  ≥ `gap_threshold_min` (180) → `home_free_multiplier` (2.0); else 1.0.
- **Triggers** `pacemaker/triggers.py:evaluate` — 5 kinds, in order: event
  (`context["events"]`, currently always `[]` — no event source wired yet),
  affect_flag (`context["affect_flag"]` truthy), desire (any dim ≥
  `triggers.desire_thresholds[dim]`), self_scheduled (`due_at <= now`), floor
  (`next_floor_due_at` elapsed, default 60min ± 10min jitter drawn only on fire via
  `reschedule_floor`). Reasons carry facts, never pre-written motive text (Design:
  reasoning happens in the cortex session, not here).
- **Expect-reply** `pacemaker/expect_reply.py` — `start(now)` marks pending after an
  outbound expecting a reply (not yet wired — no outbound exists, C5). `evaluate`
  checks every `check_interval_min` (30) while pending+unreplied, escalates
  `tone_level` through `tone_levels` config, adds `worry_increment` (0.05) to the
  worry desire each check, unlimited checks (no start() call limit).
- **Gates** `pacemaker/gates.py:ALL_GATES` — active-suspend (active session → block),
  cooldown (`gates.cooldown_min` 45 since last wake), daily-cap (`daily_message_cap`
  12, counted via `ct_wake_log` wake=1 rows today), fatigue-window (`fatigue_windows`,
  wraps midnight, default 23:30-07:00), token-budget
  (`token_budget_remaining_fraction < token_budget_min_reserve` 0.1 blocks). All run
  (no short-circuit) so every result logs; `wake = bool(reasons) and not gated_by`.
- **Integration** `pacemaker/integration.py` — owns all I/O. `build_context` computes:
  `active_session` (last `ct_activity` row within `active_window_min` 5min),
  `token_budget_remaining_fraction` (parses `audit_log.summary` for
  `in=.. out=.. cache_read=.. cache_write=..` over `window_hours`; budget=0 → always
  1.0, gate no-op), `cal_busy`/`at_home` currently config defaults only
  (`pacemaker.cal_busy_default` false / `at_home_default` true — geofence-derived
  location not wired in yet), `affect_flag`/`self_scheduled` read from JSON files
  (`config.affect_flag_path`/`self_schedule_path`, tolerant — missing/corrupt →
  default). State persisted single-row JSON in `ct_pacemaker_state` (id=1);
  `run_tick` = load state → build context → `core.tick` → save state → write
  `ct_wake_log` row (always, even dry_run — dry_run is log-only downstream, not a
  skip). `pacemaker_tick.py:main` is the launchd entry (`tick.pacemaker_interval_sec`
  300s default plist, config example 5min); calls `wake.run_wake` only when
  `decision["wake"] and not dry_run`.

## 4. Wake runner (`cortex/wake.py`)

- `run_wake` — daily rebirth: `rebirth = state.cortex_session_date != today` (local
  tz). On rebirth: archive existing `day_log.md` (`day_log.archive`, no-clobber
  `-N` suffix fallback), create fresh (`day_log.new_day`), **persist the date/None
  session_id rollover to `ct_pacemaker_state` immediately** — before the marrow call
  — so a failed wake retry doesn't re-archive the fresh blank log over the real one.
  `resume_sid = None` on rebirth, else `state.cortex_session_id`.
- `symlinks.ensure_all` runs every wake (idempotent) — see §7.
- `assemble_bulletin` → `call_marrow_cortex` (subprocess spawn of marrow's venv
  python running an inline script that imports `marrow.llm.LLMClient` and calls
  `.call_cortex(prompt, cwd=cortex_home, resume_sid=.., timeout=inner)`).
- **Timeout unification** (0ea536d): `marrow.call_timeout_s` (config, 600s) is the
  single source — passed down as the *inner* claude-call budget; the *outer*
  subprocess.run timeout = inner + `_OUTER_TIMEOUT_MARGIN_S` (30s), so marrow's own
  `threading.Timer` always fires first with a clean `LLMError` before cortex's outer
  kill would. Must stay in sync with marrow's `[llm.claude_cli_cortex].timeout_s`.
- After the call: `new_state.cortex_session_id = result.get("session_id") or
  resume_sid` (falls back to the sid it resumed if the call didn't return a fresh
  one), `cortex_session_date = today`, saved. Then `day_log.update` re-renders
  Status/Flow/Tasks/Track from current DB state. `collect_tick.py:main` also
  calls `day_log.update` after each collector run (07-04) — file stays fresh
  between wakes, not wake-only; skips quietly if day_log.md doesn't exist yet
  (wake still owns creation/archive).
- `wake.py:main` — manual CLI entry: `--print-bulletin` (assemble + print only, no
  marrow call) or `--force` (bypass pacemaker gates, synthetic wake decision).
- Marrow-side runner: `marrow/llm.py:LLMClient.call_cortex` → `_run_claude_cortex` —
  NO isolation flags (full persona/rules/MCP/agents load), always injects
  `MARROW_CORTEX=1` env so marrow's own hooks skip this session's turns end-to-end
  (see marrow/MAP.md §C3 guard), `--permission-mode bypassPermissions` (headless
  pipe has no one to approve tool prompts), `--resume <sid>` when resuming. Provider
  spec `[llm.claude_cli_cortex]`, tier `[cortex].tier` = `top` (opus).

## 5. day_log.md v2 zones (`cortex/day_log.py`)

Six zones bounded by stable HTML-comment markers so the file survives round-trips:

- **First** — cortex-written 3-5 action lines; preserved byte-for-byte on re-render
  (`_extract_bounded`), cortex overwrites it herself during a wake (not wired by
  code — a future wake writes into this zone via the marrow session, not day_log.py).
- **Status** — render-only, rebuilt every `update()`: last-seen line (`ct_activity`,
  local-day UTC-window bounded — see below), top usage category today
  (`ct_category_usage`), collector health (`ct_collector_log`, latest row per
  source).
- **Flow** (display title, was Today; marker IDs unchanged — `TODAY_START/END`)
  — one time axis: geofence rows (`ct_geofence`, `HH:MM [event]`) +
  self-authored tl rows (`events WHERE role='tl'`, A2r), re-sorted by leading
  `HH:mm` (`_sort_key`) so a late tl write self-heals into position. Pure
  DB→render, one-way — no reconcile (Decided 07-03 eve HARD). Calendar rows not
  wired yet (schedule.py ownership moves here at C6) — omitted honestly, not faked.
- **Tasks** (display title, was Reminders; marker IDs unchanged —
  `REMINDERS_START/END`) — subnote "task pool, not nag triggers — coax only".
  **Track** — honest placeholders (`DEFAULT_REMINDERS_BODY`,
  `DEFAULT_TRACK_BODY`) until a reminder collector and category-bucket/sleep
  inference config exist (tail block).
- **Stellan's Notes** — cortex free text; everything after the marker carried over
  byte-for-byte on re-render (`_split_notes`), her edits never clobbered.
- **tz correctness** (0ea536d): `_utc_day_bounds` computes the local calendar day's
  UTC `[start, end)` window rather than a naive `ts LIKE 'YYYY-MM-DD%'` prefix match
  — DB timestamps are UTC, so pre-10:00 Melbourne rows would otherwise land under
  the previous UTC date. Used by `_last_seen_line` and `_tl_rows_today`.
- `new_day(path, date)` always overwrites (caller must archive first). `archive`
  moves the file as-is (no compression) to `day_log_archive_dir/<L1-date>.md`,
  falls back to `-N` suffix if the destination already exists (never clobbers a
  real archived day with a blank re-archive from a failed retry).

## 6. Bulletin (`cortex/bulletin.py`)

- `gather` — thin DB read for `now`'s local date: last `ct_activity` row, top
  `ct_category_usage` row, `events` count today, folds in caller-supplied facts
  (pacemaker `decision`, `cal_next_3h` — not wired, `expect_reply_state`). Also
  reads `ct_rate_limit` (kv table, marrow-side writer) and the last N
  user/assistant `events` pairs (07-04).
- `render` — pure (no I/O): `Now:` / `Trigger:` (decision explanation or trigger
  facts or "none") / `Last activity:` / `Calendar (3h): none` (honest — schedule.py
  ownership transfers at C6) / `Usage today:` / `Budget:` (07-04, battery gauge)
  / `Counts:` / `Expect-reply:` / forced replay section. Hard cap `MAX_CHARS`
  default 2000, config `[bulletin].max_chars` overridable.
- **Budget gauge** (07-04) — `_rate_limit_kv` reads `ct_rate_limit` (flat
  key/value/updated_at, owned by a marrow-side writer parsing
  `rate_limit_event` off the cortex stream — not yet landed at write time of
  this reader). Key contract assumed here (`five_hour_pct`/`five_hour_reset_at`,
  `seven_day_pct`/`seven_day_reset_at`, `window_tokens`) — reconcile against
  the real writer once it ships. Table/keys missing → honest "Budget: no data".
- **Forced replay** (07-04) — `_replay_pairs` reads the last
  `[bulletin].replay_pairs` (default 3) user/assistant `events` pairs (role
  'tl' and non-conversation rows excluded — tool calls already stripped by
  marrow's `transcript.clean` before archiving). Per-message truncated to
  `[bulletin].replay_pair_chars` (default 240). Decided 07-04: never rely on
  cortex self-serve recall queries for this.

## 7. Symlinks, config, install (`cortex/symlinks.py`, `config.py`, `install.py`)

- `symlinks.ensure_all` — one-time idempotent: creates `wishlist.md` with a minimal
  header if missing (never overwrites — pure append-only md, her hand edits are
  source of truth), symlinks `day_log.md` + `wishlist.md` into
  `paths.ny_db_pages` (default `~/Desktop/NY/db-pages`). Refuses to clobber a
  non-symlink target at either destination (`FileExistsError`).
- Config loader `config.py:load` — tolerant TOML load from
  `~/.config/marrow/cortex.toml` (override `CORTEX_CONFIG` env); missing file or
  keys fall back to `_DEFAULTS`, deep-merged per section. All path helpers
  (`marrow_db_path`, `day_log_path`, `cortex_home`, `wishlist_path`, etc.) apply
  the same "empty string → documented default" pattern.
- `install.py` — registers 2 launchd plists (`gui/<uid>` domain, mirrors marrow's
  plist pattern): `com.cortex.collect-tick` and `com.cortex.pacemaker-tick`, both
  under `deploy/`, template tokens (`__VENV_PYTHON__`, `__COLLECT_INTERVAL_SEC__`,
  etc.) resolved from `config.load()[tick]` at install time — cadence stays
  config-driven, never hand-edited into the plist. `python -m cortex.install
  remove` unloads + deletes both.
- **Safety default**: `pacemaker.dry_run = true` — wake decisions are recorded to
  `ct_wake_log` but `pacemaker_tick.py` never calls `run_wake` while dry_run is on;
  flip to false only once outbound (C5) exists.

## 8. Status

- 122 tests passing (`.venv/bin/python -m pytest -q`, 2026-07-04) — collectors,
  pacemaker core/gates/desire/triggers/expect-reply, integration layer (synthetic
  `audit_log` fixtures), day_log v2 zones, wake runner (`caller` injectable so tests
  never spawn a real claude process), bulletin (incl. budget gauge + replay).
- Shipped: C1 collectors, C2 pacemaker core + integration wiring, C3 bulletin +
  day_log v2 + wake runner + symlinks + timeout unification, C4 Block1 (Flow/Tasks
  renames, tick-path render, bulletin budget gauge + forced replay).
- Not yet wired: event-sourced triggers (`context["events"]` always `[]`),
  `cal_busy`/`at_home` from real geofence/calendar data (config defaults only),
  expect-reply `start()` call site (no outbound exists), Tasks/Track zone data,
  reminder collector, goals-driven Track rendering (goals table lives in marrow,
  C1 said "Track renderer reads goals" — not yet read from cortex side). Budget
  gauge key contract (`ct_rate_limit`) unverified against the real marrow writer.
- Health/geofence collectors are feature-flagged off by default
  (`config[health].enabled` / `config[geofence].enabled` = false) — no export
  shape/file exists yet to point them at.
