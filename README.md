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
3. Enable the marrow-side bridge: set `[cortex] enabled = true` in marrow's config.toml and list the shells that run as cortex shells in `[cortex] shells` (default `["cli"]`; mirror it in this repo's `[core] shells`), then restart the marrow watcher. This installs the MCP tools (`lie_down` for every shell, `say` for the cli shell; `wish` / `first` / `goal` everywhere) and the wake hooks.
4. Seed the cortex home dir `~/.config/marrow/cortex/` (configurable via `[paths] cortex_home`) — this is the resident session's cwd and inner world. Copy [templates/](templates/) there and customise names/paths:
   ```
   cp templates/*.md ~/.config/marrow/cortex/
   ```
   - `CLAUDE.md` — world rules + house rules for the resident session
   - `playbook.md` — activity menu (what to do when awake)
   - `notebook.md` — long-term memory, self-maintained
   - `handoff_template.md` — page template for the rolling log (per shell, `handoff-<shell>.md`; a page over `handoff_max_lines` is archived and a fresh page carries the unchecked todos + last lines)
   - `wishlist.md` — created automatically on first `wish`; template optional
   Everything else under cortex_home (wakeup_note, wake_state, handoff-cli.md, logs) is generated at runtime.
5. Install the launchd jobs (collect-tick + pacemaker-tick):
   ```
   .venv/bin/python -m cortex.install
   ```
   `python -m cortex.install remove` unloads them.

Ships with `pacemaker.dry_run = true` — pacemaker logs decisions without waking until you flip it.

## How it works

- Collectors (launchd, ~30 min) read macOS app-usage (plus optional geofence/health) into `ct_` tables on the shared marrow DB.
- Pacemaker (launchd, ~5 min) evaluates triggers (floor timer, self-schedule, affect flag) against the daily token budget gate and decides wake or stay down.
- A wake lands in a resident iTerm window running `claude` (fresh spawn, `--resume`, or a bell into the live window), with the wakeup note injected by marrow's hook. Headless marrow-subprocess call is the fallback.
- The session ends its wake itself via `lie_down(next_wake_min=N)` (0 = wake again immediately). While it stays up, every `silent_max_min` of user silence injects one free-round note + `[NEW ROUND]` line and re-arms the same timer — a perpetual cycle, never a forced sleep. A per-wake watchdog and a one-shot sentinel cover that cycle, token fuses, and exact-time wakes.

## Docs

- [DESIGN.md](DESIGN.md) — goals and outcomes.
- [MAP.md](MAP.md) — how each part works today.
