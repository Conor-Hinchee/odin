---
name: odin
description: The Odin Allfather agentic loop. Runs complex development and refactoring tasks through five tracked phases (Huginn plan, Thor and Valkyrie subagent execution, Heimdall verification and pre-PR review gate, Hlidskjalf retrospective, human sign-off), with a live HTML state file per task, a local dashboard at 127.0.0.1:8788, append-only iteration history, contextual diff review, desktop notifications, and a reactive bridge that wakes the session when the human approves or rejects in the dashboard. Use when the user invokes /odin, asks to run a task "with Odin", or asks to boot, start, restart or check the Odin server or dashboard.
---

# Odin: The Allfather Agentic Workflow

Odin runs a task through five phases and records every phase in a live HTML **state file**, `<workspace>/odin-states/<task-slug>.html`. A small local server (`127.0.0.1:8788`) serves those files and a dashboard of every Odin project, and lets the human approve, revise or reject from the browser.

Scripts live in `${CLAUDE_SKILL_DIR}/scripts/`. In the commands below, `ODIN=${CLAUDE_SKILL_DIR}/scripts`.

| Script | Use |
|---|---|
| `odin-server.py` | Dashboard + state server (`--dir <root> --port 8788`) |
| `odin-state.py` | Phase updates, iterations, diffs, wakeups, deltas, staging. See [references/cli.md](references/cli.md) |
| `odin-isolate.py` | Worktree + port allocation for configured repos. See [references/isolation.md](references/isolation.md) |
| `odin-notify.sh` | Desktop notification + chime (click opens the task page via terminal-notifier on macOS) |

Optional settings (repos, ports, telemetry, skill pills, PR telemetry, staging) come from `~/.config/odin/config.json` or `<workspace>/.odin/config.json`. Run `python3 "$ODIN/odin_config.py"` to print the effective config. Everything works with no config.

---

## Ground rules (MANDATORY)

1. **One state file per task.** Store it at `<workspace>/odin-states/<task-slug>.html`, using a descriptive kebab-case slug (`checkout-form-validation`, `migrate-auth-to-jwt`). Create the directory if needed. Before creating a file, list `odin-states/` and resume an existing file if this is the same task. Never write a shared root `odin_state.html`.
2. **Start from the template.** New state files start from [references/state-template.html](references/state-template.html). Fill in the title and slug, and stamp `<body>` with what you know:
   ```html
   <body data-branch="feat/<task-slug>" data-worktree="<absolute repo or worktree path>"
         data-app-port="<port or empty>" data-backend-port="<port or empty>"
         data-session-id="<session id>">
   ```
   The session id appears in your context as `Odin session id: <id>` (added by the plugin's SessionStart hook). Omit the attribute if it is not there.
3. **Ensure the server is running** when you start a project or the user asks to boot Odin:
   ```bash
   lsof -nP -iTCP:8788 -sTCP:LISTEN || nohup python3 "$ODIN/odin-server.py" --dir "<odin root>" >/tmp/odin-server.log 2>&1 &
   ```
   `<odin root>` is the configured `root`, or a parent folder of your repos (the server scans three levels deep for `odin-states/`). If the user asked to boot Odin, also open `http://127.0.0.1:8788/` (`open` on macOS, `xdg-open` on Linux).
4. **Show the links** at the top of every phase update:
   - `[🌐 Open <task-slug>](http://127.0.0.1:8788/<path-from-root>/odin-states/<task-slug>.html)`
   - `[🏛️ Odin Dashboard](http://127.0.0.1:8788/)`
   - the state file path
5. **Update in real time.** Mark a phase `active` when it starts, and `done`, `reject` or `awaiting_human` the moment it ends, using `odin-state.py update-phase`. Never batch status updates at the end of a turn. Never overwrite earlier iteration cards: `update-phase` only touches the current `data-iteration="N"` card.
6. **Notify on every phase change**, including every pause for questions:
   ```bash
   sh "$ODIN/odin-notify.sh" "<Phase name>" "<one-line status>" "<phase|awaiting_human|done|reject>" "odin-states/<task-slug>.html"
   ```
   Use `phase` for phase 1 and 2 transitions, `awaiting_human` when pausing for input, `done` when phase 3 or 4 completes, and `reject` when Heimdall rejects. Always pass the state file: clicking the banner then opens the task's page, and the banner is skipped when an open Odin page with browser notifications allowed already shows the alert.
7. **Pausing for the human.** When you stop to ask questions or wait for approval:
   - set the blocked section to `data-status="awaiting_human"`, and list the exact questions in `<div class="open-questions-box"><ol><li>…</li></ol></div>` inside it;
   - ask them in chat with AskUserQuestion as well, so the user can answer in either place;
   - arm the wakeup watcher (below) before ending your turn.
8. **Closing the loop (first tool call).** When the user answers questions, approves, or rejects, your **first** tool call that turn moves the paused phase out of `awaiting_human` with `update-phase`. On a rejection or follow-up feedback after phase 4 or 5, the first call is `step-back` (see phase 5).

### Reactive wakeup bridge (`wait-for-human`)

Whenever you pause at `awaiting_human` (phase 1 plan review, the phase 3 pre-PR gate, or phase 5 sign-off), start the watcher with the Bash tool using `run_in_background: true`:

```bash
python3 "$ODIN/odin-state.py" wait-for-human \
  --file "odin-states/<task-slug>.html" \
  --phase "<huginn-plan|heimdall-validation|human-validation>" \
  --session-id "<session id>"
```

The dashboard then shows **🟢 PARTNER TURN**. When the human clicks Save & Launch Phase 2, Ask Huginn to Revise, Approve Code Diff & Open PR, Request Code Changes, or Sign Off / Reject, the watcher prints a wakeup JSON (`decision`, `notes`, `selectedSkills`) and exits. Claude Code re-invokes you when a background command exits, so the session resumes without copy-pasting. If `selectedSkills` lists skills (for example `/code-review`), invoke them at the start of the resumed turn. If the user answers in chat instead, stop the watcher.

---

## Workflow phases

```mermaid
graph TD
    A["User Prompt"] --> B["Phase 1: Huginn (Blueprint)"]
    B -->|Open Questions| Q["❓ Awaiting Human"]
    Q -->|Answered| C["Phase 2: Thor & Valkyries (Execution)"]
    B -->|No ambiguity| C
    C --> D["Phase 3: Heimdall (Verification & Pre-PR Gate)"]
    D -->|DONE| E["Phase 4: Hlidskjalf (Retrospective & Harness Deltas)"]
    D -->|REJECT| C
    E --> H["Phase 5: Human Validation (Sign-Off)"]
    H -->|Reject / follow-up → step-back| C
    H -->|Approved| Z["✅ Cycle Complete"]
    E -.->|harness_delta_proposals| M["🧠 Mímir Harness Audit"]
```

### Phase 1: Huginn (plan)
- Analyze the request and the codebase. Bootstrap the state file, start the server, notify.
- If the user has configured repos, lock in an isolated branch, worktree and ports with `odin-isolate.py allocate` ([references/isolation.md](references/isolation.md)) and stamp them on `<body>`. Otherwise record the current repo and branch.
- Write into `#huginn-plan`:
  1. **Environment**: branch, worktree, ports.
  2. **Plan**: concrete steps ordered by dependency.
  3. **Testing strategy**: the exact test, lint and type-check commands.
  4. **Definition of Done**: an unambiguous checklist.
  5. **Parallelism**: whether parts can run as parallel Valkyries, with a disjoint file-ownership matrix if so.
- **Blueprint review.** The human can co-edit the plan inline in the state file, attach `/skill` pills, and click **⚡ Save Edits & Launch Phase 2** (`approve_huginn`) or **🪶 Ask Huginn to Revise** (`revise_huginn`). For a non-trivial plan, or whenever there are open questions, pause at `awaiting_human` (ground rule 7) and arm the watcher. On `approve_huginn`, re-read `#huginn-plan` from disk first, because the human may have edited it.

### Phase 2: Thor and the Valkyries (execute)
- Mark `#thor-execution` active. Make the changes and run the tests inside the assigned worktree.
- **Single-domain work**: Thor (you) does it directly.
- **Independent parallel work**: dispatch Valkyries as subagents with the Agent tool, sent in one message so they run concurrently. Each gets an explicit, non-overlapping file list. Every Valkyrie prompt includes:
  > *GIT GUARDRAIL: You are working in `<worktree>` on branch `<branch>`. Edit ONLY these files: `<file-list>`. NEVER run `git commit`, `git checkout`, `git switch`, `git pull`, `git reset`, `git stash`, or `git clean`. Leave verified edits in the working tree for Thor to commit.*

  Valkyrie roles: **Brunhild** (core logic and integrations), **Gunnr** (tests and fixtures), **Kara** (refactors, renames, lint sweeps), **Sigrun** (schemas, contracts, tool registrations).
- **Thor is the only committer.** Stage and commit at verified checkpoints.
- Record what changed in the iteration card, mark the phase done, and notify.

### Phase 3: Heimdall (verify + pre-PR review gate)
- Check the work against the testing strategy and Definition of Done.
- When code changed, render the contextual diff in the state file:
  ```bash
  python3 "$ODIN/odin-state.py" update-phase --file "odin-states/<task-slug>.html" \
    --phase heimdall --status awaiting_human \
    --title "🛡️ Iteration N — Heimdall Verification & Pre-PR Review" \
    --summary "Tests and linters pass. Review the diff to approve opening the PR." \
    --with-diff --diff-context 12 \
    --diff-rationale "<what changed and why>" \
    --file-note "<path>=<why this file changed>" \
    --html "<test and lint results>"
  ```
  The review shows 12 lines of context, a rationale banner, per-file notes, split and unified views with word-level highlights, and line or whole-file comments.
- **Pre-PR gate.** If tests pass and no PR exists yet, do not skip ahead. Pause at `awaiting_human`, ask the user to approve opening the PR, and arm the watcher. After approval, mark the phase active (first call), push the branch, and open the PR. Then run `update-phase --phase heimdall --status done --with-diff --pr-url "<url>"`, which stamps `data-pr-url` and the 🔗 PR badge.
- **PR body.** Include a `### Prompt Summary` bullet list of the user's prompts. If the user has a `pr` block in their Odin config, the plugin's PreToolUse hook adds the configured label, a session telemetry section and the verbatim prompts, and asks the user to confirm the rewritten command. Never write a bare `#<digits>` for numbered rules or steps ("Rule 12", not "Rule #12"), because GitHub links those to unrelated issues.
- **REJECT**: mark `reject`, notify with `reject`, and route the specific failures back to phase 2.

### Phase 4: Hlidskjalf (retrospective)
- In `#hlidskjalf-judge`, review plan adherence, subagent efficiency, and what slowed the task down.
- If the task needed more than one iteration, had a Heimdall rejection, or exposed a reusable gap in a skill, rule or script, record a **harness delta proposal**:
  ```bash
  python3 "$ODIN/odin-state.py" propose-delta --file "odin-states/<task-slug>.html" \
    --id "delta-<task-slug>-01" --category "<skill|rule|script|test-gate>" \
    --target-path "<file the fix belongs in>" \
    --failure-pattern "<what went wrong or what gap was exposed>" \
    --proposed-diff "<concise diff or rule text>"
  ```
  Proposals stay in the state file's ledger and do not touch the feature branch. They are reviewed later through the dashboard's **🧠 Mímir Harness Audit** or `mimir-audit`, and **Approve & Spawn Delta Ticket** turns one into its own Odin task.
- Mark done and notify.

### Phase 5: Human validation (sign-off)
- A task that changed a git repo needs a linked PR before sign-off, unless the user waived it at the pre-PR gate.
- Set `#human-validation` to `awaiting_human`, notify, and arm the watcher.
- **Rejection or follow-up feedback.** Whether it comes from the dashboard or in chat after phase 4 or 5, your **first** tool call is:
  ```bash
  python3 "$ODIN/odin-state.py" step-back --file "odin-states/<task-slug>.html" \
    --reason "<rejection note or follow-up summary>" \
    --root-cause "<why the previous iteration missed this>"
  ```
  This collapses earlier iteration cards (keeping them intact), starts iteration N+1 in phase 2, and resets phases 3 and 4 to pending. Then notify `"Phase 2: Thor — Iteration N" … "phase"` and run phases 2 to 5 again.
- On final approval, mark `#human-validation` done.

---

## More

- **Staging sandbox (`:8789`)**: lets you debug or improve Odin itself without disturbing live projects. See [references/staging.md](references/staging.md).
- **Command reference**: [references/cli.md](references/cli.md).
