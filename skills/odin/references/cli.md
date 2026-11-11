# Odin command reference

All commands are `python3 "${CLAUDE_SKILL_DIR}/scripts/odin-state.py" <command> …`. File arguments are paths to `odin-states/<slug>.html`, given positionally or with `--file`.

| Command | What it does | Key options |
|---|---|---|
| `update-phase` | Update one phase's iteration card in place. It keeps the card's timeline, keeps its body when given only `--summary`, and notifies and logs a 🚦 entry only when the status changes | `--phase {huginn,thor,heimdall,hlidskjalf,human}` (or section ids), `--status {pending,active,done,reject,awaiting_human}`, `--title`, `--summary`, `--files a,b`, `--html` / `--html-file-fragment`, `--iteration N`, `--pr-url`, `--prompt-summary-item "<bullet>"` (repeatable; seeds `### Prompt Summary` curation in the Phase 3 Heimdall Pre-PR Gate) |
| `update-phase --with-diff` | Also render the contextual diff review | `--diff-context 12`, `--diff-rationale`, `--file-note "path=why"` (repeatable), `--repo-dir` (repeatable; defaults to `data-worktree`). A re-render without `--diff-rationale` or `--file-note` keeps the card's existing ones |
| `log` | Append an entry to a phase's timeline. No status change, no notification. Consecutive duplicates are skipped | `--kind {step,phase,commit,test,finding,decision,plan-change,valkyrie,incident,env}`, `--text` (required), `--detail`, `--outcome {pass,fail}`, `--phase` (defaults to the furthest active/awaiting phase), `--at <ISO time>` to record an event after the fact |
| `set-env` | Restamp `data-branch`/`data-worktree`/`data-*-port` and the header env bar, and log the change | `--branch`, `--worktree`, `--app-port`, `--backend-port`, `--reason` |
| `step-back` | Start iteration N+1 after a rejection or follow-up; earlier cards are kept | `--reason` (required), `--root-cause`, `--source {human,ci,heimdall}` |
| `wait-for-human` | Block until the human acts in the dashboard, then print the wakeup JSON and exit. One per task: a newer watcher stops the older one; refuses to start if stdout is `/dev/null` | `--phase`, `--session-id`, `--timeout`, `--consume-existing` |
| `friction-scan` | List the harness failures a phase 4 retrospective must answer, each with an id and evidence: `missing-pr` (Heimdall done, no PR linked), `gate-ignored` (a dashboard decision woke the session and the turn ended without acting on it), `gate-unwatched` (a pause at `awaiting_human` with no `wait-for-human`), `state-file-late` (project files read before the state file existed). Reads the state file and the local session transcript; nothing leaves the machine | `--transcript` (default: found from `data-session-id` under `~/.claude/projects`), `--json` |
| `update-phase --phase hlidskjalf --status done` | Blocks on open incidents and failed checks, and on every `friction-scan` finding whose id no delta or waiver names | `--incident-waiver "<id>: <reason>"`, `--transcript` |
| `propose-delta` | Record a phase 4 harness delta proposal | `--id` (required), `--category`, `--target-path`, `--failure-pattern`, `--proposed-diff`, `--priority {p0,p1,p2}` |
| `mimir-audit` | Summarize delta proposals across every state file under a root | `--dir`, `--format {markdown,json}` |
| `spawn-delta-ticket` | Turn a delta proposal into its own Odin state file | `--id` (required), `--dir` |
| `refresh-diff` | Re-render diff viewers and review scripts in a state file | |
| `snapshot-staging` / `promote-staging` | Staging sandbox, see [staging.md](staging.md) | `--repo-dir`, `--staging-dir`, `--no-server` |

## Server

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/odin-server.py" --dir <root> [--port 8788] [--host 0.0.0.0] [--cert odin.pem --key odin-key.pem]
```

- `GET /`: dashboard of every `odin-states/*.html` up to three levels below `<root>` (archived files in `odin-states/archive/` included).
- `GET /api/projects`, `/api/session-telemetry?id=…`, `/api/telemetry`, `/api/mimir/audit`, `/api/skills`.
- `GET /go?file=<absolute state file path>`: redirects to that state file's page (or `/` when the file is outside `<root>`). Desktop banners link here.
- `POST /api/presence {"scope": "*" | "<page path>"}`: open pages with browser notifications allowed ping this every 5s. `GET /api/presence?file=<path>` returns `covered: true` when the dashboard or that file's page pinged in the last 15s, and `odin-notify.sh` then skips its banner.
- `POST /api/projects/action`: archive, restore, update phases, wake the agent, allocate environments, stop dev ports, prune worktrees, staging.

## Files Odin writes next to state files

| File | Purpose |
|---|---|
| `.odin-watcher-<slug>.json` | `wait-for-human` is armed (shown as PARTNER TURN) |
| `.odin-wakeup-<slug>.json` | The human acted in the dashboard; consumed by `wait-for-human` |
| `.odin-paused.json` | Live watchers are paused |
| `.<slug>.html.lock` | Lock that serializes writes to a state file from the CLI, hooks and subagents |

## Config

`python3 "${CLAUDE_SKILL_DIR}/scripts/odin_config.py"` prints the merged config; `--url` prints just the live server's base URL. Keys: `root`, `ports`, `server` (`host`, `cert`, `key`, `allowed_hosts`), `repos`, `skill_pills`, `telemetry`, `pr`, `staging`, `progress`. See `odin.config.example.json` in the repository.

`telemetry.command` is any shell command that prints a JSON array of row objects. The dashboard shows it as a table, with one row per object, `name` as the row label, and `columns: [{key, label}]` choosing the columns. A row named `latest` becomes the reference that other rows are compared to.

`progress` controls the progress hook: `{"capture": true, "check_patterns": ["\\bnpm (run )?(test|lint|typecheck|build)\\b", …]}`. `check_patterns` are regexes searched in Bash commands, and a matching command is logged as a ✅/❌ `test` entry. Setting a list replaces the defaults, so include any defaults you want to keep.
