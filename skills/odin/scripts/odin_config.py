#!/usr/bin/env python3
"""Shared configuration loader for Odin scripts.

Config files are JSON and are deep-merged in this order (later wins):
  1. built-in defaults (below)
  2. ~/.config/odin/config.json   (or $XDG_CONFIG_HOME/odin/config.json)
  3. <workspace>/.odin/config.json
  4. $ODIN_CONFIG
Environment overrides: ODIN_ROOT, ODIN_PORT, ODIN_STAGING_PORT,
ODIN_PRIMARY_REPO_DIR, ODIN_STAGING_DIR.

See odin.config.example.json at the repository root for every key.
"""

import copy
import json
import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent

DEFAULTS = {
    # Directory scanned (up to 3 levels deep) for odin-states/ folders.
    "root": None,
    "ports": {"live": 8788, "staging": 8789},
    # name -> {path, base_branch, port_range: [lo, hi], role: frontend|backend,
    #          start: "PORT={port} npm start", copy_paths: ["node_modules"]}
    "repos": {},
    "skill_pills": [
        {"id": "/code-review", "label": "/code-review", "desc": "Review the current diff for bugs"},
        {"id": "/simplify", "label": "/simplify", "desc": "Clean up the changed code"},
    ],
    # {command: "my-cli --json", columns: [{key, label}], title}
    "telemetry": None,
    # {label, heading, require_prompt_summary, sanitize_hash_refs}
    "pr": None,
    # {repo_dir, staging_dir, branch}
    "staging": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_json(path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config_paths(workspace=None) -> list:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    paths = [Path(xdg) / "odin" / "config.json"]
    ws = workspace or os.environ.get("ODIN_ROOT") or os.getcwd()
    paths.append(Path(ws) / ".odin" / "config.json")
    if os.environ.get("ODIN_CONFIG"):
        paths.append(Path(os.path.expanduser(os.environ["ODIN_CONFIG"])))
    return paths


def _git_toplevel(path: Path):
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def load_config(workspace=None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    for path in config_paths(workspace):
        if path.is_file():
            cfg = _deep_merge(cfg, _read_json(path))

    root = os.environ.get("ODIN_ROOT") or cfg.get("root") or workspace or os.getcwd()
    cfg["root"] = os.path.abspath(os.path.expanduser(root))

    ports = cfg["ports"]
    ports["live"] = int(os.environ.get("ODIN_PORT") or ports.get("live") or 8788)
    ports["staging"] = int(os.environ.get("ODIN_STAGING_PORT") or ports.get("staging") or 8789)

    for repo in cfg["repos"].values():
        if repo.get("path"):
            repo["path"] = os.path.abspath(os.path.expanduser(repo["path"]))

    # Staging snapshots the git checkout Odin itself runs from. When installed
    # from the plugin cache there is no checkout, so staging is unavailable.
    staging = cfg.get("staging") or {}
    repo_dir = os.environ.get("ODIN_PRIMARY_REPO_DIR") or staging.get("repo_dir")
    if not repo_dir:
        top = _git_toplevel(SKILL_DIR)
        repo_dir = str(top) if top else ""
    repo_dir = os.path.abspath(os.path.expanduser(repo_dir)) if repo_dir else ""
    staging_dir = os.environ.get("ODIN_STAGING_DIR") or staging.get("staging_dir")
    if not staging_dir and repo_dir:
        staging_dir = repo_dir.rstrip("/") + "-staging"
    staging["repo_dir"] = repo_dir
    staging["staging_dir"] = os.path.abspath(os.path.expanduser(staging_dir)) if staging_dir else ""
    staging["branch"] = staging.get("branch") or f"staging/odin-{ports['staging']}"
    staging["available"] = bool(repo_dir and Path(repo_dir, ".git").exists())
    cfg["staging"] = staging
    return cfg


def render_ports(text: str, cfg: dict) -> str:
    """Rewrite the default 8788/8789 port literals in generated HTML/JS to the configured ports."""
    live, staging = cfg["ports"]["live"], cfg["ports"]["staging"]
    if live == 8788 and staging == 8789:
        return text
    return (
        text.replace("8788", "\0ODIN_LIVE\0")
        .replace("8789", str(staging))
        .replace("\0ODIN_LIVE\0", str(live))
    )


if __name__ == "__main__":
    print(json.dumps(load_config(), indent=2))
