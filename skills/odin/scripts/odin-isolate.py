#!/usr/bin/env python3
"""Odin Git Worktree & Multi-Instance Port Isolation Manager.

Repos come from the "repos" block of your Odin config (see odin_config.py):

    "repos": {
      "web": {"path": "~/code/web", "base_branch": "origin/main", "port_range": [8080, 8099],
              "role": "frontend", "start": "PORT={port} API_URL=http://localhost:{backend_port} npm run dev",
              "copy_paths": ["node_modules"]},
      "api": {"path": "~/code/api", "port_range": [3001, 3099], "role": "backend",
              "start": "PORT={port} npm start"}
    }

Isolation rules:
1. Hybrid worktree isolation: use the primary checkout ONLY if it is clean, on the requested or
   base branch, and not leased by another active Odin project. Otherwise create a sibling git
   worktree (<repo>-<port>) on a dedicated branch and copy `copy_paths` into it (copy-on-write
   `cp -cR` on macOS).
2. Port leases + live lsof inspection: each repo allocates from its port_range, skipping ports
   leased by other Odin state files (data-app-port / data-backend-port) or already listening.
3. Backend repos not named in --repo are shared at their default (first) port.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odin_config import load_config  # noqa: E402

CONFIG = load_config()
ROOT_DIR = CONFIG["root"]
REPO_CONFIGS = CONFIG.get("repos") or {}


def run_cmd(cmd: list, cwd: str = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def get_listening_ports() -> dict[int, dict]:
    """Return mapping of listening TCP port -> {pid, command}."""
    code, out, _ = run_cmd(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    listeners = {}
    if code != 0 or not out:
        return listeners
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd_name = parts[0]
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        name_field = parts[8]
        m = re.search(r":(\d+)$", name_field)
        if m:
            port = int(m.group(1))
            listeners[port] = {"pid": pid, "command": cmd_name}
    return listeners


def read_git_head_branch(repo_path: str) -> str:
    """Read current branch directly from .git/HEAD (works even inside strict sandbox)."""
    dot_git = os.path.join(repo_path, ".git")
    head_file = os.path.join(dot_git, "HEAD")
    try:
        if os.path.isfile(dot_git):
            with open(dot_git, "r", encoding="utf-8", errors="replace") as f:
                line = f.read().strip()
            if line.startswith("gitdir:"):
                gitdir = line.split(":", 1)[1].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(repo_path, gitdir))
                head_file = os.path.join(gitdir, "HEAD")
        if os.path.isfile(head_file):
            with open(head_file, "r", encoding="utf-8", errors="replace") as f:
                ref = f.read().strip()
            if ref.startswith("ref: refs/heads/"):
                return ref[len("ref: refs/heads/"):]
            return ref[:12]
    except OSError:
        pass
    return ""


def get_git_state(repo_path: str) -> dict:
    if not repo_path or not os.path.isdir(repo_path):
        return {"exists": False, "branch": "", "dirty": False, "dirtyCount": 0}
    code_b, branch, _ = run_cmd(["git", "branch", "--show-current"], cwd=repo_path)
    branch_str = branch.strip() if code_b == 0 and branch.strip() else read_git_head_branch(repo_path)
    code_p, porcelain, _ = run_cmd(["git", "status", "--porcelain"], cwd=repo_path)
    lines = [ln for ln in porcelain.splitlines() if ln.strip()] if code_p == 0 else []
    return {
        "exists": True,
        "branch": branch_str,
        "dirty": len(lines) > 0,
        "dirtyCount": len(lines),
    }


def resolve_base_branch(cfg: dict) -> str:
    """Configured base_branch, else the remote default branch, else main."""
    if cfg.get("base_branch"):
        return cfg["base_branch"]
    code, out, _ = run_cmd(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cfg.get("path"))
    if code == 0 and out:
        return out
    return "main"


def _port_range(cfg: dict) -> range:
    pr = cfg.get("port_range") or [8080, 8099]
    return range(int(pr[0]), int(pr[1]) + 1)


def scan_active_odin_leases(exclude_slug: str = "") -> list[dict]:
    patterns = [
        os.path.join(ROOT_DIR, "odin-states", "*.html"),
        os.path.join(ROOT_DIR, "*", "odin-states", "*.html"),
        os.path.join(ROOT_DIR, "*", "*", "odin-states", "*.html"),
    ]
    leases = []
    seen = set()
    for pat in patterns:
        for path in glob.glob(pat):
            real = os.path.realpath(path)
            if real in seen or os.path.basename(path).lower() == "index.html":
                continue
            seen.add(real)
            slug = os.path.basename(path)[:-5]
            if exclude_slug and slug == exclude_slug:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue

            # Skip if human-validation is explicitly marked done
            if re.search(
                r'id=["\']human-validation["\'][^>]*data-status=["\']done["\']',
                content,
                re.IGNORECASE,
            ):
                continue

            body_m = re.search(r"<body\b[^>]*>", content, re.IGNORECASE)
            body_tag = body_m.group(0) if body_m else ""

            def attr(name: str) -> str:
                m = re.search(rf'{name}=["\']([^"\']*)["\']', body_tag, re.IGNORECASE)
                return m.group(1).strip() if m else ""

            worktree = attr("data-worktree")
            branch = attr("data-branch")
            app_port_str = attr("data-app-port")
            backend_port_str = attr("data-backend-port")
            if worktree and not branch:
                branch = get_git_state(worktree).get("branch", "")

            leases.append({
                "slug": slug,
                "path": path,
                "branch": branch,
                "worktree": worktree,
                "appPort": int(app_port_str) if app_port_str.isdigit() else None,
                "backendPort": int(backend_port_str) if backend_port_str.isdigit() else None,
            })
    return leases


def _allocate_repo(name: str, cfg: dict, branch: str, listeners: dict, leased_ports: set, leased_worktrees: set) -> dict:
    primary = cfg["path"]
    ports = _port_range(cfg)
    default_port = ports.start
    base_branch = resolve_base_branch(cfg)
    state = get_git_state(primary)
    integration = base_branch.split("/", 1)[1] if base_branch.startswith("origin/") else base_branch
    primary_available = (
        os.path.realpath(primary) not in leased_worktrees
        and not state["dirty"]
        and state["branch"] in (branch, integration, "")
        and default_port not in leased_ports
        and default_port not in listeners
    )
    alloc = {
        "repo": name,
        "role": cfg.get("role", "frontend"),
        "port": default_port,
        "worktree": primary,
        "mode": "primary",
        "baseBranch": base_branch,
        "warning": "",
    }
    if not primary_available:
        alloc["port"] = next(
            (p for p in ports if p != default_port and p not in listeners and p not in leased_ports),
            None,
        )
        alloc["worktree"] = f"{primary}-{alloc['port']}"
        alloc["mode"] = "worktree"
        if state["dirty"]:
            alloc["warning"] = (
                f"Primary {name} has {state['dirtyCount']} uncommitted change(s) on '{state['branch']}'; "
                f"isolating into {alloc['worktree']}."
            )
    return alloc


def provision_worktree(alloc: dict, cfg: dict, branch: str) -> None:
    wt_path = alloc["worktree"]
    if os.path.exists(wt_path):
        return
    run_cmd(["git", "worktree", "add", wt_path, "-b", branch, alloc["baseBranch"]], cwd=cfg["path"])
    cp_flags = "-cR" if sys.platform == "darwin" else "-R"  # APFS copy-on-write on macOS
    for rel in cfg.get("copy_paths", ["node_modules"]):
        src = os.path.join(cfg["path"], rel)
        dst = os.path.join(wt_path, rel)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            run_cmd(["cp", cp_flags, src, dst])


def allocate_environment(
    targets: list,
    branch: str,
    slug: str = "",
    provision: bool = False,
) -> dict:
    unknown = [t for t in targets if t not in REPO_CONFIGS]
    if unknown:
        raise SystemExit(
            f"Unknown repo(s): {', '.join(unknown)}. Configured repos: {', '.join(REPO_CONFIGS) or '(none)'}"
        )
    listeners = get_listening_ports()
    leases = scan_active_odin_leases(exclude_slug=slug)
    leased_ports = {l[k] for l in leases for k in ("appPort", "backendPort") if l.get(k)}
    leased_worktrees = {os.path.realpath(l["worktree"]) for l in leases if l.get("worktree") and os.path.exists(l["worktree"])}

    allocations = [
        _allocate_repo(name, REPO_CONFIGS[name], branch, listeners, leased_ports, leased_worktrees)
        for name in targets
    ]
    # Backend repos the task doesn't modify are shared at their default port
    shared = [
        {"repo": name, "role": "backend", "port": _port_range(cfg).start, "worktree": cfg["path"], "mode": "shared"}
        for name, cfg in REPO_CONFIGS.items()
        if cfg.get("role") == "backend" and name not in targets
    ]

    frontend = next((a for a in allocations if a["role"] != "backend"), None)
    backend = next((a for a in allocations + shared if a["role"] == "backend"), None)
    primary_alloc = frontend or (allocations[0] if allocations else None)

    commands = []
    for a in allocations:
        template = REPO_CONFIGS[a["repo"]].get("start")
        if template:
            cmd = template.format(port=a["port"], backend_port=backend["port"] if backend else "", path=a["worktree"])
            commands.append(f"cd {a['worktree']} && {cmd}")
        if provision and a["mode"] == "worktree":
            provision_worktree(a, REPO_CONFIGS[a["repo"]], branch)

    return {
        "targets": targets,
        "branch": branch,
        "slug": slug,
        "worktree": primary_alloc["worktree"] if primary_alloc else "",
        "mode": primary_alloc["mode"] if primary_alloc else "",
        "appPort": frontend["port"] if frontend else None,
        "backendPort": backend["port"] if backend else None,
        "allocations": allocations + shared,
        "hygieneWarnings": [a["warning"] for a in allocations if a.get("warning")],
        "commands": commands,
    }


def main():
    parser = argparse.ArgumentParser(description="Odin Git Worktree & Port Isolation CLI")
    sub = parser.add_subparsers(dest="cmd")

    alloc = sub.add_parser("allocate", help="Allocate isolated worktree & ports for an Odin task")
    alloc.add_argument(
        "--repo", "--target", dest="targets", action="append", default=None,
        help="Configured repo name to allocate (repeatable). Defaults to the first configured repo.",
    )
    alloc.add_argument("--branch", required=True, help="Feature branch name (e.g. feat/my-task)")
    alloc.add_argument("--slug", default="", help="Odin state slug")
    alloc.add_argument("--provision", "--create-worktree", dest="provision", action="store_true", help="Create the git worktree & copy copy_paths if needed")

    sub.add_parser("status", help="Show live listening ports and active Odin leases")

    args = parser.parse_args()
    if args.cmd == "allocate":
        if not REPO_CONFIGS:
            raise SystemExit('No repos configured. Add a "repos" block to your Odin config (see odin.config.example.json).')
        targets = args.targets or [next(iter(REPO_CONFIGS))]
        res = allocate_environment(targets, args.branch, args.slug, args.provision)
        print(json.dumps(res, indent=2))
    else:
        print(json.dumps({
            "listeners": get_listening_ports(),
            "activeLeases": scan_active_odin_leases(),
            "repos": {
                k: get_git_state(v.get("path", "")) for k, v in REPO_CONFIGS.items()
            },
        }, indent=2))


if __name__ == "__main__":
    main()
