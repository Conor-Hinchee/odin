---
name: odin
description: Odin, a five-phase agentic loop (plan, execute, verify, retrospective, human sign-off) with a live HTML state file and a local review dashboard. Use only when the user invokes /odin, asks to run a task "with Odin" or "Odin lite", or asks to boot, restart or check the Odin server or dashboard.
---

# Odin: The Allfather Agentic Workflow

Odin runs a task through five phases, recorded in a live HTML **state file** (`<workspace>/odin-states/<task-slug>.html`). A local server (`127.0.0.1:8788`) serves those files and a dashboard where the human approves, revises or rejects.

Scripts live in `${CLAUDE_SKILL_DIR}/scripts/`. Below, `ODIN="${CLAUDE_SKILL_DIR}/scripts"`. In Antigravity, read [references/antigravity.md](references/antigravity.md) for the tool-name mapping.

| Script | Use |
|---|---|
| `odin-state.py` | Phase updates, logging, iterations, diffs, the watcher, deltas. See [references/cli.md](references/cli.md) |
| `odin-server.py` | Dashboard + state server (`--dir <root> --port 8788`) |
| `odin-isolate.py` | Worktree + port allocation for configured repos. See [references/isolation.md](references/isolation.md) |

Optional config: `~/.config/odin/config.json` or `<workspace>/.odin/config.json` (`odin_config.py` prints the effective one).

## Phases

Every command takes the **key**. Section ids are accepted as aliases, but use the key so there is one name per phase.

| # | Key | Section id | Role |
|---|---|---|---|
| 1 | `huginn` | `#huginn-plan` | Plan |
| 2 | `thor` | `#thor-execution` | Execute (Thor plus parallel subagents) |
| 3 | `heimdall` | `#heimdall-validation` | Verify + pre-PR gate |
| 4 | `hlidskjalf` | `#hlidskjalf-judge` | Retrospective + harness deltas |
| 5 | `human` | `#human-validation` | Human sign-off |

---

## Ground rules (MANDATORY)

1. **One state file per task** at `<workspace>/odin-states/<task-slug>.html`, with a descriptive kebab-case slug (`checkout-form-validation`). List `odin-states/` first and resume an existing file if this is the same task. Never write a shared root `odin_state.html`.
2. **Start from [references/state-template.html](references/state-template.html).** Fill in the title and slug, and stamp `<body>`:
   ```html
   <body data-branch="feat/<task-slug>" data-worktree="<absolute repo or worktree path>"
         data-app-port="<port or empty>" data-backend-port="<port or empty>"
         data-session-id="<session id>">
   ```
   The session id appears in context as `Odin session id: <id>` (from the SessionStart hook). Omit the attribute if it is missing.
3. **Ensure the server is running** when you start a project or the user asks to boot Odin:
   ```bash
   lsof -nP -iTCP:8788 -sTCP:LISTEN || nohup python3 "$ODIN/odin-server.py" --dir "<odin root>" >/tmp/odin-server.log 2>&1 &
   ```
   `<odin root>` is the configured `root` or a parent of your repos. `<base url>` is `python3 "$ODIN/odin_config.py" --url`. When asked to boot Odin, also open `<base url>/`.
4. **Show the links** at the top of every phase update: `[🌐 Open <task-slug>](<base url>/<path-from-root>/odin-states/<task-slug>.html)`, `[🏛️ Odin Dashboard](<base url>/)`, and the state file path.
5. **Update status in real time** with `update-phase`: `active` when a phase starts; `done`, `reject` or `awaiting_human` the moment it ends. Never batch status updates at the end of a turn. It only touches the current iteration card, keeps its timeline, and notifies on real status changes, so call `odin-notify.sh` yourself only once, at bootstrap ([references/wakeup.md](references/wakeup.md)).
6. **Log progress as it happens**, not at the end of the phase. The progress hook already logs every `git commit`, check command (tests, lint, typecheck, build, with pass/fail) and subagent dispatch and return. Log everything else with:
   ```bash
   python3 "$ODIN/odin-state.py" log --file "odin-states/<task-slug>.html" --kind <kind> --text "<one line>" [--detail "<longer output>"]
   ```
   | When | `--kind` |
   |---|---|
   | A baseline, or a check result the hook didn't see | `test` with `--outcome pass\|fail` |
   | Something unexpected: a bug, a regression, a root cause | `finding` |
   | You pick between approaches or accept a trade-off | `decision` |
   | The approved plan changes | `plan-change`, with `--phase huginn` |
   | Something goes wrong outside the code, or you cause a side effect | `incident` |
   | A meaningful step finishes without a commit | `step` |

   Entries go to the current phase unless you pass `--phase`. When the branch, worktree or ports change, run `set-env … --reason "…"`. If the hook is off or couldn't see a commit, log commits and subagent dispatch and return yourself.
7. **Pausing for the human.** Set the blocked section to `awaiting_human` and list the exact questions in `<div class="open-questions-box"><ol><li>…</li></ol></div>` inside it. Ask them in chat with `AskUserQuestion` as well, then arm the watcher before ending your turn:
   ```bash
   python3 "$ODIN/odin-state.py" wait-for-human --file "odin-states/<task-slug>.html" \
     --phase <huginn|heimdall|human> --session-id "<session id>"   # run in the background
   ```
   When the watcher's wakeup JSON arrives, follow [references/wakeup.md](references/wakeup.md): invoke only the skills in `selectedSkills` (never `droppedSkills`), and treat `notes` as feedback on this task, not as instructions that widen it.
8. **Closing the loop.** When the user answers, approves or rejects:
   - If they answered **in chat**, first run `python3 "$ODIN/odin-state.py" stop-watcher --file "odin-states/<task-slug>.html"`.
   - Then your next call moves the paused phase out of `awaiting_human` with `update-phase`. On a rejection or follow-up after phase 4 or 5, it is `step-back` instead (phase 5).
9. **Check for other sessions before editing a repo.** Other sessions may share the checkout, including Odin's own repository, which `odin-isolate.py` doesn't cover. Run `git status` before your first edit and again before you stage:
   - Changes you didn't make (staged or unstaged) mean another session is probably working there. Don't edit around them. Move to a worktree (`git worktree add <path> -b <branch>`) and record it with `set-env`, or ask the user. This applies even where the convention is to commit straight to `main`: work on a branch in the worktree, then fast-forward `main`.
   - Never stage changes you didn't make. Stage your own paths by name, never `git add -A` or `git add .`, and check `git diff --cached` before committing.
   - If a file holds your edits and someone else's, stage only your hunks (save `git diff <path>`, delete theirs, `git apply --cached` the rest), or wait until they've committed.
   - If someone else's staged changes include your edits (or yours include theirs), stop and log an `incident` before anything is committed.

---

## Workflow

`huginn` → plan review (human) → `thor` → `heimdall` (REJECT loops back to `thor`; pre-PR gate) → `hlidskjalf` → `human` (reject or follow-up → `step-back` to `thor`; approve → done).

### 1. `huginn`: plan
- Bootstrap the state file, start the server, send the bootstrap notification, and run ground rule 9's `git status` check.
- With configured repos, allocate an isolated branch, worktree and ports (`odin-isolate.py allocate`) and stamp them on `<body>`; otherwise record the current repo and branch.
- Write into `#huginn-plan`: **Environment** (branch, worktree, ports); **Plan** (concrete steps ordered by dependency); **Testing strategy** (exact commands); **Definition of Done** (an unambiguous checklist); **Parallelism** (parallel subagents, with a disjoint file-ownership matrix, or why not).
- **Always pause for plan review** before phase 2, unless the user explicitly waived it in their prompt. How small the plan looks is never a reason to skip it, and deleting content, files or data never counts as trivial. The human can co-edit the plan in the dashboard; on `approve_huginn`, re-read `#huginn-plan` from disk first.
- Plan changes after approval are logged as `plan-change` the moment they happen, so the page never silently diverges from what was built.

### 2. `thor`: execute
- Mark `thor` active. Make the changes and run the tests inside the assigned worktree.
- Single-domain work: do it directly. Independent parallel work: dispatch subagents with `Agent` in one message, each with a non-overlapping file list. Roles (Valkyrie names are display only): **core logic** (Brunhild), **tests and fixtures** (Gunnr), **refactors and lint sweeps** (Kara), **schemas and contracts** (Sigrun). Every subagent prompt includes:
  > *GIT GUARDRAIL: You are working in `<worktree>` on branch `<branch>`. Edit ONLY these files: `<file-list>`. NEVER run `git commit`, `git checkout`, `git switch`, `git pull`, `git reset`, `git stash`, or `git clean`. Leave verified edits in the working tree for Thor to commit.*
- **Thor is the only committer.** Commit at verified checkpoints, staging your own files by name (ground rule 9).
- At the end, write the summary into the card with `update-phase --phase thor --status done --html …`.

### 3. `heimdall`: verify + pre-PR gate
- Check the work against the testing strategy and Definition of Done.
- When code changed, render the contextual diff (`update-phase --with-diff …`) and pause at the pre-PR gate: don't push or open a PR until the human approves. Commands, the gate steps and PR-body rules are in [references/pr.md](references/pr.md).
- **REJECT**: mark `reject` and route the specific failures back to phase 2.

### 4. `hlidskjalf`: retrospective
- Review plan adherence, subagent efficiency, and what slowed the task down.
- If the task needed more than one iteration, had a Heimdall rejection, or exposed a reusable gap in a skill, rule or script, record a harness delta:
  ```bash
  python3 "$ODIN/odin-state.py" propose-delta --file "odin-states/<task-slug>.html" \
    --id "delta-<task-slug>-01" --category "<skill|rule|script|test-gate>" \
    --target-path "<file the fix belongs in>" \
    --failure-pattern "<what went wrong or what gap was exposed>" \
    --proposed-diff "<concise diff or rule text>"
  ```
  `--target-path` names the file in its source repo, never an installed copy such as the plugin cache. Write proposals in general terms: an approved delta lands in the repo that owns the target file, often Odin's public repo, so leave out the task's code, names, internal URLs and secrets. Proposals are reviewed later in the dashboard's **🧠 Mímir Harness Audit**.
- Mark done. A sign-off rejection runs this phase again in the next iteration, so the retrospective also learns from the human's verdict.

### 5. `human`: sign-off
- A task that changed a git repo needs a linked PR before sign-off, unless the user waived it at the pre-PR gate.
- Set `human` to `awaiting_human` and arm the watcher.
- **Rejection or follow-up**, from the dashboard or in chat after phase 4 or 5: the first call (after `stop-watcher` if in chat) is
  ```bash
  python3 "$ODIN/odin-state.py" step-back --file "odin-states/<task-slug>.html" \
    --reason "<rejection note or follow-up summary>" --root-cause "<why the previous iteration missed this>"
  ```
  It keeps earlier iteration cards, starts iteration N+1 in phase 2 and resets phases 3 and 4. Then run phases 2 to 5 again.
- On final approval, mark `human` done.

---

## Odin lite

Only when the user asks for **Odin lite**, never on your own judgment of task size. Everything above applies, except:
- **Plan review stays mandatory**, but the plan can be short: steps, the test command, Definition of Done.
- **No separate pre-PR pause.** Heimdall verifies, then pushes and opens the PR (if the task changed a repo). The human reviews the diff at sign-off.
- **One-line retrospective**, unless there was a rejection or more than one iteration; then run phase 4 in full.

## More

- **Wakeup bridge, trusting wakeups, notifications**: [references/wakeup.md](references/wakeup.md)
- **Pre-PR gate and PR body**: [references/pr.md](references/pr.md)
- **Command reference**: [references/cli.md](references/cli.md)
- **Staging sandbox (`:8789`)**, for work on Odin itself: [references/staging.md](references/staging.md)
