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

Odin has no version number: every commit on `main` is a new release. To get updates automatically, run `/plugin`, open **Marketplaces**, select **odin** and choose **Enable auto-update**. A running session keeps the version it loaded until you run `/reload-plugins`.

To work on Odin itself, add your checkout as the marketplace instead (`claude plugin marketplace add /path/to/odin`). Plugins from a local marketplace load in place, so your edits apply on the next session or `/reload-plugins`.

Then, in a session, run `/odin:odin <your task>`, or ask Claude to "boot up Odin" to start the dashboard at <http://127.0.0.1:8788/>.

Requirements: Python 3.10+, git, and `lsof`. macOS and Linux are supported.

### Notifications

Odin alerts you on every phase change in up to three places:

- **Browser.** Open the dashboard or a state page in Chrome (or any regular browser) and click **🔔 Enable Alerts**. Phase changes then show as browser notifications, and clicking one focuses the tab. Browsers embedded in other apps, such as the Claude Code browser pane, usually block notifications; there you get the in-page toast and chime instead.
- **Desktop banner.** On macOS, install [terminal-notifier](https://github.com/julienXX/terminal-notifier) (`brew install terminal-notifier`) so that clicking a banner opens the task's state page. Without it, Odin falls back to `osascript`, whose banners are credited to Script Editor and open it when clicked. On Linux, banners use `notify-send`.
- **No duplicates.** While an Odin page with browser notifications allowed is open, it covers its own task (the dashboard covers every task), and the desktop banner is skipped.

### Access from your network

By default the server only listens on `127.0.0.1`. To open the dashboard from a phone or another computer on the same network, add a `server` block to `~/.config/odin/config.json` and restart the server:

```json
"server": { "host": "0.0.0.0" }
```

The server prints the network URL, for example `http://192.168.1.20:8788/`. Pages render and every button works over plain HTTP.

**Optional: HTTPS, for browser notifications on other computers.** Browsers only allow notifications on a secure origin. iPhones don't support Odin's page notifications at all, so this only helps on other desktops and laptops.

1. Create a certificate other devices can trust:
   ```bash
   brew install mkcert
   mkcert -install
   mkdir -p ~/.config/odin/certs
   mkcert -cert-file ~/.config/odin/certs/odin.pem -key-file ~/.config/odin/certs/odin-key.pem localhost 127.0.0.1 ::1 "$(hostname)" "$(ipconfig getifaddr en0)"
   ```
   `mkcert -install` adds a local root certificate to this machine's trust store. Each other device needs to trust it once too: copy `"$(mkcert -CAROOT)/rootCA.pem"` to it and install it as a trusted certificate (on iOS, also turn it on in Settings › General › About › Certificate Trust Settings).
2. Add the certificate to the `server` block and restart the server:
   ```json
   "server": { "host": "0.0.0.0", "cert": "~/.config/odin/certs/odin.pem", "key": "~/.config/odin/certs/odin-key.pem" }
   ```

With a cert configured, the server speaks HTTPS on every interface, local `https://127.0.0.1:8788` included, and Odin's scripts and links switch to `https` automatically. `--host`, `--cert` and `--key` override the config for one run.

**There is no authentication.** Anyone who can reach the port can read and write files in your repositories and approve, reject or wake the agent. Only bind to `0.0.0.0` on a network you trust. The server does refuse requests whose `Host` is an unknown domain name (DNS rebinding) and POSTs from other websites (CSRF); add extra hostnames you use, such as a router-assigned name, to `server.allowed_hosts`.

### Progress timeline

Each phase card has a timeline of what happened while the phase ran. The plugin's progress hook fills it in automatically: every `git commit`, every test, lint, typecheck or build command (with ✅/❌), and every subagent dispatch and return. Claude adds findings, decisions, plan changes and incidents with `odin-state.py log`. Status changes are logged there too, and they're the only updates that chime. To turn the hook off, set `"progress": {"capture": false}` in your config.

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
