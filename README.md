# cortex

Awake-presence layer: collectors gather signals → pacemaker decides when to wake → a resident Claude Code session runs the wake.

Assumes [marrow](../marrow) + synapse already installed and a Claude Code max plan.

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
3. Install the launchd jobs (collect-tick + pacemaker-tick):
   ```
   .venv/bin/python -m cortex.install
   ```
   `python -m cortex.install remove` unloads them.

Ships with `pacemaker.dry_run = true` — pacemaker logs decisions without waking until you flip it.

## Docs

- [DESIGN.md](DESIGN.md) — goals and outcomes.
- [MAP.md](MAP.md) — how each part works today.
