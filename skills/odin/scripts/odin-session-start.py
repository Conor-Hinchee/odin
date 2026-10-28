#!/usr/bin/env python3
"""Claude Code SessionStart hook: tell the session its own id.

Odin stamps this id on state files (data-session-id) and passes it to
`odin-state.py wait-for-human --session-id`, so the dashboard can find the
session transcript. Plain stdout from a SessionStart hook is added to context.
"""

import json
import sys

try:
    payload = json.loads(sys.stdin.read() or "{}")
except ValueError:
    payload = {}
session_id = str(payload.get("session_id") or "").strip()
if session_id:
    print(f"Odin session id: {session_id}")
