#!/usr/bin/env python3
"""Claude Code & Antigravity hook: record Odin progress as it happens.

Registered for PostToolUse (Bash / Agent in Claude Code; run_command /
invoke_subagent in Antigravity), PostToolUseFailure (Bash) and SubagentStop.
When this session has an active Odin task (a state file whose
<body data-session-id> matches and with a phase marked active or
awaiting_human), it appends to that task's progress timeline:

- every `git commit`, as the new HEAD's short sha and subject;
- check commands (tests, lint, typecheck, build; see `progress.check_patterns`
  in the Odin config) with pass/fail;
- each subagent (Valkyrie) dispatch and return.

The hook never blocks, outputs `{}` when called by Antigravity PostToolUse,
and exits 0 on any error. Set `"progress": {"capture": false}` in the Odin
config to turn it off.
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
FRICTION_COMMAND_RE = re.compile(
    r"\b(?:gh\s+pr\s+(?:create|edit|merge|ready)|git\s+(?:push|checkout|switch|worktree|merge|rebase|cherry-pick|pull))\b"
)
EXIT_CODE_RE = re.compile(r"Command exited with code (\d+)\.")
BG_TASK_ID_RE = re.compile(r"Tool is running as a background task with task id:\s*([^\s.\n]+)")
TASK_DESC_RE = re.compile(r"Task Description:\s*(.+)")
LEADING_CD_RE = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*")
# Quote-aware, so a ">" inside an attribute value (e.g. an unfilled "<app-port>") doesn't end the tag.
BODY_TAG_RE = re.compile(r"""<body\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
ACTIVE_SECTION_RE = re.compile(r'<section\b[^>]*\bdata-status=["\'](?:active|awaiting_human)["\']', re.IGNORECASE)


def is_antigravity_payload(payload: dict) -> bool:
    return "toolCall" in payload or "conversationId" in payload


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


def _extract_tool_and_args(payload: dict) -> tuple[str, dict]:
    if is_antigravity_payload(payload):
        tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        name = str(tc.get("name") or "")
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        return name, args
    name = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    return name, args


def _inspect_transcript_outcome(payload: dict) -> dict:
    """Inspect Antigravity transcript.jsonl (if present) for exit codes and background task IDs."""
    info = {"exit_code": None, "failed": False, "bg_task_id": "", "output": ""}
    t_path_raw = str(payload.get("transcriptPath") or payload.get("transcript_path") or "").strip()
    if not t_path_raw:
        return info
    t_path = Path(t_path_raw)
    if not t_path.is_file():
        return info
    try:
        lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return info
    step_idx = payload.get("stepIdx") if "stepIdx" in payload else payload.get("step_idx")
    target_step = int(step_idx) if isinstance(step_idx, int) or (isinstance(step_idx, str) and step_idx.isdigit()) else None

    candidate_contents: list[str] = []
    tail = lines[-60:] if len(lines) > 60 else lines
    parsed_tail: list[dict] = []
    for raw in tail:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                parsed_tail.append(obj)
        except ValueError:
            continue

    if target_step is not None:
        for obj in parsed_tail:
            s_idx = obj.get("step_index")
            if isinstance(s_idx, int) and s_idx >= target_step and obj.get("type") in ("GENERIC", "TOOL_RESPONSE"):
                content = str(obj.get("content") or "")
                if content:
                    candidate_contents.append(content)
                    break

    if not candidate_contents:
        for obj in reversed(parsed_tail):
            if obj.get("type") in ("GENERIC", "TOOL_RESPONSE") and obj.get("content"):
                candidate_contents.append(str(obj.get("content") or ""))
                break

    if not candidate_contents:
        return info

    content = candidate_contents[0]
    m_bg = BG_TASK_ID_RE.search(content)
    if m_bg:
        info["bg_task_id"] = m_bg.group(1).strip()
    m_exit = EXIT_CODE_RE.search(content)
    if m_exit:
        code = int(m_exit.group(1))
        info["exit_code"] = code
        info["failed"] = code != 0
    if "Command output:" in content:
        info["output"] = content.split("Command output:", 1)[1].strip()
    elif "Output:" in content:
        info["output"] = content.split("Output:", 1)[1].strip()
    else:
        info["output"] = content.strip()
    return info


def _lookup_background_task_command(payload: dict, task_id: str) -> str:
    """Scan transcript.jsonl for the original command launched under task_id."""
    if not task_id:
        return ""
    t_path_raw = str(payload.get("transcriptPath") or payload.get("transcript_path") or "").strip()
    if not t_path_raw:
        return ""
    t_path = Path(t_path_raw)
    if not t_path.is_file():
        return ""
    try:
        lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    parsed: list[dict] = []
    for raw in lines:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                parsed.append(obj)
        except ValueError:
            continue
    for idx in range(len(parsed) - 1, -1, -1):
        obj = parsed[idx]
        content = str(obj.get("content") or "")
        if task_id not in content:
            continue
        if "Task Description:" in content:
            m_desc = TASK_DESC_RE.search(content)
            if m_desc:
                return m_desc.group(1).strip()
        if BG_TASK_ID_RE.search(content) and idx > 0:
            prev = parsed[idx - 1]
            for tc in prev.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("name") in ("run_command", "Bash"):
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    cmd = str(args.get("CommandLine") or args.get("command") or "").strip()
                    if cmd:
                        return cmd
    return ""


def _command_failed(payload: dict, transcript_info: dict | None = None) -> bool:
    if payload.get("hook_event_name") == "PostToolUseFailure" or bool(payload.get("error")):
        return True
    response = payload.get("tool_response")
    if isinstance(response, dict):
        rc = response.get("returncode") if "returncode" in response else response.get("exit_code")
        if isinstance(rc, int) and rc != 0:
            return True
    t_info = transcript_info if transcript_info is not None else _inspect_transcript_outcome(payload)
    return bool(t_info.get("failed"))


def classify(payload: dict, check_patterns: list[str]) -> str:
    """What this hook call is about, decided before touching any state file."""
    event = payload.get("hook_event_name", "")
    tool, tool_args = _extract_tool_and_args(payload)
    if event == "SubagentStop":
        return "subagent_return"
    if (tool == "Agent" and event == "PostToolUse") or tool == "invoke_subagent":
        return "subagent_dispatch"
    if tool == "manage_task":
        action = str(tool_args.get("Action") or tool_args.get("action") or "").strip().lower()
        if action == "kill":
            return "kill_task"
        return ""
    if tool not in ("Bash", "run_command"):
        return ""
    command = str(tool_args.get("command") or tool_args.get("CommandLine") or "")
    if "odin-state.py" in command:
        return ""
    if COMMIT_RE.search(command):
        return "commit"
    if any(re.search(p, command) for p in check_patterns):
        return "check"
    if FRICTION_COMMAND_RE.search(command):
        return "friction_cmd"
    return ""


def handle(payload: dict) -> None:
    session_id = str(payload.get("session_id") or payload.get("conversationId") or "").strip()
    if not session_id:
        return
    ws_paths = payload.get("workspacePaths")
    cwd = (
        payload.get("cwd")
        or (ws_paths[0] if isinstance(ws_paths, list) and ws_paths else None)
        or os.getcwd()
    )
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
    _, tool_input = _extract_tool_and_args(payload)
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    t_info = _inspect_transcript_outcome(payload)

    if intent == "commit":
        command = str(tool_input.get("command") or tool_input.get("CommandLine") or "")
        if _command_failed(payload, t_info):
            error_detail = str(payload.get("error") or t_info.get("output") or "")
            exit_suffix = f" (exit {t_info['exit_code']})" if t_info.get("exit_code") else ""
            odin_state.log_progress(
                html_path,
                "incident",
                f"🚨 Command failed{exit_suffix}: {short_command(command)}",
                detail=error_detail[:1500],
            )
            return
        m = COMMIT_RE.search(command)
        explicit = os.path.join(cwd, m.group(1)) if m and m.group(1) else ""
        cd_match = LEADING_CD_RE.search(command)
        cd_dir = os.path.join(cwd, cd_match.group(1).strip("\"'")) if cd_match else ""
        worktree = odin_state._resolve_worktree_from_html(html_path.read_text(encoding="utf-8"), None)
        head = head_commit(explicit, cd_dir, cwd, worktree)
        if not head:
            return
        sha, subject = head
        if any(e.get("kind") == "commit" and e.get("sha") == sha for e in ledger_events(odin_state, html_path)):
            return
        odin_state.log_progress(html_path, "commit", f"{sha} {subject}", extra={"sha": sha})

    elif intent == "check":
        command = str(tool_input.get("command") or tool_input.get("CommandLine") or "")
        bg_task_id = t_info.get("bg_task_id") or ""
        if bg_task_id:
            odin_state.log_progress(
                html_path,
                "step",
                f"⏳ {short_command(command)} (backgrounded: {bg_task_id})",
                extra={"bg_task_id": bg_task_id},
            )
        elif _command_failed(payload, t_info):
            error = str(payload.get("error") or t_info.get("output") or response.get("stderr") or response.get("stdout") or "")
            exit_suffix = f" (exit {t_info['exit_code']})" if t_info.get("exit_code") else ""
            odin_state.log_progress(
                html_path,
                "test",
                f"❌ {short_command(command)}{exit_suffix}",
                detail=error[:1500],
                outcome="fail",
            )
        else:
            detail = tail_lines(str(response.get("stdout") or t_info.get("output") or ""))
            odin_state.log_progress(html_path, "test", f"✅ {short_command(command)}", detail=detail, outcome="pass")

    elif intent == "friction_cmd":
        if not _command_failed(payload, t_info):
            return
        command = str(tool_input.get("command") or tool_input.get("CommandLine") or "")
        error = str(payload.get("error") or t_info.get("output") or response.get("stderr") or response.get("stdout") or "")
        exit_suffix = f" (exit {t_info['exit_code']})" if t_info.get("exit_code") else ""
        odin_state.log_progress(
            html_path,
            "incident",
            f"🚨 Command failed{exit_suffix}: {short_command(command)}",
            detail=error[:1500],
        )

    elif intent == "kill_task":
        task_id = str(tool_input.get("TaskId") or tool_input.get("task_id") or "").strip()
        orig_cmd = _lookup_background_task_command(payload, task_id)
        if "odin-state.py" in orig_cmd and "wait-for-human" in orig_cmd:
            return
        label = short_command(orig_cmd) if orig_cmd else (task_id or "background task")
        detail = f"Killed background task {task_id}" + (f"\nCommand: {orig_cmd}" if orig_cmd else "")
        odin_state.log_progress(
            html_path,
            "incident",
            f"🚨 Killed hung/background task ({task_id or 'task'}): {label}",
            detail=detail[:1500],
            extra={"killed_task_id": task_id},
        )

    elif intent == "subagent_dispatch":
        description = str(tool_input.get("description") or tool_input.get("Role") or "subagent")
        agent_type = str(tool_input.get("subagent_type") or tool_input.get("TypeName") or "agent")
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
    is_agy = False
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            is_agy = is_antigravity_payload(payload)
            handle(payload)
    except Exception:
        pass
    if is_agy:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
