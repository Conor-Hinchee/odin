#!/usr/bin/env python3
"""Claude Code hook: record Odin progress as it happens.

Registered for PostToolUse (Bash, Agent), PostToolUseFailure (Bash) and
SubagentStop. When this session has an active Odin task (a state file whose
<body data-session-id> matches and with a phase marked active or
awaiting_human), it appends to that task's progress timeline:

- every `git commit`, as the new HEAD's short sha and subject;
- check commands (tests, lint, typecheck, build; see `progress.check_patterns`
  in the Odin config) with pass/fail;
- each subagent (Valkyrie) dispatch and return.

The hook never blocks or prints, and exits 0 on any error. Set
`"progress": {"capture": false}` in the Odin config to turn it off.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from odin_config import load_config  # noqa: E402

COMMIT_RE = re.compile(r"(?:^|[;&|(]|\s)git\s+(?:-C\s+(\S+)\s+)?commit\b")
LEADING_CD_RE = re.compile(r"^\s*cd\s+\S+\s*&&\s*")
# Quote-aware, so a ">" inside an attribute value (e.g. an unfilled "<app-port>") doesn't end the tag.
BODY_TAG_RE = re.compile(r"""<body\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
ACTIVE_SECTION_RE = re.compile(r'<section\b[^>]*\bdata-status=["\'](?:active|awaiting_human)["\']', re.IGNORECASE)


def _odin_state():
    spec = importlib.util.spec_from_file_location("odin_state", SCRIPTS_DIR / "odin-state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state_files(root: str) -> list[str]:
    patterns = [os.path.join(root, *(["*"] * depth), "odin-states", "*.html") for depth in range(4)]
    return sorted({p for pattern in patterns for p in glob.glob(pattern)})


def find_active_state(root: str, session_id: str) -> Path | None:
    """The most recently modified state file of this session that has an active phase."""
    needle = f'data-session-id="{session_id}"'
    best: tuple[float, str] | None = None
    for path in state_files(root):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = BODY_TAG_RE.search(text)
        if not body or needle not in body.group(0) or not ACTIVE_SECTION_RE.search(text):
            continue
        mtime = os.path.getmtime(path)
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return Path(best[1]) if best else None


def short_command(command: str) -> str:
    one_line = " ".join(command.split())
    one_line = LEADING_CD_RE.sub("", one_line)
    return one_line if len(one_line) <= 100 else one_line[:97] + "…"


def tail_lines(text: str, count: int = 3) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def ledger_events(odin_state, html_path: Path) -> list[dict]:
    ledger = odin_state.extract_ledger(html_path.read_text(encoding="utf-8"), str(html_path))
    return [e for it in ledger.get("iterations", []) for e in it.get("events", [])]


def head_commit(*repo_dirs: str) -> tuple[str, str] | None:
    for repo in repo_dirs:
        if not repo or not os.path.isdir(repo):
            continue
        res = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%h%x09%s"], capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0 and "\t" in res.stdout:
            sha, subject = res.stdout.strip().split("\t", 1)
            return sha, subject
    return None


def classify(payload: dict, check_patterns: list[str]) -> str:
    """What this hook call is about, decided before touching any state file."""
    event = payload.get("hook_event_name", "")
    tool = payload.get("tool_name", "")
    if event == "SubagentStop":
        return "subagent_return"
    if tool == "Agent" and event == "PostToolUse":
        return "subagent_dispatch"
    if tool != "Bash":
        return ""
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if "odin-state.py" in command:
        return ""
    if COMMIT_RE.search(command):
        return "commit" if event == "PostToolUse" else ""
    if any(re.search(p, command) for p in check_patterns):
        return "check"
    return ""


def handle(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return
    cwd = payload.get("cwd") or os.getcwd()
    cfg = load_config(cwd)
    progress = cfg.get("progress") or {}
    if not progress.get("capture", True):
        return
    intent = classify(payload, progress.get("check_patterns") or [])
    if not intent:
        return
    html_path = find_active_state(cfg["root"], session_id)
    if not html_path:
        return
    odin_state = _odin_state()
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}

    if intent == "commit":
        command = str(tool_input.get("command") or "")
        m = COMMIT_RE.search(command)
        explicit = os.path.join(cwd, m.group(1)) if m and m.group(1) else ""
        worktree = odin_state._resolve_worktree_from_html(html_path.read_text(encoding="utf-8"), None)
        head = head_commit(explicit, cwd, worktree)
        if not head:
            return
        sha, subject = head
        if any(e.get("kind") == "commit" and e.get("sha") == sha for e in ledger_events(odin_state, html_path)):
            return
        odin_state.log_progress(html_path, "commit", f"{sha} {subject}", extra={"sha": sha})

    elif intent == "check":
        command = str(tool_input.get("command") or "")
        if payload.get("hook_event_name") == "PostToolUseFailure":
            error = str(payload.get("error") or "")
            odin_state.log_progress(
                html_path, "test", f"❌ {short_command(command)}", detail=error[:1500], outcome="fail"
            )
        else:
            detail = tail_lines(str(response.get("stdout") or ""))
            odin_state.log_progress(html_path, "test", f"✅ {short_command(command)}", detail=detail, outcome="pass")

    elif intent == "subagent_dispatch":
        description = str(tool_input.get("description") or "subagent")
        agent_type = str(tool_input.get("subagent_type") or "agent")
        agent_id = str(response.get("agentId") or "")
        if response.get("status") == "completed":
            content = response.get("content")
            if isinstance(content, list):
                content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            odin_state.log_progress(
                html_path,
                "valkyrie",
                f"{description} ({agent_type}) returned",
                detail=str(content or "")[:1500],
                extra={"agent_id": agent_id, "returned": True},
            )
        else:
            odin_state.log_progress(
                html_path, "valkyrie", f"Dispatched {description} ({agent_type})", extra={"agent_id": agent_id}
            )

    elif intent == "subagent_return":
        agent_id = str(payload.get("agent_id") or "")
        events = ledger_events(odin_state, html_path)
        if agent_id and any(e.get("agent_id") == agent_id and e.get("returned") for e in events):
            return
        dispatched = next((e for e in events if agent_id and e.get("agent_id") == agent_id), None)
        name = (
            dispatched["text"].removeprefix("Dispatched ")
            if dispatched
            else str(payload.get("agent_type") or "subagent")
        )
        odin_state.log_progress(
            html_path,
            "valkyrie",
            f"{name} returned",
            detail=str(payload.get("last_assistant_message") or "")[:1500],
            extra={"agent_id": agent_id, "returned": True},
        )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            handle(payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
