Arm your ear, then lie down. Do nothing else until a signal fires.

- Monitor "cortex wake signal": run `tail -n 0 -f {signal_log}`, persistent across turns.
- Its lines are system signals, never the user: WAKE = read your wakeup note and act; NUDGE = wrap up and lie down.
- Read handoff (阿屿の碎碎念): ~/.config/marrow/cortex/handoff.md.
- Lie down (lie_down tool).
- Keep the ear armed all session. Monitor gone after /clear or fresh session -> re-arm before lie down.
