# cortex

Awake-presence layer: collectors gather signals → pacemaker decides when to wake → a resident Claude Code session runs the wake.

Assumes [marrow](../marrow) + synapse already installed and a Claude Code max plan. Window mode (default) additionally needs macOS + iTerm2.

## Setup

1. Clone into `~/CC-Lab/cortex` and create a venv (stdlib only, no deps):
   ```
   python3.11 -m venv .venv
   ```
2. Copy the config template and edit identity/paths:
   ```
   cp config.example.toml ~/.config/marrow/cortex.toml
   ```
   Override the path with the `CORTEX_CONFIG` env var if needed.
3. Enable the marrow-side bridge: set `[cortex] enabled = true` in marrow's config.toml, then restart the marrow watcher. This installs the MCP tools (`lie_down` / `wait` / `say` for the cortex session; `wish` / `first` / `goal` everywhere) and the wake hooks.
4. Seed the cortex home dir `~/.config/marrow/cortex/` (configurable via `[paths] cortex_home`) — this is the resident session's cwd and inner world:
   - `CLAUDE.md` — world rules + house rules for the resident session
   - `playbook.md` — activity menu (what to do when awake)
   - `notebook.md` — long-term memory, self-maintained
   - `handoff_template.md` — daily journal template (new page each day, old pages auto-archived)
   - `wishlist.md` — created automatically on first `wish`; template optional
   Start from the templates and customise names/paths; everything else under cortex_home (wakeup_note, wake_state, handoff.md, logs) is generated at runtime.
5. Install the launchd jobs (collect-tick + pacemaker-tick):
   ```
   .venv/bin/python -m cortex.install
   ```
   `python -m cortex.install remove` unloads them.

Ships with `pacemaker.dry_run = true` — pacemaker logs decisions without waking until you flip it.

## How it works

- Collectors (launchd, ~30 min) read macOS app-usage (plus optional geofence/health) into `ct_` tables on the shared marrow DB.
- Pacemaker (launchd, ~5 min) evaluates triggers (floor timer, self-schedule, affect flag) against gates (night window, daily token budget) and decides wake or stay down.
- A wake lands in a resident iTerm window running `claude` (fresh spawn, `--resume`, or a bell into the live window), with the wakeup note injected by marrow's hook. Headless marrow-subprocess call is the fallback.
- The session ends its wake itself via `lie_down(next_wake_min=N)` (or `wait(N)` to linger); a per-wake watchdog and a one-shot sentinel cover silence, token fuses, and exact-time wakes.

## Docs

- [DESIGN.md](DESIGN.md) — goals and outcomes.
- [MAP.md](MAP.md) — how each part works today.
