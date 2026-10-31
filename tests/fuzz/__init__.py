"""Seeded fuzz harness for LLM Bridge (standard library only).

``generators`` builds chaotic inputs from a ``random.Random``; ``harness`` holds
the sandbox, fake curses windows and invariant helpers; ``targets`` defines one
fuzz target per Bridge surface. The pytest suite (``tests/test_bridge_fuzz*.py``)
and the long-running runner (``scripts/bridge_fuzz.py``) call the same targets.
"""
