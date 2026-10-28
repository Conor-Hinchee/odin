# Odin ⚡

A Claude Code plugin that runs complex development tasks through a five-phase, Norse-themed agentic loop. Each task gets a live HTML state file, and a local dashboard lets you review, edit, approve or reject without leaving the browser.

| Phase | Role |
|---|---|
| 1. **Huginn** | Plans the work: steps, testing strategy, Definition of Done, parallelism. You can co-edit the plan inline before execution. |
| 2. **Thor & the Valkyries** | Executes, fanning independent work out to parallel subagents with disjoint file ownership. Thor is the only one who commits. |
| 3. **Heimdall** | Verifies against the Definition of Done and renders a contextual diff review (12-line context, rationale, per-file notes, split/unified view, comments). It pauses for your approval before opening a PR. |
| 4. **Hlidskjalf** | Runs a retrospective. Records "harness delta" proposals when a task needed retries, so recurring gaps get fixed at the source. |
| 5. **Human validation** | Gets your sign-off. A rejection steps back to a new iteration while keeping the full history. |

When Odin pauses for you, it arms a background watcher. Clicking **Approve**, **Revise** or **Reject** in the dashboard wakes the Claude Code session, with no copy-pasting.

## Install

```bash
claude plugin marketplace add Conor-Hinchee/odin
claude plugin install odin@odin
```

Then, in a session, run `/odin:odin <your task>`, or ask Claude to "boot up Odin" to start the dashboard at <http://127.0.0.1:8788/>.

Requirements: Python 3.10+, git, and `lsof`. macOS and Linux are supported.

### Notifications

Odin alerts you on every phase change in up to three places:

- **Browser.** Open the dashboard or a state page in Chrome (or any regular browser) and click **🔔 Enable Alerts**. Phase changes then show as browser notifications, and clicking one focuses the tab. Browsers embedded in other apps, such as the Claude Code browser pane, usually block notifications; there you get the in-page toast and chime instead.
- **Desktop banner.** On macOS, install [terminal-notifier](https://github.com/julienXX/terminal-notifier) (`brew install terminal-notifier`) so that clicking a banner opens the task's state page. Without it, Odin falls back to `osascript`, whose banners are credited to Script Editor and open it when clicked. On Linux, banners use `notify-send`.
- **No duplicates.** While an Odin page with browser notifications allowed is open, it covers its own task (the dashboard covers every task), and the desktop banner is skipped.

## Configure (optional)

Odin works with no config. To add repos for worktree and port isolation, a deployment-telemetry table, custom skill pills, PR telemetry, or a different port, copy [`odin.config.example.json`](odin.config.example.json) to one of these locations and edit it:

- `~/.config/odin/config.json` for all workspaces
- `<workspace>/.odin/config.json` for one workspace
- any path named in `$ODIN_CONFIG`

These locations are merged in that order, with later ones winning. To print the effective config:

```bash
python3 skills/odin/scripts/odin_config.py
```

| Key | Purpose |
|---|---|
| `root` | Folder the dashboard scans (three levels deep) for `odin-states/` |
| `ports` | `live` (8788) and `staging` (8789) |
| `repos` | Repos `odin-isolate.py` may allocate worktrees and ports for |
| `telemetry` | A command that prints a JSON array, shown as a table on the dashboard |
| `skill_pills` | Skills offered as one-click pills when approving a plan |
| `pr` | Turns on the PR hook: label, telemetry section, verbatim prompts, `Rule #12` → `Rule 12` |
| `staging` | Paths for the `:8789` sandbox used to develop Odin itself |

The PR hook is off unless `pr` is set. When it rewrites a command, it asks you to confirm instead of auto-approving.

## Run the dashboard at login (macOS, optional)

Create `~/Library/LaunchAgents/com.odin.server.plist` with `ProgramArguments` set to `python3 <path>/skills/odin/scripts/odin-server.py --dir <your root>`, with `RunAtLoad` and `KeepAlive` both true. Then run `launchctl load ~/Library/LaunchAgents/com.odin.server.plist`.

## Develop

```bash
git clone https://github.com/Conor-Hinchee/odin && cd odin
python3 -m unittest discover tests
claude plugin validate .
claude --plugin-dir .
```

Running from a checkout also enables the staging sandbox (see [skills/odin/references/staging.md](skills/odin/references/staging.md)).

## License

[MIT](LICENSE)
