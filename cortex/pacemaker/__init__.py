"""Pacemaker: pure decision core for cortex wake cycle.

Composed of triggers (wake reasons) -> gates (suspend/allow) -> core.tick()
(single entry point). No I/O, no datetime.now() calls; clock and rng are
always injected by the caller.
"""
