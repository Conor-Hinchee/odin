# Staging sandbox (`:8789`)

The staging sandbox lets you debug or improve Odin itself while live projects keep running on `:8788`. It requires Odin to run from a **git checkout** of the Odin repository, not the plugin cache. Set `staging.repo_dir` in your config, or install with `claude --plugin-dir <checkout>`. The dashboard hides staging controls when no checkout is available.

## Snapshot

Click **🧪 Pause & Snapshot to Staging**, or run `odin-state.py snapshot-staging`. Odin then:

1. pauses live `:8788` watchers (`.odin-paused.json`);
2. auto-commits any dirty work on the Odin checkout's `main`;
3. hard-resets the staging worktree (`staging.staging_dir`, default `<repo>-staging`, on branch `staging.branch`, default `staging/odin-8789`) to `main`;
4. mirrors `odin-states/` (including `archive/`) into it, rewriting `:8788` links so navigation stays on `:8789`;
5. starts the staging server on `:8789`, which shows a violet **🧪 STAGING** banner;
6. copies a ready-to-paste prompt for a **new Claude Code session** that works in the staging worktree.

## Promote

Once a fix is verified on `:8789`, click **🚀 Promote to Live**, or run `odin-state.py promote-staging`. Odin then:

1. commits the staging branch;
2. fast-forward merges it into `main`, leaving live `odin-states/` untouched by sandbox clicks;
3. restarts both servers on the new commit;
4. resumes live watchers.

Ports, paths and the branch name all come from config (`ports.live`, `ports.staging`, `staging.*`).

When spawning `odin-server.py` from a script or request handler, always pass `--port` and `--dir`. Otherwise the child defaults to the live port and can kill the parent server mid-request.
