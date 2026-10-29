#!/usr/bin/env python3
"""Claude Code & Antigravity SessionStart hook: tell the session its own id.

Odin stamps this id on state files (data-session-id) and passes it to
`odin-state.py wait-for-human --session-id`, so the dashboard can find the
session transcript.

- Claude Code: plain stdout from a SessionStart hook is added to context.
- Antigravity: outputs a JSON `injectSteps` systemMessage object.
"""

import json
import sys

try:
    payload = json.loads(sys.stdin.read() or "{}")
except ValueError:
    payload = {}

if not isinstance(payload, dict):
    payload = {}

if "conversationId" in payload or "workspacePaths" in payload or "transcriptPath" in payload:
    cid = str(payload.get("conversationId") or "").strip()
    if cid:
        print(
            json.dumps(
                {
                    "injectSteps": [
                        {
                            "systemMessage": {
                                "systemMessage": f"Odin session id: {cid}"
                            }
                        }
                    ]
                }
            )
        )
    else:
        print("{}")
else:
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        print(f"Odin session id: {session_id}")
