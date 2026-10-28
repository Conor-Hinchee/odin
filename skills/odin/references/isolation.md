# Worktree and port isolation

`odin-isolate.py` keeps concurrent Odin projects, and parallel Valkyries, from clobbering each other's branches or dev-server ports. It works only on repos listed in your Odin config:

```json
"repos": {
  "web": {
    "path": "~/code/web",
    "base_branch": "origin/main",
    "port_range": [8080, 8099],
    "role": "frontend",
    "start": "PORT={port} API_URL=http://localhost:{backend_port} npm run dev",
    "copy_paths": ["node_modules"]
  },
  "api": { "path": "~/code/api", "port_range": [3001, 3099], "role": "backend", "start": "PORT={port} npm start" }
}
```

## Commands

```bash
# Active leases, live listeners, and each repo's git state
python3 "${CLAUDE_SKILL_DIR}/scripts/odin-isolate.py" status

# Allocate ports (and a worktree if the primary checkout is busy)
python3 "${CLAUDE_SKILL_DIR}/scripts/odin-isolate.py" allocate \
  --repo web [--repo api] --branch "feat/<task-slug>" --slug "<task-slug>" --create-worktree
```

The output JSON gives `worktree`, `appPort`, `backendPort`, per-repo `allocations`, `hygieneWarnings`, and ready-to-run `commands`. Stamp the values on the state file's `<body>` (`data-worktree`, `data-branch`, `data-app-port`, `data-backend-port`).

## Rules

1. **Primary checkout or sibling worktree.** The primary checkout is used only if it is clean, on the task branch or the base branch, and not leased by another active Odin project. Otherwise Odin creates `<repo>-<port>` on a new branch off `base_branch` (default: the remote's default branch). It copies `copy_paths` in, using copy-on-write on macOS.
2. **Ports.** Each repo allocates from its `port_range`, skipping ports leased by other state files or already listening.
3. **Shared backends.** A backend repo the task does not modify is shared at its first port. Pass it with `--repo` only when the task changes it.
4. **Escalate surprises.** If a worktree you did not create has unexpected changes or the wrong branch, stop and ask the user.
5. **Single committer.** Valkyries edit only their assigned files and never commit or switch branches. Thor commits.

The dashboard's ⏹ Stop and Prune buttons only act on ports inside configured `port_range`s, and only on clean `<repo>-<port>` worktrees.
