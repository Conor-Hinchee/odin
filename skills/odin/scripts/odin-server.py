#!/usr/bin/env python3
"""Local HTTP & API server for Odin Allfather State Dashboards.

Serves the configured Odin root (see odin_config.py) over http://127.0.0.1:8788 with:
- Homepage Dashboard at http://127.0.0.1:8788/ (and /odin-states/)
- GET /api/projects: discovers all odin-states/*.html and odin-states/archive/*.html
  across workspaces, extracts 5-phase status telemetry, and enriches with live
  git branch, worktree hygiene, and lsof port status for configured dev-port ranges.
- POST /api/projects/action: supports archiving (with optional port stop & worktree
  prune), restoring, bulk-archiving completed projects, stopping dev ports,
  auto-allocating free ports/worktrees, and updating phase statuses/titles/notes/env on disk.
"""

import argparse
import datetime
import glob
import html as html_lib
import http.server
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odin_config import load_config, render_ports  # noqa: E402
import odin_transcript  # noqa: E402

CONFIG = load_config()
PORT = CONFIG["ports"]["live"]
STAGING_PORT = CONFIG["ports"]["staging"]
ROOT_DIR = CONFIG["root"]
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_HTML_PATH = os.path.join(SKILL_DIR, "resources", "dashboard.html")


def configure_server(port: int | None = None, root_dir: str | None = None) -> tuple[int, str]:
    global PORT, ROOT_DIR
    if port is not None:
        PORT = int(port)
        os.environ["ODIN_PORT"] = str(PORT)
        CONFIG["ports"]["live"] = PORT
    if root_dir:
        ROOT_DIR = os.path.abspath(root_dir)
        os.environ["ODIN_ROOT"] = ROOT_DIR
        CONFIG["root"] = ROOT_DIR
    return PORT, ROOT_DIR


def parse_server_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Odin Allfather State & Command Server", add_help=False)
    parser.add_argument("--port", "-p", type=int, default=None, help="TCP port to listen on (default: ODIN_PORT or 8788)")
    parser.add_argument("--dir", "--root", "-d", dest="root_dir", default=None, help="Root workspace directory to serve")
    args, _ = parser.parse_known_args(argv)
    return args


PHASE_IDS = [
    ("huginn-plan", "Phase 1: Huginn"),
    ("thor-execution", "Phase 2: Thor & Valkyries"),
    ("heimdall-validation", "Phase 3: Heimdall"),
    ("hlidskjalf-judge", "Phase 4: Hlidskjalf"),
    ("human-validation", "Phase 5: Human Validation"),
]

BADGE_HTML = {
    "done": '<span class="badge-done">DONE</span>',
    "active": '<span class="badge-active">ACTIVE</span>',
    "awaiting_human": '<span class="badge-awaiting_human">❓ OPEN QUESTIONS — AWAITING HUMAN</span>',
    "pending": '<span class="badge-pending">PENDING</span>',
    "reject": '<span class="badge-reject">REJECT</span>',
}

_GIT_CACHE = {}
_GIT_CACHE_TTL = 4.0
_SESSION_CACHE = {"ts": 0.0, "map": {}}

_TELEMETRY_TTL = 60.0
_TELEMETRY_LOCK = threading.Lock()
_TELEMETRY_CACHE = {
    "ts": 0.0,
    "updatedIso": "",
    "refreshing": False,
    "error": "",
    "environments": [],
}


def _telemetry_config() -> dict:
    tcfg = CONFIG.get("telemetry") or {}
    return tcfg if isinstance(tcfg, dict) and tcfg.get("command") else {}


def _telemetry_payload(snap: dict) -> dict:
    tcfg = _telemetry_config()
    out = dict(snap)
    out["configured"] = bool(tcfg)
    out["title"] = tcfg.get("title") or "Environment Telemetry"
    out["columns"] = tcfg.get("columns") or []
    return out


def refresh_telemetry(force: bool = False) -> dict:
    """Run the configured telemetry command (expects a JSON array of row objects) and cache the result."""
    tcfg = _telemetry_config()
    if not tcfg:
        return _telemetry_payload(_TELEMETRY_CACHE)
    now = time.time()
    with _TELEMETRY_LOCK:
        if (
            not force
            and _TELEMETRY_CACHE["environments"]
            and (now - _TELEMETRY_CACHE["ts"]) < _TELEMETRY_TTL
        ):
            return _telemetry_payload(_TELEMETRY_CACHE)
        if _TELEMETRY_CACHE["refreshing"] and not force:
            return _telemetry_payload(_TELEMETRY_CACHE)
        _TELEMETRY_CACHE["refreshing"] = True

    try:
        proc = subprocess.run(
            tcfg["command"],
            shell=True,
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=int(tcfg.get("timeout", 20)),
        )
        raw_out = (proc.stdout or "").strip()
        json_start = raw_out.find("[")
        json_end = raw_out.rfind("]")
        if proc.returncode == 0 and json_start != -1 and json_end != -1:
            parsed = json.loads(raw_out[json_start : json_end + 1])
            ts_now = time.time()
            iso_now = datetime.datetime.fromtimestamp(ts_now).isoformat(timespec="seconds")
            with _TELEMETRY_LOCK:
                _TELEMETRY_CACHE["ts"] = ts_now
                _TELEMETRY_CACHE["updatedIso"] = iso_now
                _TELEMETRY_CACHE["refreshing"] = False
                _TELEMETRY_CACHE["error"] = ""
                _TELEMETRY_CACHE["environments"] = parsed if isinstance(parsed, list) else []
                return _telemetry_payload(_TELEMETRY_CACHE)

        err_msg = (proc.stderr or raw_out or f"telemetry command exited with code {proc.returncode}").strip()
        with _TELEMETRY_LOCK:
            _TELEMETRY_CACHE["refreshing"] = False
            _TELEMETRY_CACHE["error"] = err_msg[:240]
            return _telemetry_payload(_TELEMETRY_CACHE)
    except Exception as exc:
        with _TELEMETRY_LOCK:
            _TELEMETRY_CACHE["refreshing"] = False
            _TELEMETRY_CACHE["error"] = str(exc)[:240]
            return _telemetry_payload(_TELEMETRY_CACHE)


def get_telemetry_snapshot() -> dict:
    """Return cached telemetry immediately and trigger a background refresh if stale."""
    if not _telemetry_config():
        return _telemetry_payload(_TELEMETRY_CACHE)
    now = time.time()
    need_async = False
    with _TELEMETRY_LOCK:
        snap = dict(_TELEMETRY_CACHE)
        if not snap["refreshing"] and (not snap["environments"] or (now - snap["ts"]) >= _TELEMETRY_TTL):
            need_async = True
    if need_async:
        threading.Thread(target=refresh_telemetry, kwargs={"force": False}, daemon=True).start()
    return _telemetry_payload(snap)


def _discover_session_for_file(filepath: str, filename: str, content: str) -> str:
    """Resolve the Claude Code session ID associated with an Odin HTML state file."""
    m_attr = re.search(r'data-session-id=["\']([0-9A-Za-z-]{8,64})["\']', content, re.IGNORECASE)
    if m_attr:
        return m_attr.group(1)
    return odin_transcript.discover_session_for_file(filename, filepath)


def _parse_iso_ts(ts_str: str) -> float:
    if not ts_str:
        return 0.0
    try:
        cleaned = ts_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(cleaned).timestamp()
    except Exception:
        return 0.0


def detect_open_questions(filepath: str, filename: str, content: str) -> dict:
    """Detect whether the Odin project or its correlated Claude Code session is paused on Open Questions."""
    result = {
        "sessionId": "",
        "awaitingHuman": False,
        "openQuestions": [],
        "planFile": "",
        "questionsResolved": False,
        "lastResolutionTs": 0.0,
        "lastQuestionTs": 0.0,
    }

    try:
        html_mtime = os.path.getmtime(filepath)
    except OSError:
        html_mtime = 0.0

    # 1. Explicit open questions block inside an awaiting_human Huginn section of the Odin HTML file
    oq_box = re.search(
        r'<div\b(?![^>]*\bdata-resolved=["\']true["\'])[^>]*class=["\'][^"\']*open-questions-box[^"\']*["\'][^>]*>([\s\S]*?)</div>',
        content,
        re.IGNORECASE,
    )
    huginn_awaiting = bool(
        re.search(
            r'<section\b[^>]*\bid=["\'](?:huginn-plan|huginn-intake)["\'][^>]*\bdata-status=["\']awaiting_human["\']',
            content,
            re.IGNORECASE,
        )
    )
    if oq_box and huginn_awaiting:
        items = re.findall(r"<li[^>]*>([\s\S]*?)</li>", oq_box.group(1), re.IGNORECASE)
        cleaned_items = [strip_tags(it) for it in items if strip_tags(it)]
        if cleaned_items:
            result["openQuestions"] = cleaned_items

    sid = _discover_session_for_file(filepath, filename, content)
    result["sessionId"] = sid
    tpath = odin_transcript.find_transcript(sid)
    if not tpath:
        return result

    session_q = odin_transcript.analyze_questions(tpath)
    result["questionsResolved"] = session_q["questionsResolved"]
    result["lastResolutionTs"] = session_q["lastResolutionTs"]
    result["lastQuestionTs"] = session_q["lastQuestionTs"]
    if session_q["awaitingHuman"]:
        result["awaitingHuman"] = True
        if not result["openQuestions"]:
            result["openQuestions"] = session_q["openQuestions"]
    elif result["questionsResolved"] and result["lastResolutionTs"] >= html_mtime - 2.0:
        # Human answered after the HTML state file was last written; clear any stale HTML openQuestions
        result["openQuestions"] = []
    return result


def run_cmd(cmd: list, cwd: str = None, timeout: int = 8) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def get_listening_ports() -> dict[int, dict]:
    """Return mapping of listening TCP port -> {pid, command}."""
    code, out, _ = run_cmd(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=4)
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
            p = int(m.group(1))
            listeners[p] = {"pid": p and pid, "command": cmd_name}
    return listeners


def _format_relative_ago(ts: float) -> str:
    if not ts or ts <= 0:
        return ""
    diff = max(0, int(time.time() - ts))
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def get_session_telemetry(
    filepath: str,
    filename: str = "",
    content: str | None = None,
    listeners: dict[int, dict] | None = None,
) -> dict:
    fname = filename or os.path.basename(filepath)
    slug = fname[:-5] if fname.endswith(".html") else fname
    states_dir = os.path.dirname(filepath)
    if content is None and os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            content = ""
    content = content or ""

    # 1. Check .odin-paused.json
    paused = False
    pause_file = os.path.join(states_dir, ".odin-paused.json")
    if os.path.isfile(pause_file):
        try:
            with open(pause_file, "r", encoding="utf-8") as f:
                p_data = json.load(f)
                paused = bool(p_data.get("paused"))
        except Exception:
            pass

    # 2. Check .odin-watcher-<slug>.json
    watcher_armed = False
    watcher_phase = ""
    watcher_pid = None
    watcher_file = os.path.join(states_dir, f".odin-watcher-{slug}.json")
    if os.path.isfile(watcher_file):
        try:
            with open(watcher_file, "r", encoding="utf-8") as f:
                w_data = json.load(f)
            watcher_pid = w_data.get("pid")
            watcher_phase = w_data.get("phase") or ""
            hb_ts = _parse_iso_ts(w_data.get("heartbeatAt") or "")
            pid_alive = False
            if isinstance(watcher_pid, int) and watcher_pid > 0:
                try:
                    os.kill(watcher_pid, 0)
                    pid_alive = True
                except OSError:
                    pid_alive = False
            if pid_alive or (hb_ts and (time.time() - hb_ts) < 15.0):
                watcher_armed = not paused
        except Exception:
            pass

    # 3. Correlated Claude Code session transcript telemetry
    sid = _discover_session_for_file(filepath, fname, content)
    last_step_index = 0
    last_step_type = ""
    activity = odin_transcript.last_activity(odin_transcript.find_transcript(sid))
    last_tool_name = activity["lastToolName"]
    last_activity_ts = activity["lastActivityTs"]

    has_awaiting = bool(
        re.search(r'data-status=["\']awaiting_human["\']', content, re.IGNORECASE)
    )
    all_done = bool(
        re.search(r'id=["\']human-validation["\'][^>]*data-status=["\']done["\']', content, re.IGNORECASE)
    )
    recent_exec = bool(
        last_activity_ts
        and (time.time() - last_activity_ts) < 20.0
        and not watcher_armed
        and not all_done
    )

    if paused:
        agent_state = "paused"
        status_label = "⏸️ WATCHERS PAUSED"
    elif watcher_armed:
        agent_state = "armed"
        status_label = "🟢 PARTNER TURN (Armed & Listening in Odin)"
    elif recent_exec:
        agent_state = "executing"
        status_label = f"⚡ AGENT EXECUTING{f' ({last_tool_name})' if last_tool_name else ''}"
    elif has_awaiting:
        agent_state = "awaiting_human"
        status_label = "🟡 YOUR TURN IN ODIN (Click Save/Approve to Wake)"
    elif all_done:
        agent_state = "done"
        status_label = "✅ ALL PHASES COMPLETE"
    else:
        agent_state = "idle"
        status_label = "🛰️ TELEMETRY ONLINE"

    port_map = listeners if listeners is not None else get_listening_ports()
    staging_live = STAGING_PORT in port_map

    return {
        "ok": True,
        "slug": slug,
        "paused": paused,
        "watcherArmed": watcher_armed,
        "watcherPhase": watcher_phase,
        "watcherPid": watcher_pid,
        "sessionId": sid,
        "sessionIdShort": sid[:8] if sid else "",
        "lastStepIndex": last_step_index,
        "lastStepType": last_step_type,
        "lastToolName": last_tool_name,
        "lastActivityTs": last_activity_ts,
        "lastActivityAgo": _format_relative_ago(last_activity_ts),
        "agentState": agent_state,
        "statusLabel": status_label,
        "stagingLive": staging_live,
        "stagingAvailable": CONFIG["staging"]["available"],
        "stagingDir": CONFIG["staging"]["staging_dir"],
        "stagingBranch": CONFIG["staging"]["branch"],
    }


def read_git_head_branch(repo_path: str) -> str:
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


def get_cached_git_info(repo_path: str) -> dict:
    if not repo_path or not os.path.isdir(repo_path):
        return {"exists": False, "branch": "", "dirty": False, "dirtyCount": 0}
    now = time.time()
    cached = _GIT_CACHE.get(repo_path)
    if cached and (now - cached["ts"]) < _GIT_CACHE_TTL:
        return cached["data"]

    code_b, branch, _ = run_cmd(["git", "branch", "--show-current"], cwd=repo_path, timeout=3)
    branch_str = branch.strip() if code_b == 0 and branch.strip() else read_git_head_branch(repo_path)
    code_p, porcelain, _ = run_cmd(["git", "status", "--porcelain"], cwd=repo_path, timeout=3)
    dirty_lines = [ln for ln in porcelain.splitlines() if ln.strip()] if code_p == 0 else []
    data = {
        "exists": True,
        "branch": branch_str,
        "dirty": len(dirty_lines) > 0,
        "dirtyCount": len(dirty_lines),
    }
    _GIT_CACHE[repo_path] = {"ts": now, "data": data}
    return data


def strip_tags(raw_html: str) -> str:
    cleaned = re.sub(r"<style[\s\S]*?</style>", " ", raw_html, flags=re.IGNORECASE)
    cleaned = re.sub(r"<script[\s\S]*?</script>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<!--[\s\S]*?-->", " ", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


SECTION_ID_PATTERNS = {
    "huginn-plan": r"(?:huginn-plan|huginn-intake)",
    "huginn-intake": r"(?:huginn-plan|huginn-intake)",
}


def extract_section_info(content: str, section_id: str) -> dict:
    sec_pat = SECTION_ID_PATTERNS.get(section_id, re.escape(section_id))
    pattern = re.compile(
        rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*>)([\s\S]*?)(</section>)',
        re.IGNORECASE,
    )
    match = pattern.search(content)
    if not match:
        default_msg = "Awaiting human sign-off" if section_id == "human-validation" else "Not started"
        return {"status": "pending", "summary": default_msg, "latestNote": ""}

    open_tag, inner, _ = match.groups()
    status_match = re.search(r'data-status=["\']([^"\']+)["\']', open_tag, re.IGNORECASE)
    status = status_match.group(1).lower() if status_match else ""

    note_match = re.search(r'data-human-note=["\']([^"\']*)["\']', open_tag, re.IGNORECASE)
    latest_note = html_lib.unescape(note_match.group(1)).strip() if note_match else ""

    if status not in ("done", "active", "awaiting_human", "reject", "pending") or status == "pending":
        header_slice = inner[:450]
        if "badge-reject" in header_slice:
            status = "reject"
        elif "badge-awaiting_human" in header_slice or "badge-questions" in header_slice:
            status = "awaiting_human"
        elif "badge-done" in header_slice:
            status = "done"
        elif "badge-active" in header_slice:
            status = "active"
        elif not status:
            status = "pending"

    body_only = re.sub(
        r'<div\s+class=["\']phase-header["\'][\s\S]*?</div>',
        " ",
        inner,
        count=1,
        flags=re.IGNORECASE,
    )
    body_only = re.sub(r"<h2[^>]*>[\s\S]*?</h2>", " ", body_only, count=1, flags=re.IGNORECASE)
    iter_cards = re.findall(
        r'<details\b[^>]*class=["\'][^"\']*odin-iteration-card[^"\']*["\'][^>]*>([\s\S]*?)</details>',
        body_only,
        flags=re.IGNORECASE,
    )
    target_snippet = iter_cards[-1] if iter_cards else body_only
    summary = latest_note if (section_id == "human-validation" and latest_note) else strip_tags(target_snippet)
    if len(summary) > 140:
        summary = summary[:137] + "..."
    fallback = "Awaiting human sign-off" if section_id == "human-validation" else "Awaiting phase output..."
    return {"status": status, "summary": summary or fallback, "latestNote": latest_note}


def extract_body_env(content: str, filepath: str, workspace: str) -> dict:
    body_m = re.search(r"<body\b[^>]*>", content, re.IGNORECASE)
    body_tag = body_m.group(0) if body_m else ""

    def get_attr(name: str) -> tuple[bool, str]:
        m = re.search(rf'{name}=["\']([^"\']*)["\']', body_tag, re.IGNORECASE)
        if not m:
            return False, ""
        return True, html_lib.unescape(m.group(1)).strip()

    has_branch, branch = get_attr("data-branch")
    has_worktree, worktree = get_attr("data-worktree")
    has_app_port, app_port_str = get_attr("data-app-port")
    has_backend_port, backend_port_str = get_attr("data-backend-port")

    # Fallback inference only when attributes are not explicitly set on <body>
    head_slice = content[:3000]
    repos = CONFIG.get("repos") or {}
    if not has_worktree and not worktree:
        named = [r for name, r in repos.items() if r.get("path") and name in head_slice]
        if named:
            worktree = named[0]["path"]
        elif workspace != "(root)":
            candidate = os.path.join(ROOT_DIR, workspace)
            if os.path.exists(os.path.join(candidate, ".git")):
                worktree = candidate

    if not has_branch and not branch:
        m_br = re.search(
            r"\b((?:feat|fix|chore|refactor|hotfix|release)/[A-Za-z0-9._/-]+)",
            head_slice,
        )
        if m_br:
            branch = m_br.group(1)

    # Infer a default port from the configured repo whose path (or path-<port> worktree) matches
    if worktree:
        for repo in repos.values():
            rpath = (repo.get("path") or "").rstrip("/")
            if not rpath:
                continue
            m_port = re.fullmatch(re.escape(rpath) + r"(?:-(\d{4}))?", worktree.rstrip("/"))
            if not m_port:
                continue
            port_range = repo.get("port_range") or []
            default_port = m_port.group(1) or (str(port_range[0]) if port_range else "")
            if repo.get("role") == "backend":
                if not has_backend_port and not backend_port_str:
                    backend_port_str = default_port
            elif not has_app_port and not app_port_str:
                app_port_str = default_port
            break

    app_port = int(app_port_str) if app_port_str.isdigit() else None
    backend_port = int(backend_port_str) if backend_port_str.isdigit() else None

    git_info = get_cached_git_info(worktree) if worktree else {"exists": False, "branch": "", "dirty": False, "dirtyCount": 0}
    live_branch = branch or git_info["branch"]

    worktree_short = os.path.basename(worktree.rstrip("/")) if worktree else ""
    is_secondary = bool(worktree_short and re.search(r"-\d{4}$", worktree_short))

    return {
        "branch": live_branch,
        "declaredBranch": branch,
        "worktree": worktree,
        "worktreeShort": worktree_short,
        "worktreeExists": git_info["exists"],
        "worktreeDirty": git_info["dirty"],
        "worktreeDirtyCount": git_info["dirtyCount"],
        "isSecondaryWorktree": is_secondary,
        "appPort": app_port,
        "backendPort": backend_port,
    }


def extract_iteration_ledger_summary(content: str) -> dict:
    m = re.search(
        r'<script[^>]*id=["\']odin-iteration-ledger["\'][^>]*>([\s\S]*?)</script>',
        content,
        re.IGNORECASE,
    )
    if m:
        try:
            ledger = json.loads(m.group(1).strip())
            iterations = ledger.get("iterations") or []
            cur_iter = int(ledger.get("current_iteration") or len(iterations) or 1)
            max_iter = int(ledger.get("max_iterations") or max(5, cur_iter))
            deltas = []
            for it_obj in iterations:
                for d in it_obj.get("harness_delta_proposals") or []:
                    deltas.append(d)
            for d in ledger.get("harness_delta_proposals") or []:
                if not any(x.get("id") == d.get("id") for x in deltas):
                    deltas.append(d)
            pending = sum(1 for d in deltas if d.get("status", "pending") == "pending")
            cur_phases = {}
            for it_obj in iterations:
                if int(it_obj.get("iteration") or 0) == cur_iter:
                    cur_phases = it_obj.get("phases") or {}
                    break
            if not cur_phases and iterations:
                cur_phases = iterations[-1].get("phases") or {}
            return {
                "hasLedger": True,
                "currentIteration": cur_iter,
                "maxIterations": max_iter,
                "iterationCount": max(cur_iter, len(iterations)),
                "deltaCount": len(deltas),
                "pendingDeltaCount": pending,
                "harnessDeltas": deltas,
                "currentPhases": cur_phases,
            }
        except Exception:
            pass

    iter_m = re.search(r'id=["\']iteration-counter["\'][^>]*>\s*(\d+)\s*/\s*(\d+)', content, re.IGNORECASE)
    cur_iter = int(iter_m.group(1)) if iter_m else 1
    max_iter = int(iter_m.group(2)) if iter_m else 5
    cards = re.findall(r'class=["\'][^"\']*odin-iteration-card', content, re.IGNORECASE)
    return {
        "hasLedger": False,
        "currentIteration": cur_iter,
        "maxIterations": max_iter,
        "iterationCount": cur_iter if cur_iter > 1 else (1 if not cards else max(1, len(cards) // 3)),
        "deltaCount": 0,
        "pendingDeltaCount": 0,
        "harnessDeltas": [],
    }


def parse_project_file(filepath: str, listeners: dict[int, dict]) -> dict:
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return {}

    rel_path = os.path.relpath(filepath, ROOT_DIR)
    parts = rel_path.split(os.sep)
    archived = "archive" in parts and "odin-states" in parts

    if "odin-states" in parts:
        idx = parts.index("odin-states")
        workspace = "/".join(parts[:idx]) if idx > 0 else "(root)"
    else:
        workspace = os.path.dirname(rel_path) or "(root)"

    filename = os.path.basename(filepath)
    slug = filename[:-5] if filename.endswith(".html") else filename

    title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", content, re.IGNORECASE)
    raw_title = strip_tags(title_match.group(1)) if title_match else slug
    clean_title = re.sub(
        r"^(?:✅\s*\[DONE\]\s*|👑\s*\[AWAITING SIGN-OFF\]\s*|⚡\s*\[ACTIVE\]\s*|❓\s*\[OPEN QUESTIONS\]\s*|🚨\s*\[REJECT\]\s*|⏳\s*)?"
        r"(?:⚡\s*)?(?:Odin\s+Command\s+Center|Odin\s+State\s+Tracker|ODIN\s+ALLFATHER|ODIN\s*//)\s*[—\-:]?\s*",
        "",
        raw_title,
        flags=re.IGNORECASE,
    ).strip() or slug

    ledger_info = extract_iteration_ledger_summary(content)
    ledger_phase_map = {
        "huginn-plan": "huginn",
        "thor-execution": "thor",
        "heimdall-validation": "heimdall",
        "hlidskjalf-judge": "hlidskjalf",
        "human-validation": "human",
    }
    ledger_phases = ledger_info.get("currentPhases") or {}

    phases = {}
    ai_phase_keys = ["huginn-plan", "thor-execution", "heimdall-validation", "hlidskjalf-judge"]
    done_count = 0
    for sec_id, _ in PHASE_IDS:
        info = extract_section_info(content, sec_id)
        l_key = ledger_phase_map.get(sec_id, "")
        l_entry = ledger_phases.get(l_key) if isinstance(ledger_phases, dict) else None
        if info["status"] == "pending" and isinstance(l_entry, dict):
            l_status = str(l_entry.get("status") or "").strip().lower()
            if l_status in ("active", "done", "awaiting_human", "reject"):
                info["status"] = l_status
                if info["summary"] in ("Not started", "") and l_entry.get("summary"):
                    info["summary"] = str(l_entry.get("summary"))[:140]
        phases[sec_id] = info
        if sec_id in ai_phase_keys and info["status"] == "done":
            done_count += 1

    stat = os.stat(filepath)
    ag_questions = detect_open_questions(filepath, filename, content)
    statuses = [phases[k]["status"] for k, _ in PHASE_IDS]
    ai_statuses = [phases[k]["status"] for k in ai_phase_keys]

    # If the Claude Code session has unanswered open questions and the AI phases are not yet done/rejected,
    # promote the first non-done AI phase to awaiting_human so the dashboard and stepper reflect it.
    is_finished = (
        phases["human-validation"]["status"] == "done"
        or (phases["hlidskjalf-judge"]["status"] == "done" and done_count == 4)
        or done_count == 4
    )
    if (ag_questions["awaitingHuman"] or ag_questions["openQuestions"]) and not is_finished and "reject" not in statuses:
        if "awaiting_human" not in ai_statuses:
            for k in ai_phase_keys:
                if phases[k]["status"] in ("pending", "active"):
                    phases[k]["status"] = "awaiting_human"
                    if ag_questions["openQuestions"]:
                        phases[k]["summary"] = f"Open Questions ({len(ag_questions['openQuestions'])}): " + " | ".join(
                            ag_questions["openQuestions"]
                        )[:115]
                    break
            statuses = [phases[k]["status"] for k, _ in PHASE_IDS]
            ai_statuses = [phases[k]["status"] for k in ai_phase_keys]
    elif not ag_questions["awaitingHuman"] and "awaiting_human" in ai_statuses:
        # Close the loop: if the HTML file has an AI phase marked awaiting_human,
        # but the correlated Claude Code session shows the user already answered the question
        # after the HTML file was written (or another AI phase is already active), auto-demote awaiting_human -> active/pending/done.
        if "active" in ai_statuses:
            first_active_idx = next(i for i, k in enumerate(ai_phase_keys) if phases[k]["status"] == "active")
            for i, k in enumerate(ai_phase_keys):
                if phases[k]["status"] == "awaiting_human":
                    phases[k]["status"] = "done" if i < first_active_idx else "pending"
            done_count = sum(1 for k in ai_phase_keys if phases[k]["status"] == "done")
            statuses = [phases[k]["status"] for k, _ in PHASE_IDS]
            ai_statuses = [phases[k]["status"] for k in ai_phase_keys]
        elif ag_questions.get("questionsResolved") and ag_questions.get("lastResolutionTs", 0.0) >= stat.st_mtime - 2.0:
            for k in ai_phase_keys:
                if phases[k]["status"] == "awaiting_human":
                    phases[k]["status"] = "active"
            statuses = [phases[k]["status"] for k, _ in PHASE_IDS]
            ai_statuses = [phases[k]["status"] for k in ai_phase_keys]

    if "reject" in statuses:
        overall = "reject"
    elif "awaiting_human" in ai_statuses:
        overall = "awaiting_human"
    elif "active" in ai_statuses:
        overall = "active"
    elif is_finished:
        overall = "done"
    elif "done" in ai_statuses:
        overall = "active"
    else:
        overall = "pending"

    env = extract_body_env(content, filepath, workspace)
    app_port = env["appPort"]
    backend_port = env["backendPort"]
    ledger_info = extract_iteration_ledger_summary(content)

    mtime_iso = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    is_v2 = ("bifrost-stepper" in content and "odin-favicon" in content) or ledger_info["hasLedger"]
    telemetry = get_session_telemetry(filepath, filename, content, listeners=listeners)

    url_path = "/".join(urllib.parse.quote(p) for p in parts)
    return {
        "id": rel_path,
        "slug": slug,
        "filename": filename,
        "title": clean_title,
        "workspace": workspace,
        "archived": archived,
        "overallStatus": overall,
        "doneCount": done_count,
        "progressPct": int(round((done_count / 4.0) * 100)),
        "phases": phases,
        "humanStatus": phases["human-validation"]["status"],
        "humanNote": phases["human-validation"].get("latestNote", ""),
        "sessionId": ag_questions["sessionId"] or telemetry.get("sessionId", ""),
        "telemetry": telemetry,
        "stagingLive": telemetry.get("stagingLive", False),
        "awaitingHuman": overall == "awaiting_human",
        "awaitingSignoff": bool(is_finished and phases["human-validation"]["status"] in ("awaiting_human", "pending", "active")),
        "openQuestions": ag_questions["openQuestions"] if overall == "awaiting_human" else [],
        "planFile": ag_questions["planFile"],
        "currentIteration": ledger_info["currentIteration"],
        "maxIterations": ledger_info["maxIterations"],
        "iterationCount": ledger_info["iterationCount"],
        "deltaCount": ledger_info["deltaCount"],
        "pendingDeltaCount": ledger_info["pendingDeltaCount"],
        "harnessDeltas": ledger_info["harnessDeltas"],
        "branch": env["branch"],
        "worktree": env["worktree"],
        "worktreeShort": env["worktreeShort"],
        "worktreeExists": env["worktreeExists"],
        "worktreeDirty": env["worktreeDirty"],
        "worktreeDirtyCount": env["worktreeDirtyCount"],
        "isSecondaryWorktree": env["isSecondaryWorktree"],
        "appPort": app_port,
        "appPortLive": bool(app_port and app_port in listeners),
        "backendPort": backend_port,
        "backendPortLive": bool(backend_port and backend_port in listeners),
        "collisionWarning": "",
        "mtime": stat.st_mtime,
        "mtimeIso": mtime_iso,
        "sizeBytes": stat.st_size,
        "isV2": is_v2,
        "url": f"/{url_path}",
        "fileUri": f"file://{filepath}",
    }


def _project_sort_priority(p: dict) -> int:
    """
    Return status priority tier so open/in-flight projects always sort before completed & archived projects:
      0 = Open & Action Required (reject, awaiting_human / openQuestions)
      1 = Open & In-Progress (active, pending AI phases)
      2 = Open & Awaiting Sign-Off (4/4 AI phases done, humanStatus != 'done')
      3 = Completed & Signed Off (overallStatus == 'done' and humanStatus == 'done', unarchived)
      4 = Archived Vault (archived == True)
    """
    if p.get("archived"):
        return 4
    overall = p.get("overallStatus", "pending")
    human = p.get("humanStatus", "pending")
    awaiting_signoff = bool(p.get("awaitingSignoff") or (overall == "done" and human != "done"))
    if overall in ("reject", "awaiting_human") or human == "reject" or p.get("awaitingHuman"):
        return 0
    if overall in ("active", "pending"):
        return 1
    if awaiting_signoff:
        return 2
    if overall == "done":
        return 3
    return 1


def discover_projects() -> list:
    listeners = get_listening_ports()
    patterns = [
        os.path.join(ROOT_DIR, "odin-states", "*.html"),
        os.path.join(ROOT_DIR, "odin-states", "archive", "*.html"),
        os.path.join(ROOT_DIR, "*", "odin-states", "*.html"),
        os.path.join(ROOT_DIR, "*", "odin-states", "archive", "*.html"),
        os.path.join(ROOT_DIR, "*", "*", "odin-states", "*.html"),
        os.path.join(ROOT_DIR, "*", "*", "odin-states", "archive", "*.html"),
        os.path.join(ROOT_DIR, "*", "*", "*", "odin-states", "*.html"),
        os.path.join(ROOT_DIR, "*", "*", "*", "odin-states", "archive", "*.html"),
    ]
    seen = set()
    projects = []
    for pat in patterns:
        for path in glob.glob(pat):
            real = os.path.realpath(path)
            if real in seen:
                continue
            if os.path.basename(path).lower() == "index.html":
                continue
            if not ROOT_DIR.rstrip("/").endswith("odin-staging") and "/odin-staging/" in path.replace("\\", "/"):
                continue
            seen.add(real)
            info = parse_project_file(path, listeners)
            if info:
                projects.append(info)

    # Detect port or worktree collisions across active (unarchived, non-done) projects
    in_flight = [
        p
        for p in projects
        if not p["archived"] and p["overallStatus"] in ("active", "awaiting_human", "reject", "pending")
    ]
    for i, p1 in enumerate(in_flight):
        warnings = []
        for j, p2 in enumerate(in_flight):
            if i == j:
                continue
            if p1["appPort"] and p1["appPort"] == p2["appPort"]:
                warnings.append(f"Port :{p1['appPort']} also claimed by {p2['slug']}")
            if p1["worktree"] and p1["worktree"] == p2["worktree"] and p1["branch"] and p2["branch"] and p1["branch"] != p2["branch"]:
                warnings.append(f"Worktree {p1['worktreeShort']} shared with {p2['slug']} ({p2['branch']})")
        if warnings:
            p1["collisionWarning"] = " • ".join(warnings)

    for p in projects:
        p["sortPriority"] = _project_sort_priority(p)
        p["isOpen"] = p["sortPriority"] < 3

    projects.sort(key=lambda p: (p["sortPriority"], -float(p.get("mtime") or 0.0)))
    return projects


def set_body_env_attributes(content: str, branch: str, worktree: str, app_port: str, backend_port: str) -> str:
    attrs = [
        ("data-branch", branch),
        ("data-worktree", worktree),
        ("data-app-port", str(app_port or "")),
        ("data-backend-port", str(backend_port or "")),
    ]

    def repl_body(match):
        tag = match.group(0)
        for attr_name, val in attrs:
            escaped = html_lib.escape(str(val or ""), quote=True)
            if re.search(rf'{attr_name}=["\'][^"\']*["\']', tag, re.IGNORECASE):
                tag = re.sub(
                    rf'{attr_name}=["\'][^"\']*["\']',
                    f'{attr_name}="{escaped}"',
                    tag,
                    flags=re.IGNORECASE,
                )
            else:
                tag = tag[:-1] + f' {attr_name}="{escaped}">'
        return tag

    content = re.sub(r"<body\b[^>]*>", repl_body, content, count=1, flags=re.IGNORECASE)

    # Also update or inject #odin-env-bar inside <main class="container">
    wt_short = os.path.basename(worktree.rstrip("/")) if worktree else "unassigned"
    env_bar_html = (
        f'<div id="odin-env-bar" style="display:flex;flex-wrap:wrap;gap:0.5rem;align-items:center;'
        f'padding:0.65rem 1rem;margin-bottom:1.25rem;border-radius:12px;'
        f'background:rgba(56,189,248,0.08);border:1px solid rgba(56,189,248,0.28);font-size:0.78rem;'
        f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">'
        f'<span>🌿 <strong>Branch:</strong> {html_lib.escape(branch or "unassigned")}</span>'
        f'<span style="opacity:0.4;">|</span>'
        f'<span>🗂️ <strong>Worktree:</strong> {html_lib.escape(wt_short)}</span>'
        f'<span style="opacity:0.4;">|</span>'
        f'<span>🔌 <strong>App port:</strong> :{html_lib.escape(str(app_port or "—"))}</span>'
        f'<span style="opacity:0.4;">|</span>'
        f'<span>🧩 <strong>Backend port:</strong> :{html_lib.escape(str(backend_port or "—"))}</span>'
        f'</div>'
    )
    if 'id="odin-env-bar"' in content:
        content = re.sub(
            r'<div\s+id=["\']odin-env-bar["\'][\s\S]*?</div>',
            env_bar_html,
            content,
            count=1,
            flags=re.IGNORECASE,
        )
    elif '<nav class="bifrost-stepper"' in content:
        content = content.replace(
            '<nav class="bifrost-stepper"',
            env_bar_html + '\n    <nav class="bifrost-stepper"',
            1,
        )
    return content


def ensure_human_validation_section(content: str, status: str = "pending") -> str:
    pattern = re.compile(r'<section\b[^>]*\bid=["\']human-validation["\']', re.IGNORECASE)
    if pattern.search(content):
        return content

    badge = BADGE_HTML.get(status, BADGE_HTML["pending"])
    new_sec = f"""
    <section id="human-validation" data-status="{status}">
      <div class="phase-header">
        <h2 class="phase-title">👑 Phase 5: Human Validation (Allfather Sign-Off)</h2>
        {badge}
      </div>
      <p>Human validation and rejection notes recorded from the Odin Command Dashboard.</p>
    </section>
"""
    if "</main>" in content:
        return content.replace("</main>", new_sec + "\n  </main>", 1)
    if "<script" in content:
        return content.replace("<script", new_sec + "\n  <script", 1)
    if "</body>" in content:
        return content.replace("</body>", new_sec + "\n</body>", 1)
    return content + new_sec


def update_section_in_html(content: str, section_id: str, new_status: str, note: str = "") -> str:
    if section_id == "human-validation":
        content = ensure_human_validation_section(content, new_status)

    sec_pat = SECTION_ID_PATTERNS.get(section_id, re.escape(section_id))
    pattern = re.compile(
        rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*>)([\s\S]*?)(</section>)',
        re.IGNORECASE,
    )

    def repl(match):
        open_tag, inner, close_tag = match.groups()
        if re.search(r'data-status=["\'][^"\']*["\']', open_tag, re.IGNORECASE):
            open_tag = re.sub(
                r'data-status=["\'][^"\']*["\']',
                f'data-status="{new_status}"',
                open_tag,
                flags=re.IGNORECASE,
            )
        else:
            open_tag = open_tag[:-1] + f' data-status="{new_status}">'

        if section_id == "human-validation" and note:
            escaped_attr = html_lib.escape(note, quote=True)
            if re.search(r'data-human-note=["\'][^"\']*["\']', open_tag, re.IGNORECASE):
                open_tag = re.sub(
                    r'data-human-note=["\'][^"\']*["\']',
                    f'data-human-note="{escaped_attr}"',
                    open_tag,
                    flags=re.IGNORECASE,
                )
            else:
                open_tag = open_tag[:-1] + f' data-human-note="{escaped_attr}">'

        if section_id == "human-validation" and new_status == "awaiting_human":
            badge_replacement = '<span class="badge-awaiting_human">👑 AWAITING SIGN-OFF</span>'
        else:
            badge_replacement = BADGE_HTML.get(new_status, BADGE_HTML["pending"])
        inner = re.sub(
            r'<span\s+class=["\']badge-(?:done|active|awaiting_human|questions|pending|reject)["\'][^>]*>[\s\S]*?</span>',
            badge_replacement,
            inner,
            count=1,
            flags=re.IGNORECASE,
        )

        if note:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            escaped_note = html_lib.escape(note)
            if section_id == "human-validation":
                if new_status == "reject":
                    verdict_box = (
                        f'\n      <div class="human-verdict-box" style="margin-top:0.85rem;padding:0.85rem 1rem;'
                        f'border-radius:10px;border-left:4px solid #f43f5e;background:rgba(244,63,94,0.12);">'
                        f'<strong>🚨 REJECTED BY HUMAN [{ts}]:</strong> {escaped_note}</div>\n    '
                    )
                elif new_status == "done":
                    verdict_box = (
                        f'\n      <div class="human-verdict-box" style="margin-top:0.85rem;padding:0.85rem 1rem;'
                        f'border-radius:10px;border-left:4px solid #10b981;background:rgba(16,185,129,0.12);">'
                        f'<strong>✅ APPROVED BY HUMAN [{ts}]:</strong> {escaped_note}</div>\n    '
                    )
                else:
                    verdict_box = (
                        f'\n      <div class="human-verdict-box" style="margin-top:0.85rem;padding:0.85rem 1rem;'
                        f'border-radius:10px;border-left:4px solid #f59e0b;background:rgba(245,158,11,0.12);">'
                        f'<strong>👑 HUMAN VALIDATION NOTE [{ts}]:</strong> {escaped_note}</div>\n    '
                    )
                inner = inner.rstrip() + verdict_box
            else:
                inner = inner.rstrip() + f'\n      <p><strong>[{ts}]</strong> {escaped_note}</p>\n    '

        return open_tag + inner + close_tag

    updated = pattern.sub(repl, content, count=1)

    if section_id == "human-validation":
        ledger_m = re.search(
            r'(<script[^>]*id=["\']odin-iteration-ledger["\'][^>]*>)([\s\S]*?)(</script>)',
            updated,
            re.IGNORECASE,
        )
        if ledger_m:
            try:
                ledger = json.loads(ledger_m.group(2).strip())
                cur_iter = int(ledger.get("current_iteration", 1))
                ts_iso = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                for it_obj in ledger.get("iterations", []):
                    if int(it_obj.get("iteration", 0)) == cur_iter:
                        it_obj["status"] = new_status
                        it_obj.setdefault("phases", {})["human"] = {
                            "status": new_status,
                            "note": note,
                            "updated_at": ts_iso,
                        }
                        if new_status == "reject":
                            it_obj["human_rejection_note"] = note
                            it_obj["human_rejected_at"] = ts_iso
                new_json = json.dumps(ledger, indent=2, ensure_ascii=False)
                updated = (
                    updated[: ledger_m.start()]
                    + ledger_m.group(1)
                    + "\n"
                    + new_json
                    + "\n  "
                    + ledger_m.group(3)
                    + updated[ledger_m.end() :]
                )
            except Exception:
                pass

    return updated


_ODIN_STATE_MOD = None
_ODIN_STATE_MTIME = 0.0


def _get_odin_state_module():
    global _ODIN_STATE_MOD, _ODIN_STATE_MTIME
    state_script = os.path.join(SKILL_DIR, "scripts", "odin-state.py")
    try:
        cur_mtime = os.path.getmtime(state_script)
    except OSError:
        cur_mtime = 0.0
    if _ODIN_STATE_MOD is not None and cur_mtime == _ODIN_STATE_MTIME:
        return _ODIN_STATE_MOD
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("odin_state", state_script)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _ODIN_STATE_MOD = mod
            _ODIN_STATE_MTIME = cur_mtime
            return mod
    except Exception:
        pass
    return _ODIN_STATE_MOD


def get_mimir_audit_payload() -> dict:
    mod = _get_odin_state_module()
    if mod and hasattr(mod, "run_mimir_audit"):
        try:
            return mod.run_mimir_audit(ROOT_DIR)
        except Exception:
            pass
    state_script = os.path.join(SKILL_DIR, "scripts", "odin-state.py")
    code, out, err = run_cmd(["python3", state_script, "mimir-audit", "--dir", ROOT_DIR, "--format", "json"])
    if code == 0 and out:
        try:
            return json.loads(out)
        except Exception:
            pass
    empty_mimir = {"total_deltas": 0, "pending_deltas": 0, "pending_proposals": 0, "applied_deltas": 0, "proposals": []}
    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "kvasir_telemetry": {
            "total_projects": 0,
            "projects_scanned": 0,
            "multi_iteration_projects": 0,
            "total_iterations": 0,
            "avg_iterations_per_project": 0,
            "step_back_rate_pct": 0,
            "rejection_breakdown": {"human": 0, "ci": 0, "heimdall": 0, "other": 0},
            "weakness_categories": {"testing": 0, "architecture": 0, "workflow": 0, "skill_gap": 0},
        },
        "mimir_synthesis": empty_mimir,
        "mimir_proposals": empty_mimir,
        "projects": [],
    }


def resolve_safe_project_path(rel_id: str) -> str:
    clean = (rel_id or "").strip()
    root_prefix = ROOT_DIR.lstrip("/") + "/"
    if clean.lstrip("/").startswith(root_prefix):
        clean = clean.lstrip("/")[len(root_prefix) :]
    target = os.path.abspath(os.path.join(ROOT_DIR, clean.lstrip("/")))
    if not target.startswith(ROOT_DIR + os.sep):
        raise ValueError("Path outside workspace root")
    if not os.path.isfile(target):
        fallback = os.path.abspath(os.path.join(ROOT_DIR, "odin-states", os.path.basename(clean)))
        if fallback.startswith(ROOT_DIR + os.sep) and os.path.isfile(fallback):
            return fallback
        raise FileNotFoundError(f"Project file not found: {rel_id}")
    return target


# Browser tabs that can show OS notifications ping /api/presence on every poll.
# Scope "*" is the dashboard (covers every task); otherwise the state page's URL path.
PRESENCE_TTL = 15.0
_PRESENCE: dict[str, float] = {}
_PRESENCE_LOCK = threading.Lock()


def state_file_url_path(file_path: str) -> str:
    """URL path the server serves a state file at, or "/" when it lies outside ROOT_DIR."""
    target = os.path.abspath(file_path or "")
    if not target.startswith(ROOT_DIR + os.sep):
        return "/"
    return "/" + urllib.parse.quote(os.path.relpath(target, ROOT_DIR).replace(os.sep, "/"))


def record_presence(scope: str, now: float | None = None) -> None:
    scope = (scope or "").strip()
    if not scope:
        return
    if scope != "*":
        scope = urllib.parse.quote(urllib.parse.unquote(scope))
    with _PRESENCE_LOCK:
        _PRESENCE[scope] = time.time() if now is None else now


def presence_covers(file_path: str, now: float | None = None) -> bool:
    """True when a notification-enabled dashboard, or this file's page, pinged recently."""
    now = time.time() if now is None else now
    scopes = ["*"]
    if file_path:
        scopes.append(state_file_url_path(file_path))
    with _PRESENCE_LOCK:
        return any(now - _PRESENCE.get(s, 0.0) <= PRESENCE_TTL for s in scopes if s != "/")


def archive_or_restore_file(filepath: str, to_archive: bool) -> str:
    parent = os.path.dirname(filepath)
    filename = os.path.basename(filepath)
    if to_archive:
        if os.path.basename(parent) == "archive":
            return filepath
        dest_dir = os.path.join(parent, "archive")
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)
    else:
        if os.path.basename(parent) != "archive":
            return filepath
        dest_dir = os.path.dirname(parent)
        dest_path = os.path.join(dest_dir, filename)

    shutil.move(filepath, dest_path)
    return dest_path


DEFAULT_DEV_PORT_RANGES = [(3000, 3099), (8080, 8099)]


def dev_port_ranges() -> list:
    """Port ranges Odin may inspect or stop: each configured repo's port_range, else common dev defaults."""
    ranges = []
    for repo in (CONFIG.get("repos") or {}).values():
        pr = repo.get("port_range") or []
        if len(pr) == 2:
            ranges.append((int(pr[0]), int(pr[1])))
    return ranges or DEFAULT_DEV_PORT_RANGES


def is_dev_port(port: int) -> bool:
    return any(lo <= port <= hi for lo, hi in dev_port_ranges())


def stop_dev_port(port: int) -> dict:
    """Safely terminate a process listening on a configured dev port."""
    if not is_dev_port(port):
        human = ", ".join(f"{lo}-{hi}" for lo, hi in dev_port_ranges())
        raise ValueError(f"Port {port} is outside the allowed dev-server range ({human}).")
    listeners = get_listening_ports()
    info = listeners.get(port)
    if not info:
        return {"stopped": False, "port": port, "message": f"Port :{port} is already stopped."}
    pid = info["pid"]
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.25)
    return {"stopped": True, "port": port, "pid": pid, "message": f"Stopped process {pid} on port :{port}."}


def prune_secondary_worktree(worktree_path: str) -> dict:
    """Safely remove a secondary sibling worktree if clean."""
    if not worktree_path:
        raise ValueError("No worktree path specified.")
    abs_wt = os.path.abspath(worktree_path)
    if not abs_wt.startswith(ROOT_DIR + os.sep):
        raise ValueError("Worktree path outside workspace root.")
    short = os.path.basename(abs_wt)
    if not re.search(r"-\d{4}$", short):
        raise ValueError(f"Refusing to prune primary repository '{short}'. Only secondary-<port> worktrees can be pruned.")
    if not os.path.exists(abs_wt):
        return {"pruned": False, "message": f"Worktree {short} already removed."}

    git_info = get_cached_git_info(abs_wt)
    if git_info.get("dirty"):
        raise ValueError(
            f"Git Hygiene Guard: Worktree '{short}' has {git_info['dirtyCount']} uncommitted/untracked file(s). "
            f"Commit or clean changes before pruning."
        )

    primary_repo = re.sub(r"-\d{4}$", "", abs_wt)
    code, out, err = run_cmd(["git", "worktree", "remove", abs_wt], cwd=primary_repo if os.path.isdir(primary_repo) else ROOT_DIR)
    if code != 0:
        raise RuntimeError(err or out or f"git worktree remove failed for {short}")
    _GIT_CACHE.pop(abs_wt, None)
    return {"pruned": True, "worktree": short, "message": f"Pruned clean worktree {short}."}


class OdinHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT_DIR, **kwargs)

    def send_json(self, payload: dict, status: int = 200):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        clean_path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        if clean_path == "/go":
            self.send_response(302)
            self.send_header("Location", state_file_url_path(query.get("file", [""])[0]))
            self.end_headers()
            return

        if clean_path == "/api/presence":
            return self.send_json({"ok": True, "covered": presence_covers(query.get("file", [""])[0])})

        if clean_path == "/api/session-telemetry":
            rel_id = query.get("id", [""])[0]
            try:
                fp = resolve_safe_project_path(rel_id) if rel_id else ""
            except Exception:
                fp = ""
            if fp and os.path.isfile(fp):
                return self.send_json(get_session_telemetry(fp, os.path.basename(fp)))
            # Fallback to global pause + staging status
            mod = _get_odin_state_module()
            pause_info = mod.get_pause_state() if (mod and hasattr(mod, "get_pause_state")) else {"paused": False}
            listeners = get_listening_ports()
            return self.send_json({
                "ok": True,
                "paused": bool(pause_info.get("paused")),
                "stagingLive": STAGING_PORT in listeners,
                "stagingAvailable": CONFIG["staging"]["available"],
                "stagingDir": CONFIG["staging"]["staging_dir"],
                "stagingBranch": CONFIG["staging"]["branch"],
                "agentState": "paused" if pause_info.get("paused") else "idle",
                "statusLabel": "⏸️ WATCHERS PAUSED" if pause_info.get("paused") else "🛰️ TELEMETRY ONLINE",
            })

        if clean_path == "/api/skills":
            mod = _get_odin_state_module()
            skills = list(getattr(mod, "DEFAULT_SKILL_PILLS", [])) if mod else []
            return self.send_json({"ok": True, "skills": skills})

        if clean_path == "/api/mimir/audit":
            return self.send_json({
                "ok": True,
                "audit": get_mimir_audit_payload(),
            })

        if clean_path == "/api/telemetry":
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            if force or not _TELEMETRY_CACHE["environments"]:
                telemetry_data = refresh_telemetry(force=force)
            else:
                telemetry_data = get_telemetry_snapshot()
            return self.send_json({
                "ok": True,
                "telemetry": telemetry_data,
            })

        if clean_path == "/api/projects":
            projects = discover_projects()
            listeners = get_listening_ports()
            dev_ports = {str(p): v for p, v in listeners.items() if is_dev_port(p)}
            mod = _get_odin_state_module()
            pause_info = mod.get_pause_state() if (mod and hasattr(mod, "get_pause_state")) else {"paused": False}
            return self.send_json({
                "ok": True,
                "projects": projects,
                "activeDevPorts": dev_ports,
                "stagingLive": STAGING_PORT in listeners,
                "stagingAvailable": CONFIG["staging"]["available"],
                "stagingDir": CONFIG["staging"]["staging_dir"],
                "stagingBranch": CONFIG["staging"]["branch"],
                "pausedLive": bool(pause_info.get("paused")),
                "telemetry": get_telemetry_snapshot(),
                "mimirAudit": get_mimir_audit_payload(),
                "rootDir": ROOT_DIR,
            })

        if clean_path in ("/", "/odin-states", "/odin-states/", "/odin-states/index.html"):
            workspace_index = os.path.join(ROOT_DIR, "odin-states", "index.html")
            target_dashboard = DASHBOARD_HTML_PATH if os.path.isfile(DASHBOARD_HTML_PATH) else workspace_index
            if os.path.isfile(target_dashboard):
                with open(target_dashboard, "r", encoding="utf-8") as f:
                    data = render_ports(f.read(), CONFIG).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        if clean_path.endswith(".html") and "odin-states" in clean_path:
            target_fp = self.translate_path(self.path)
            if os.path.isfile(target_fp):
                try:
                    with open(target_fp, "r", encoding="utf-8", errors="replace") as f:
                        html_text = f.read()
                    mtime = os.path.getmtime(target_fp)
                    ag_q = detect_open_questions(target_fp, os.path.basename(target_fp), html_text)
                    if not ag_q["awaitingHuman"] and ag_q.get("questionsResolved") and ag_q.get("lastResolutionTs", 0.0) >= mtime - 2.0:
                        html_text = re.sub(
                            r'(<section\b[^>]*\bid=["\'](?:huginn-plan|huginn-intake|thor-execution|heimdall-validation|hlidskjalf-judge)["\'][^>]*\bdata-status=["\'])awaiting_human(["\'])',
                            r"\g<1>active\g<2>",
                            html_text,
                            flags=re.IGNORECASE,
                        )
                        html_text = re.sub(
                            r'(<div\b(?![^>]*\bdata-resolved=)[^>]*\bclass=["\'][^"\']*open-questions-box[^"\']*["\'])',
                            r'\1 data-resolved="true"',
                            html_text,
                            flags=re.IGNORECASE,
                        )
                    mod = _get_odin_state_module()
                    if mod and hasattr(mod, "extract_ledger") and hasattr(mod, "sync_top_banners_in_html"):
                        try:
                            ledger_obj = mod.extract_ledger(html_text, target_fp)
                            telemetry_obj = get_session_telemetry(target_fp, os.path.basename(target_fp), html_text)
                            html_text = mod.sync_top_banners_in_html(html_text, ledger_obj, telemetry=telemetry_obj)
                        except Exception:
                            pass
                    html_text = re.sub(
                        r'(<a\b[^>]*\bclass=["\'][^"\']*\bbtn\b[^"\']*["\'][^>]*)\bhref=["\']http://127\.0\.0\.1:8788/?["\'](?![^>]*\bonclick=)',
                        r'''\1href="/" onclick="if(location.protocol==='file:'){location.href='http://127.0.0.1:8788/';return false;}"''',
                        html_text,
                        flags=re.IGNORECASE,
                    )
                    raw_html = html_text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw_html)))
                    self.end_headers()
                    self.wfile.write(raw_html)
                    return
                except OSError:
                    pass

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/presence":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                body = {}
            record_presence(str(body.get("scope") or ""))
            return self.send_json({"ok": True})
        if parsed.path not in ("/api/projects/action", "/api/mimir/apply"):
            return self.send_json({"ok": False, "error": "Unknown endpoint"}, status=404)

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            action = body.get("action") or ("mimir_mark_status" if parsed.path == "/api/mimir/apply" else "")

            if action == "mimir_spawn_delta_ticket":
                delta_id = (body.get("deltaId") or "").strip()
                if not delta_id and body.get("deltaIds"):
                    delta_id = str(body["deltaIds"][0]).strip()
                if not delta_id:
                    raise ValueError("Missing deltaId for mimir_spawn_delta_ticket")
                parent_file = (body.get("filePath") or body.get("id") or "").strip()
                mod = _get_odin_state_module()
                if not mod or not hasattr(mod, "spawn_mimir_delta_ticket"):
                    raise RuntimeError("odin-state spawn_mimir_delta_ticket unavailable")
                res = mod.spawn_mimir_delta_ticket(
                    delta_id=delta_id,
                    scan_root=ROOT_DIR,
                    parent_file=parent_file,
                )
                audit_payload = get_mimir_audit_payload()
                res["audit"] = audit_payload
                res["mimirAudit"] = audit_payload
                res["projects"] = discover_projects()
                return self.send_json(res)

            if action == "mimir_mark_status":
                delta_ids = list(body.get("deltaIds") or [])
                if body.get("deltaId") and body["deltaId"] not in delta_ids:
                    delta_ids.append(body["deltaId"])
                new_status = body.get("status") or "applied"
                mod = _get_odin_state_module()
                for proj in discover_projects():
                    fp = resolve_safe_project_path(proj["id"])
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        c = f.read()
                    ledger_m = re.search(
                        r'(<script[^>]*id=["\']odin-iteration-ledger["\'][^>]*>)([\s\S]*?)(</script>)',
                        c,
                        re.IGNORECASE,
                    )
                    if not ledger_m:
                        continue
                    try:
                        ledger = json.loads(ledger_m.group(2).strip())
                        changed = False
                        ts_iso = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                        for it_obj in ledger.get("iterations", []):
                            for d in it_obj.get("harness_delta_proposals", []):
                                if d.get("id") in delta_ids:
                                    d["status"] = new_status
                                    d["updated_at"] = ts_iso
                                    changed = True
                        for d in ledger.get("harness_delta_proposals", []):
                            if d.get("id") in delta_ids:
                                d["status"] = new_status
                                d["updated_at"] = ts_iso
                                changed = True
                        if changed:
                            if mod and hasattr(mod, "save_ledger_into_html"):
                                c = mod.save_ledger_into_html(c, ledger)
                            else:
                                new_json = json.dumps(ledger, indent=2, ensure_ascii=False)
                                c = (
                                    c[: ledger_m.start()]
                                    + ledger_m.group(1)
                                    + "\n"
                                    + new_json
                                    + "\n  "
                                    + ledger_m.group(3)
                                    + c[ledger_m.end() :]
                                )
                            with open(fp, "w", encoding="utf-8") as fw:
                                fw.write(c)
                    except Exception:
                        pass
                audit_payload = get_mimir_audit_payload()
                return self.send_json({
                    "ok": True,
                    "audit": audit_payload,
                    "mimirAudit": audit_payload,
                    "projects": discover_projects(),
                })

            if action == "refresh_telemetry":
                telemetry_data = refresh_telemetry(force=True)
                return self.send_json({
                    "ok": True,
                    "telemetry": telemetry_data,
                    "projects": discover_projects(),
                })

            if action == "stop_port":
                port = int(body.get("port", 0))
                res = stop_dev_port(port)
                return self.send_json({"ok": True, "result": res, "projects": discover_projects()})

            if action == "prune_worktree":
                wt = body.get("worktree", "")
                res = prune_secondary_worktree(wt)
                return self.send_json({"ok": True, "result": res, "projects": discover_projects()})

            if action == "allocate_env":
                target = body.get("target") or next(iter(CONFIG.get("repos") or {}), "")
                if not target:
                    raise RuntimeError("No repos configured. Add a \"repos\" block to your Odin config to auto-allocate.")
                branch = body.get("branch", "feat/odin-task")
                slug = body.get("slug", "")
                script_path = os.path.join(SKILL_DIR, "scripts", "odin-isolate.py")
                code, out, err = run_cmd(
                    ["python3", script_path, "allocate", "--target", target, "--branch", branch, "--slug", slug]
                )
                if code != 0:
                    raise RuntimeError(err or "Failed to allocate environment")
                return self.send_json({"ok": True, "allocation": json.loads(out)})

            if action == "archive_all_done":
                moved = []
                for proj in discover_projects():
                    if not proj["archived"] and proj["overallStatus"] == "done":
                        fp = resolve_safe_project_path(proj["id"])
                        new_fp = archive_or_restore_file(fp, True)
                        moved.append(os.path.relpath(new_fp, ROOT_DIR))
                return self.send_json({"ok": True, "moved": moved, "projects": discover_projects()})

            if action == "toggle_pause":
                mod = _get_odin_state_module()
                if not mod or not hasattr(mod, "get_pause_state"):
                    raise RuntimeError("odin-state module unavailable")
                cur = mod.get_pause_state()
                next_paused = not bool(cur.get("paused")) if "paused" not in body else bool(body["paused"])
                state_info = mod.set_pause_state(next_paused, reason=body.get("reason") or "Toggled via Odin UI")
                return self.send_json({"ok": True, "pauseState": state_info, "projects": discover_projects()})

            if action in ("snapshot_staging", "promote_staging") and not CONFIG["staging"]["available"]:
                raise RuntimeError(
                    "Staging needs Odin running from a git checkout. Clone the Odin repo and set "
                    "staging.repo_dir in your Odin config."
                )

            if action == "snapshot_staging":
                mod = _get_odin_state_module()
                if not mod or not hasattr(mod, "snapshot_staging"):
                    raise RuntimeError("odin-state snapshot_staging unavailable")
                res = mod.snapshot_staging(active_state_rel=body.get("id") or "")
                return self.send_json(res)

            if action == "promote_staging":
                mod = _get_odin_state_module()
                if not mod or not hasattr(mod, "promote_staging"):
                    raise RuntimeError("odin-state promote_staging unavailable")
                res = mod.promote_staging(stop_staging_server=False, restart_staging_server=False, restart_live_server=False)
                res["projects"] = discover_projects()
                self.send_json(res)
                if res.get("ok") and hasattr(mod, "trigger_post_promote_servers"):
                    threading.Timer(
                        0.35,
                        mod.trigger_post_promote_servers,
                        kwargs={
                            "repo_dir": res.get("promotedTo") or getattr(mod, "ODIN_REPO_DIR", CONFIG["staging"]["repo_dir"]),
                            "staging_dir": res.get("promotedFrom") or getattr(mod, "ODIN_STAGING_DIR", CONFIG["staging"]["staging_dir"]),
                            "live_root": getattr(mod, "AI_DEV_ROOT", ROOT_DIR),
                            "stop_staging": False,
                            "restart_staging": True,
                            "restart_live": True,
                        },
                    ).start()
                return

            rel_id = body.get("id", "")
            filepath = resolve_safe_project_path(rel_id)

            if action == "trigger_wakeup":
                mod = _get_odin_state_module()
                if mod and hasattr(mod, "write_wakeup_event"):
                    wf = mod.write_wakeup_event(
                        filepath,
                        decision=body.get("decision") or "manual_wakeup",
                        phase=body.get("phase") or "",
                        notes=body.get("notes") or "",
                        selected_skills=body.get("selectedSkills") or [],
                    )
                    return self.send_json({"ok": True, "wakeupFile": str(wf)})
                return self.send_json({"ok": False, "error": "write_wakeup_event unavailable"}, status=500)

            if action == "read_repo_file":
                raw_wt = str(body.get("worktree") or "").strip()
                file_rel = str(body.get("filePath") or "").strip()
                if not raw_wt or not file_rel:
                    raise ValueError("Missing worktree or filePath")
                root_real = os.path.realpath(ROOT_DIR)
                wt_real = os.path.realpath(raw_wt)
                target_real = os.path.realpath(os.path.join(wt_real, file_rel))
                if not (target_real.startswith(root_real + os.sep) and os.path.isfile(target_real)):
                    raise ValueError(f"Invalid or inaccessible repository file: {file_rel}")
                with open(target_real, "r", encoding="utf-8", errors="replace") as rf:
                    file_text = rf.read()
                return self.send_json({"ok": True, "content": file_text, "absPath": target_real})

            if action == "save_repo_file":
                raw_wt = str(body.get("worktree") or "").strip()
                file_rel = str(body.get("filePath") or "").strip()
                new_text = body.get("content")
                if not raw_wt or not file_rel or not isinstance(new_text, str):
                    raise ValueError("Missing worktree, filePath, or content")
                root_real = os.path.realpath(ROOT_DIR)
                wt_real = os.path.realpath(raw_wt)
                target_real = os.path.realpath(os.path.join(wt_real, file_rel))
                if not (target_real.startswith(root_real + os.sep) and os.path.isfile(target_real)):
                    raise ValueError(f"Invalid or inaccessible repository file: {file_rel}")
                with open(target_real, "w", encoding="utf-8") as wf:
                    wf.write(new_text)

                mod = _get_odin_state_module()
                new_viewer_html = ""
                if mod and hasattr(mod, "generate_contextual_diff_html"):
                    ctx_lines = int(body.get("contextLines") or 12)
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        state_html = f.read()

                    viewer_re = re.compile(
                        r'<div\b[^>]*\bclass=["\'][^"\']*odin-diff-viewer[^"\']*["\'][^>]*>',
                        re.IGNORECASE,
                    )
                    matched_spans = []
                    first_m = None
                    for vm in viewer_re.finditer(state_html):
                        tag_str = vm.group(0)
                        wt_attr = re.search(r'\bdata-worktree=["\']([^"\']+)["\']', tag_str, re.IGNORECASE)
                        if not wt_attr or os.path.realpath(html_lib.unescape(wt_attr.group(1))) == wt_real:
                            if first_m is None:
                                first_m = vm
                            if hasattr(mod, "_find_balanced_tag_span"):
                                sp = mod._find_balanced_tag_span(state_html, vm, "div")
                                if sp:
                                    matched_spans.append((sp, vm))

                    if first_m is None:
                        first_m = viewer_re.search(state_html)
                        if first_m and hasattr(mod, "_find_balanced_tag_span"):
                            sp = mod._find_balanced_tag_span(state_html, first_m, "div")
                            if sp:
                                matched_spans.append((sp, first_m))

                    rationale_val = ""
                    file_notes_val = {}
                    if first_m:
                        tag_str = first_m.group(0)
                        rat_m = re.search(r'\bdata-rationale=["\']([^"\']*)["\']', tag_str, re.IGNORECASE)
                        if rat_m:
                            rationale_val = html_lib.unescape(rat_m.group(1))
                        fn_m = re.search(r'\bdata-file-notes=["\']([^"\']*)["\']', tag_str, re.IGNORECASE)
                        if fn_m:
                            try:
                                parsed_fn = json.loads(html_lib.unescape(fn_m.group(1)))
                                if isinstance(parsed_fn, dict):
                                    file_notes_val = parsed_fn
                            except Exception:
                                pass

                    new_viewer_html = mod.generate_contextual_diff_html(
                        worktree_dir=wt_real,
                        context_lines=ctx_lines,
                        rationale=rationale_val,
                        file_notes=file_notes_val,
                        show_pre_pr_banner=False,
                    )
                    if matched_spans:
                        for (s_idx, e_idx), _vm in reversed(matched_spans):
                            state_html = state_html[:s_idx] + new_viewer_html + state_html[e_idx:]
                        if hasattr(mod, "ensure_diff_styles"):
                            state_html = mod.ensure_diff_styles(state_html)
                        with open(filepath, "w", encoding="utf-8") as sf:
                            sf.write(state_html)

                return self.send_json({
                    "ok": True,
                    "absPath": target_real,
                    "diffViewerHtml": new_viewer_html,
                })

            if action == "save_partner_plan":
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                mod = _get_odin_state_module()
                if not mod:
                    raise RuntimeError("odin-state module unavailable")

                decision = (body.get("decision") or "approve_huginn").strip()
                plan_html = (body.get("planHtml") or "").strip()
                partner_notes = (body.get("partnerNotes") or "").strip()
                selected_skills = [str(s).strip() for s in (body.get("selectedSkills") or []) if str(s).strip()]

                ledger = mod.extract_ledger(content, filepath)
                ledger["partner_notes"] = partner_notes
                ledger["selected_skills"] = selected_skills

                # Build optional Partner Co-Edit Stamp Box
                stamp_parts = []
                if selected_skills:
                    pills_str = " ".join(f"<code>{html_lib.escape(s)}</code>" for s in selected_skills)
                    stamp_parts.append(f"<div><strong>⚡ Attached Skills:</strong> {pills_str}</div>")
                if partner_notes:
                    stamp_parts.append(
                        f"<div style='margin-top:0.35rem;white-space:pre-wrap;'><strong>💬 Partner Decisions &amp; Notes:</strong>\n{html_lib.escape(partner_notes)}</div>"
                    )
                partner_box_html = ""
                if stamp_parts:
                    partner_box_html = (
                        '\n        <div class="odin-inline-partner-box" style="margin-top:1rem;padding:0.85rem 1.1rem;'
                        'border-radius:10px;border:1px solid rgba(56,189,248,0.5);border-left:4px solid #38bdf8;'
                        'background:rgba(15,23,42,0.85);font-size:0.85rem;line-height:1.5;">\n          '
                        + "\n          ".join(stamp_parts)
                        + "\n        </div>"
                    )

                if plan_html:
                    def replace_huginn_body(_old_body: str) -> str:
                        return "\n      <div class=\"phase-body\">\n        " + plan_html + partner_box_html + "\n      </div>\n    "
                    content = mod.mutate_section_body(content, "huginn-plan", replace_huginn_body)
                elif partner_box_html:
                    def append_partner_box(old_body: str) -> str:
                        cleaned = re.sub(
                            r'\s*<div[^>]*class=["\'][^"\']*odin-inline-partner-box[^"\']*["\'][^>]*>[\s\S]*?</div>',
                            "",
                            old_body,
                            flags=re.IGNORECASE,
                        )
                        if re.search(r'<div\b[^>]*class=["\'][^"\']*\bphase-body\b[^"\']*["\']', cleaned, re.IGNORECASE):
                            return re.sub(
                                r"(</div>\s*)$",
                                partner_box_html + "\n      \\1",
                                cleaned,
                                count=1,
                                flags=re.IGNORECASE,
                            )
                        return "\n      <div class=\"phase-body\">\n        " + cleaned.strip() + partner_box_html + "\n      </div>\n    "
                    content = mod.mutate_section_body(content, "huginn-plan", append_partner_box)

                # Mark open-questions-box resolved if approving
                if decision == "approve_huginn":
                    content = re.sub(
                        r'(<div\b(?![^>]*\bdata-resolved=)[^>]*\bclass=["\'][^"\']*open-questions-box[^"\']*["\'])',
                        r'\1 data-resolved="true"',
                        content,
                        flags=re.IGNORECASE,
                    )
                    content = update_section_in_html(content, "huginn-plan", "done", "")
                    thor_info = extract_section_info(content, "thor-execution")
                    if thor_info["status"] in ("pending", "awaiting_human"):
                        content = update_section_in_html(content, "thor-execution", "active", "")
                elif decision == "revise_huginn":
                    content = update_section_in_html(content, "huginn-plan", "awaiting_human", "")

                if isinstance(ledger.get("iterations"), list) and ledger["iterations"]:
                    active_entry = ledger["iterations"][-1]
                    phase_map = active_entry.setdefault("phases", {})
                    h_obj = phase_map.setdefault("huginn", {})
                    h_obj["status"] = "done" if decision == "approve_huginn" else "awaiting_human"
                    if partner_notes:
                        h_obj["summary"] = f"Partner Co-Edited ({decision}): {partner_notes[:120]}"
                    if decision == "approve_huginn":
                        t_obj = phase_map.setdefault("thor", {})
                        if t_obj.get("status") in (None, "", "pending"):
                            t_obj["status"] = "active"

                content = mod.save_ledger_into_html(content, ledger)
                content = mod.update_document_title(content)
                with open(filepath, "w", encoding="utf-8") as fw:
                    fw.write(content)

                wakeup_path = None
                if decision in ("approve_huginn", "revise_huginn"):
                    wf = mod.write_wakeup_event(
                        filepath,
                        decision=decision,
                        phase="huginn-plan",
                        notes=partner_notes,
                        selected_skills=selected_skills,
                    )
                    wakeup_path = str(wf)

                return self.send_json({
                    "ok": True,
                    "decision": decision,
                    "wakeupFile": wakeup_path,
                    "projects": discover_projects(),
                })

            if action == "heimdall_gate_decision":
                mod = _get_odin_state_module()
                if not mod:
                    raise RuntimeError("odin-state module unavailable")
                decision = (body.get("decision") or "approve_pr").strip()
                human_note = (body.get("humanNote") or "").strip()
                selected_skills = [str(s).strip() for s in (body.get("selectedSkills") or []) if str(s).strip()]

                if decision == "reject_pr":
                    # Invoke step-back CLI logic via odin-state.py
                    state_script = os.path.join(SKILL_DIR, "scripts", "odin-state.py")
                    run_cmd(
                        [
                            sys.executable,
                            state_script,
                            "step-back",
                            "--file",
                            filepath,
                            "--source",
                            "heimdall",
                            "--reason",
                            human_note or "Requested code changes at Heimdall Pre-PR Gate",
                        ]
                    )
                else:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    content = update_section_in_html(content, "heimdall-validation", "done", human_note)
                    ledger = mod.extract_ledger(content, filepath)
                    if selected_skills:
                        ledger["selected_skills"] = selected_skills
                    if isinstance(ledger.get("iterations"), list) and ledger["iterations"]:
                        active_entry = ledger["iterations"][-1]
                        h_obj = active_entry.setdefault("phases", {}).setdefault("heimdall", {})
                        h_obj["status"] = "done"
                        if human_note:
                            h_obj["summary"] = human_note
                    content = mod.save_ledger_into_html(content, ledger)
                    content = mod.update_document_title(content)
                    with open(filepath, "w", encoding="utf-8") as fw:
                        fw.write(content)

                wf = mod.write_wakeup_event(
                    filepath,
                    decision=decision,
                    phase="heimdall-validation",
                    notes=human_note,
                    selected_skills=selected_skills,
                )
                return self.send_json({
                    "ok": True,
                    "decision": decision,
                    "wakeupFile": str(wf),
                    "projects": discover_projects(),
                })

            if action == "archive":
                cleanup_notes = []
                if body.get("stopPort"):
                    port_to_stop = int(body["stopPort"])
                    try:
                        r = stop_dev_port(port_to_stop)
                        cleanup_notes.append(r["message"])
                    except Exception as e:
                        cleanup_notes.append(str(e))
                if body.get("pruneWorktree"):
                    try:
                        r = prune_secondary_worktree(body["pruneWorktree"])
                        cleanup_notes.append(r["message"])
                    except Exception as e:
                        cleanup_notes.append(str(e))

                new_fp = archive_or_restore_file(filepath, True)
                return self.send_json({
                    "ok": True,
                    "newId": os.path.relpath(new_fp, ROOT_DIR),
                    "cleanupNotes": cleanup_notes,
                    "projects": discover_projects(),
                })

            if action == "unarchive":
                new_fp = archive_or_restore_file(filepath, False)
                return self.send_json({
                    "ok": True,
                    "newId": os.path.relpath(new_fp, ROOT_DIR),
                    "projects": discover_projects(),
                })

            if action == "update_project":
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                new_title = (body.get("title") or "").strip()
                if new_title:
                    escaped_title = html_lib.escape(new_title)
                    content = re.sub(
                        r"<title[^>]*>[\s\S]*?</title>",
                        f"<title>⚡ Odin Command Center — {escaped_title}</title>",
                        content,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                    content = re.sub(
                        r'(<h1\s+class=["\']brand-title["\'][^>]*>)[\s\S]*?(</h1>)',
                        rf"\g<1>ODIN ALLFATHER — {escaped_title}\g<2>",
                        content,
                        count=1,
                        flags=re.IGNORECASE,
                    )

                if any(k in body for k in ("branch", "worktree", "appPort", "backendPort")):
                    content = set_body_env_attributes(
                        content,
                        (body.get("branch") or "").strip(),
                        (body.get("worktree") or "").strip(),
                        str(body.get("appPort") or "").strip(),
                        str(body.get("backendPort") or "").strip(),
                    )

                phase_updates = body.get("phases") or {}
                human_note = (body.get("humanNote") or "").strip()
                note = (body.get("note") or "").strip()
                note_target = body.get("notePhase") or "thor-execution"
                selected_skills = [str(s).strip() for s in (body.get("selectedSkills") or []) if str(s).strip()]

                # If human-validation is being rejected, invoke step-back CLI so a new iteration is created cleanly!
                if phase_updates.get("human-validation") == "reject" and human_note:
                    state_script = os.path.join(SKILL_DIR, "scripts", "odin-state.py")
                    run_cmd(
                        [
                            sys.executable,
                            state_script,
                            "step-back",
                            "--file",
                            filepath,
                            "--source",
                            "human",
                            "--reason",
                            human_note,
                        ]
                    )
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                else:
                    for sec_id, _ in PHASE_IDS:
                        if sec_id in phase_updates:
                            sec_note = ""
                            if sec_id == "human-validation" and human_note:
                                sec_note = human_note
                            elif note and sec_id == note_target:
                                sec_note = note
                            content = update_section_in_html(content, sec_id, phase_updates[sec_id], sec_note)
                    if human_note and "human-validation" not in phase_updates:
                        current_info = extract_section_info(content, "human-validation")
                        content = update_section_in_html(content, "human-validation", current_info["status"], human_note)
                    if note and note_target not in phase_updates:
                        current_info = extract_section_info(content, note_target)
                        content = update_section_in_html(content, note_target, current_info["status"], note)

                # Also synchronize the embedded #odin-iteration-ledger, top banners, and <title>
                try:
                    mod = _get_odin_state_module()
                    ledger = mod.extract_ledger(content, filepath) if mod else None
                    if ledger and isinstance(ledger.get("iterations"), list) and ledger["iterations"]:
                        if selected_skills:
                            ledger["selected_skills"] = selected_skills
                        active_entry = ledger["iterations"][-1]
                        phase_map = active_entry.setdefault("phases", {})
                        short_map = {
                            "huginn-plan": "huginn",
                            "thor-execution": "thor",
                            "heimdall-validation": "heimdall",
                            "hlidskjalf-judge": "hlidskjalf",
                            "human-validation": "human",
                        }
                        for sec_id, short_key in short_map.items():
                            if sec_id in phase_updates and phase_updates.get("human-validation") != "reject":
                                p_obj = phase_map.setdefault(short_key, {})
                                p_obj["status"] = phase_updates[sec_id]
                                if sec_id == "human-validation" and human_note:
                                    p_obj["summary"] = human_note
                                elif note and sec_id == note_target:
                                    p_obj["summary"] = note
                        content = mod.save_ledger_into_html(content, ledger)
                        if hasattr(mod, "sync_parent_mimir_delta_on_completion") and (
                            phase_updates.get("hlidskjalf-judge") == "done"
                            or phase_updates.get("human-validation") in ("done", "awaiting_human")
                        ):
                            mod.sync_parent_mimir_delta_on_completion(filepath, content, ledger)
                    if mod and hasattr(mod, "update_document_title"):
                        content = mod.update_document_title(content)
                except Exception:
                    pass

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)

                # Write reactive wakeup event whenever phases or human notes are updated
                try:
                    mod = _get_odin_state_module()
                    if mod and hasattr(mod, "write_wakeup_event") and (phase_updates or human_note or note):
                        decision_label = "update_project"
                        if phase_updates.get("human-validation") == "done":
                            decision_label = "approve_signoff"
                        elif phase_updates.get("human-validation") == "reject":
                            decision_label = "reject_signoff"
                        mod.write_wakeup_event(
                            filepath,
                            decision=decision_label,
                            phase=",".join(phase_updates.keys()),
                            notes=human_note or note,
                            selected_skills=selected_skills,
                        )
                except Exception:
                    pass

                return self.send_json({"ok": True, "projects": discover_projects()})

            return self.send_json({"ok": False, "error": f"Unsupported action: {action}"}, status=400)
        except Exception as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=500)

    def translate_path(self, path):
        translated = super().translate_path(path)
        if os.path.exists(translated):
            return translated

        parsed = urllib.parse.unquote(urllib.parse.urlparse(path).path)
        filename = os.path.basename(parsed)
        if filename.endswith(".html") and "odin-states" in parsed:
            patterns = [
                os.path.join(ROOT_DIR, "odin-states", filename),
                os.path.join(ROOT_DIR, "odin-states", "archive", filename),
                os.path.join(ROOT_DIR, "*", "odin-states", filename),
                os.path.join(ROOT_DIR, "*", "odin-states", "archive", filename),
                os.path.join(ROOT_DIR, "*", "*", "odin-states", filename),
                os.path.join(ROOT_DIR, "*", "*", "odin-states", "archive", filename),
                os.path.join(ROOT_DIR, "*", "*", "*", "odin-states", filename),
                os.path.join(ROOT_DIR, "*", "*", "*", "odin-states", "archive", filename),
            ]
            matches = []
            for pattern in patterns:
                for candidate in glob.glob(pattern):
                    if not ROOT_DIR.rstrip("/").endswith("odin-staging") and "/odin-staging/" in candidate.replace("\\", "/"):
                        continue
                    matches.append(candidate)
            if matches:
                matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return matches[0]

        return translated

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, format, *args):
        pass


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None):
    args = parse_server_args(argv)
    configure_server(port=args.port, root_dir=args.root_dir)
    listeners = get_listening_ports()
    existing = listeners.get(PORT)
    if (
        existing
        and existing["pid"] not in (os.getpid(), os.getppid())
        and "python" in existing["command"].lower()
    ):
        try:
            os.kill(existing["pid"], signal.SIGTERM)
            time.sleep(0.3)
        except OSError:
            pass
    if _telemetry_config():
        threading.Thread(target=refresh_telemetry, kwargs={"force": True}, daemon=True).start()
    try:
        with ReusableTCPServer(("127.0.0.1", PORT), OdinHTTPRequestHandler) as httpd:
            print(f"Odin State Server listening on http://127.0.0.1:{PORT} (root: {ROOT_DIR})", flush=True)
            httpd.serve_forever()
    except OSError as exc:
        print(f"Odin State Server port {PORT} already in use ({exc})", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
