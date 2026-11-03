# Running Odin in Antigravity

The workflow is the same; only the tool names and the script path differ.

| In SKILL.md | In Antigravity |
|---|---|
| `${CLAUDE_SKILL_DIR}/scripts/` | `<skill-dir>/scripts/`, next to `SKILL.md` |
| `AskUserQuestion` | `ask_question` |
| `Agent` (Valkyrie dispatch, sent in one message) | `invoke_subagent` |
| `Bash` with `run_in_background: true` (the watcher) | `run_command` with `WaitMsBeforeAsync: 500` |

- Antigravity also re-invokes the session when a background command exits, so the wakeup bridge works unchanged.
- The root `hooks.json` wires the same SessionStart, PR and progress hooks to `run_command` and `invoke_subagent`. If the `Odin session id` line is missing from context, omit `data-session-id` and `--session-id`.
