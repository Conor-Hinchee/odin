"""Friction scan for the Hlidskjalf retrospective.

The retrospective used to see only the state file, and most harness failures never reach it: a dashboard
decision the agent didn't act on, project files read before the state file existed, a required PR replaced
by an assumption. This module reads the state file and, when it can find one, the session transcript, and
returns the failures it can prove, each with evidence.

Findings (stable ids, used by the Phase 4 gate):
- missing-pr: Heimdall is done, but no PR is linked.
- gate-ignored: a dashboard decision woke the session, and the turn ended without acting on it
  (ground rule 8). The human then had to answer the same gate again in chat.
- gate-unwatched: a phase was set to awaiting_human, and the turn ended without arming wait-for-human
  (ground rule 7), so a dashboard decision had nowhere to go and the human had to answer in chat.
- state-file-late: project files were read before the state file was created (ground rule 1).

Transcripts are Claude Code session JSONL (~/.claude/projects/<project>/<session-id>.jsonl) or
`claude -p --output-format stream-json` output. Both carry the same message shapes. Stdlib only, and
nothing leaves the machine.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
from pathlib import Path

WAKEUP_MARKER = "ODIN REACTIVE PARTNER WAKEUP EVENT"
# Agents often hold the script path in a variable (`O=.../odin-state.py; python3 $O update-phase`),
# so match the subcommand in any command that mentions odin-state, not right after the script name.
ACT = re.compile(r"\b(?:update-phase|step-back)\b")
RETRO = re.compile(r"\bupdate-phase\b[^\n]*--phase[ =]['\"]?(?:hlidskjalf|hlidskjalf-judge)\b")
STATE_FILE = re.compile(r"odin-states/[\w.$\{\}\"'-]+\.html")  # also odin-states/$S.html
WRITE_OP = re.compile(r"(?<![0-9&])>(?!&)|\btee\b|\bcp\b|\bsed\s+-i\b|\bpython3?\b")
# Commands that read file contents. Listing names (ls, tree) is allowed before the state file, like git status.
READ_CMDS = {"cat", "head", "tail", "less", "more", "wc", "grep", "rg", "sed", "awk", "find", "nl", "file", "diff"}

FINDING_TEXT = {
    "missing-pr": "Heimdall was closed without a linked PR",
    "gate-ignored": "a dashboard decision woke the session, and the turn ended without acting on it",
    "gate-unwatched": "a phase paused at awaiting_human, and the turn ended without arming wait-for-human",
    "state-file-late": "project files were read before the state file existed",
}


# --------------------------------------------------------------------------- transcript

def find_transcript(session_id: str, projects_dir: str | None = None) -> Path | None:
    """The session's JSONL under ~/.claude/projects, if Claude Code kept one."""
    sid = (session_id or "").strip()
    if not sid or not re.fullmatch(r"[\w-]+", sid):
        return None
    base = projects_dir or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    hits = glob.glob(os.path.join(base, "*", f"{sid}.jsonl"))
    return Path(max(hits, key=os.path.getmtime)) if hits else None


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _blocks(ev: dict) -> list:
    content = (ev.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return "\n".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
    return str(c or "")


def steps(events: list[dict]) -> list[tuple]:
    """Flatten a transcript into ordered steps: ("tool", name, input), ("result", text), ("turn_end",)."""
    out: list[tuple] = []
    for ev in events:
        t = ev.get("type")
        if t == "result":  # stream-json: one per turn end
            out.append(("turn_end",))
            continue
        if t == "assistant":
            for b in _blocks(ev):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    out.append(("tool", b.get("name") or "", b.get("input") or {}))
            # Session JSONL has no result events: an assistant message that ends the turn marks it.
            if (ev.get("message") or {}).get("stop_reason") == "end_turn" and "uuid" in ev:
                out.append(("turn_end",))
        elif t == "user":
            for b in _blocks(ev):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append(("result", _result_text(b)))
    return out


def _command(inp: dict) -> str:
    return str(inp.get("command") or "")


def cut_at_retro(seq: list[tuple]) -> list[tuple]:
    """Only what happened before the retrospective started (what it can be expected to look back on)."""
    for i, s in enumerate(seq):
        if s[0] == "tool" and "odin-state" in _command(s[2]) and RETRO.search(_command(s[2])):
            return seq[:i]
    return seq


def gate_ignored(seq: list[tuple]) -> list[dict]:
    found = []
    for i, s in enumerate(seq):
        if s[0] != "result" or WAKEUP_MARKER not in s[1]:
            continue
        m = re.search(r'"decision"\s*:\s*"([^"]+)"', s[1])
        decision = m.group(1) if m else "unknown"
        for later in seq[i + 1:]:
            if later[0] == "tool" and "odin-state" in _command(later[2]) and ACT.search(_command(later[2])):
                break
            if later[0] == "turn_end":
                found.append({"id": "gate-ignored", "evidence": f"{decision} was delivered, and the turn ended "
                              "before any update-phase or step-back"})
                break
    return found


AWAIT = re.compile(r"--status[ =]['\"]?awaiting_human\b")


def gate_unwatched(seq: list[tuple]) -> list[dict]:
    for i, s in enumerate(seq):
        cmd = _command(s[2]) if s[0] == "tool" else ""
        if not ("odin-state" in cmd and AWAIT.search(cmd)) or "wait-for-human" in cmd:
            continue
        for later in seq[i + 1:]:
            if later[0] == "tool" and "wait-for-human" in _command(later[2]):
                break
            if later[0] == "turn_end":
                phase = re.search(r"--phase[ =]['\"]?([\w-]+)", cmd)
                return [{"id": "gate-unwatched", "evidence": f"{phase.group(1) if phase else 'a phase'} was set to "
                         "awaiting_human, and the turn ended with no wait-for-human"}]
    return []


def _project_paths(seq_step: tuple, roots: list[Path], skill_dir: Path) -> list[str]:
    """Paths inside the project that a read-only step touches. Odin's own files and odin-states/ don't count."""
    _, name, inp = seq_step
    cands: list[str] = []
    if name in ("Read", "Grep", "Glob", "view_file", "grep_search"):
        cands = [str(inp.get(k)) for k in ("file_path", "path", "AbsolutePath", "SearchPath") if inp.get(k)]
    elif name in ("Bash", "run_command"):
        cmd = _command(inp) or str(inp.get("CommandLine") or "")
        for seg in re.split(r"[;&|\n]+", cmd):
            try:
                toks = shlex.split(seg)
            except ValueError:
                toks = seg.split()
            if toks and os.path.basename(toks[0]) in READ_CMDS:
                cands += [t for t in toks[1:] if not t.startswith("-")]
    hits = []
    for c in cands:
        c = c.rstrip("/") or c
        for root in roots:
            pattern = c if os.path.isabs(c) else str(root / c)
            # A glob (`shop/*.py`) reads whatever it matches.
            matches = glob.glob(pattern) if any(ch in c for ch in "*?[") else [pattern]
            if not matches:
                continue
            try:
                rp = Path(matches[0]).resolve()
            except OSError:
                continue
            if not rp.exists() or root.resolve() not in (rp, *rp.parents) or rp == root.resolve():
                continue
            if "odin-states" in rp.parts or ".git" in rp.parts or skill_dir in (rp, *rp.parents):
                continue
            hits.append(c)
            break
    return hits


def _creates_state_file(s: tuple) -> bool:
    if s[0] != "tool":
        return False
    if s[1] in ("Write", "write_to_file"):
        target = str(s[2].get("file_path") or s[2].get("TargetFile") or "")
        return bool(STATE_FILE.search(target))
    if s[1] in ("Bash", "run_command"):
        cmd = _command(s[2]) or str(s[2].get("CommandLine") or "")
        # odin-state.py calls (update-phase, log, set-env) need the file to exist, so they never create it.
        rest = "\n".join(seg for seg in re.split(r"&&|\|\||;|\n", cmd) if not re.search(r"odin-state\.py", seg))
        return bool(STATE_FILE.search(rest)) and bool(WRITE_OP.search(re.sub(r">\s*/dev/null", "", rest)))
    return False


def creation_index(seq: list[tuple]) -> int | None:
    return next((i for i, s in enumerate(seq) if _creates_state_file(s)), None)


def scope_to_task(seq: list[tuple], name: str = "") -> list[tuple]:
    """One session can run several Odin tasks. Keep only this one: from the turn of the first tool call that
    names its state file (agents usually set S=odin-states/<slug>.html before writing it)."""
    if not name:
        return seq
    first = next((i for i, s in enumerate(seq) if s[0] == "tool" and name in json.dumps(s[2])), None)
    if first is None:
        return seq
    start = max((i + 1 for i, s in enumerate(seq[:first]) if s[0] == "turn_end"), default=0)
    return seq[start:]


def state_file_late(seq: list[tuple], roots: list[Path], skill_dir: Path) -> list[dict]:
    created = creation_index(seq)
    if created is None:
        return []
    read = sorted({p for s in seq[:created] if s[0] == "tool" for p in _project_paths(s, roots, skill_dir)})
    if not read:
        return []
    return [{"id": "state-file-late", "evidence": "read before the state file existed: " + ", ".join(read[:6])}]


# --------------------------------------------------------------------------- state file

def missing_pr(content: str) -> list[dict]:
    heimdall = re.search(r'<section\b[^>]*\bid=["\']heimdall-validation["\'][^>]*>', content, re.I)
    if not heimdall or not re.search(r'data-status=["\']done["\']', heimdall.group(0)):
        return []
    if re.search(r'\bdata-pr-url=["\']https?://', content):
        return []
    return [{"id": "missing-pr", "evidence": "Heimdall is done and no data-pr-url is stamped"}]


def scan(content: str, state_path: Path | None = None, transcript: Path | None = None,
         before_retro: bool = True) -> list[dict]:
    """Every finding, with its id, text and evidence. Each id appears at most once."""
    findings = missing_pr(content)
    if transcript and transcript.exists():
        name = state_path.name if state_path is not None else ""
        seq = scope_to_task(steps(read_events(transcript)), name)
        if before_retro:
            seq = cut_at_retro(seq)
        roots = []
        m = re.search(r'<body\b[^>]*\bdata-worktree=["\']([^"\']+)["\']', content)
        if m and os.path.isdir(m.group(1)):
            roots.append(Path(m.group(1)))
        if state_path is not None:
            roots.append(state_path.resolve().parent.parent)
        skill_dir = Path(__file__).resolve().parent.parent
        findings += gate_ignored(seq)[:1] + gate_unwatched(seq) + state_file_late(seq, roots, skill_dir)
    seen, out = set(), []
    for f in findings:
        if f["id"] not in seen:
            seen.add(f["id"])
            out.append({**f, "text": FINDING_TEXT[f["id"]]})
    return out


def addressed(finding_id: str, deltas: list[dict], waivers: list[str]) -> bool:
    """A finding is addressed when a delta or a waiver names its id."""
    texts = [json.dumps(d) for d in deltas] + list(waivers)
    return any(finding_id in t for t in texts)
