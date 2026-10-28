#!/usr/bin/env python3
"""Read Claude Code session transcripts for Odin.

Claude Code writes one JSONL file per session at
~/.claude/projects/<project-slug>/<session_id>.jsonl (override the base with
$CLAUDE_CONFIG_DIR). The format is not a documented API, so every reader here
is defensive and degrades to "no data" rather than raising.

Line shapes used:
  user prompt   {"type":"user","message":{"role":"user","content":"<str>" | [{"type":"text",...}]}, "timestamp":...}
  tool result   {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":...}]}, ...}
  assistant     {"type":"assistant","message":{"model":...,"content":[{"type":"text"|"tool_use",...}]}, ...}
"""

import datetime
import glob
import json
import os
import re
import time

QUESTION_TOOL = "AskUserQuestion"
_OPEN_Q_HEADING = re.compile(
    r"(?:Open\s+Questions|Before\s+Phase\s+\d+|Please\s+let\s+me\s+know\s+your\s+preference)",
    re.IGNORECASE,
)
_PATH_CACHE: dict = {}
_DISCOVERY_CACHE = {"ts": 0.0, "map": {}}


def projects_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects")


def find_transcript(session_id: str) -> str:
    """Return the transcript path for a session id, or ""."""
    if not session_id or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", session_id):
        return ""
    cached = _PATH_CACHE.get(session_id)
    if cached and os.path.isfile(cached):
        return cached
    matches = glob.glob(os.path.join(projects_dir(), "*", f"{session_id}.jsonl"))
    path = max(matches, key=os.path.getmtime) if matches else ""
    if path:
        _PATH_CACHE[session_id] = path
    return path


def discover_session_for_file(filename: str, filepath: str = "", max_files: int = 40) -> str:
    """Find the most recent session whose transcript mentions this Odin state file."""
    now = time.time()
    if (now - _DISCOVERY_CACHE["ts"]) < 6.0 and filename in _DISCOVERY_CACHE["map"]:
        return _DISCOVERY_CACHE["map"][filename]
    candidates = glob.glob(os.path.join(projects_dir(), "*", "*.jsonl"))
    candidates.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    best = ""
    for path in candidates[:max_files]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        if filename in raw or (filepath and filepath in raw):
            best = os.path.basename(path)[: -len(".jsonl")]
            break
    _DISCOVERY_CACHE["ts"] = now
    if best:
        _DISCOVERY_CACHE["map"][filename] = best
    return best


def read_lines(path: str, tail_bytes: int | None = None) -> list:
    """Parse JSONL objects from a transcript (optionally just its last tail_bytes)."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as fh:
            if tail_bytes:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if tail_bytes and lines and not lines[0].startswith("{"):
        lines = lines[1:]  # partial first line
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _ts(obj: dict) -> float:
    raw = obj.get("timestamp") or ""
    if not raw:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _blocks(obj: dict) -> list:
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def is_human_prompt(obj: dict) -> bool:
    """True for a prompt the person typed (not a tool result, hook, or meta line)."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isSidechain"):
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") and origin.get("kind") != "human":
        return False
    blocks = _blocks(obj)
    return bool(blocks) and all(b.get("type") == "text" for b in blocks)


def prompt_text(obj: dict) -> str:
    return "\n".join(b.get("text", "") for b in _blocks(obj) if b.get("type") == "text").strip()


def user_prompts(path: str) -> list:
    """All human prompts in a transcript, oldest first."""
    prompts = []
    for obj in read_lines(path):
        if is_human_prompt(obj):
            text = prompt_text(obj)
            if text:
                prompts.append(text)
    return prompts


def last_model(path: str) -> str:
    for obj in reversed(read_lines(path, tail_bytes=262144)):
        if obj.get("type") == "assistant":
            model = (obj.get("message") or {}).get("model")
            if model and model != "<synthetic>":
                return str(model)
    return ""


def last_activity(path: str) -> dict:
    """Most recent tool call and the transcript mtime."""
    result = {"lastToolName": "", "lastActivityTs": 0.0}
    if not path or not os.path.isfile(path):
        return result
    try:
        result["lastActivityTs"] = os.path.getmtime(path)
    except OSError:
        pass
    for obj in reversed(read_lines(path, tail_bytes=32768)):
        if obj.get("type") != "assistant":
            continue
        for block in reversed(_blocks(obj)):
            if block.get("type") == "tool_use" and block.get("name"):
                result["lastToolName"] = str(block["name"])
                return result
    return result


def _short(text: str, limit: int = 165) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def analyze_questions(path: str) -> dict:
    """Detect whether the session is paused on unanswered questions.

    A question is an AskUserQuestion tool call without a matching tool_result,
    or an assistant text turn with an "Open Questions" heading and a "?" that no
    later prompt or tool call has moved past.
    """
    result = {
        "awaitingHuman": False,
        "openQuestions": [],
        "questionsResolved": False,
        "lastResolutionTs": 0.0,
        "lastQuestionTs": 0.0,
    }
    pending: dict = {}
    text_question = None
    resolved = False
    for obj in read_lines(path, tail_bytes=131072):
        kind = obj.get("type")
        ts = _ts(obj)
        if kind == "user":
            if is_human_prompt(obj):
                if pending or text_question is not None:
                    resolved = True
                    result["lastResolutionTs"] = max(result["lastResolutionTs"], ts)
                pending.clear()
                text_question = None
                continue
            for block in _blocks(obj):
                if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    pending.pop(block["tool_use_id"], None)
                    resolved = True
                    result["lastResolutionTs"] = max(result["lastResolutionTs"], ts)
        elif kind == "assistant":
            for block in _blocks(obj):
                btype = block.get("type")
                if btype == "tool_use" and block.get("name") == QUESTION_TOOL:
                    qs = (block.get("input") or {}).get("questions") or []
                    pending[block.get("id", "")] = [
                        _short(q["question"]) for q in qs if isinstance(q, dict) and q.get("question")
                    ]
                    result["lastQuestionTs"] = ts
                elif btype == "tool_use":
                    if text_question is not None:
                        resolved = True
                        result["lastResolutionTs"] = max(result["lastResolutionTs"], ts)
                    text_question = None
                elif btype == "text":
                    text = block.get("text") or ""
                    if _OPEN_Q_HEADING.search(text) and "?" in text:
                        text_question = text
                        result["lastQuestionTs"] = ts

    if pending:
        result["awaitingHuman"] = True
        result["openQuestions"] = [q for qs in pending.values() for q in qs][:4]
    elif text_question is not None:
        result["awaitingHuman"] = True
        bullets = re.findall(r"(?:^|\n)\s*\d+\.\s+\*\*([^\n]+)", text_question) or re.findall(
            r"(?:^|\n)\s*[-*]\s+\*\*([^\n]+)", text_question
        )
        result["openQuestions"] = [_short(re.sub(r"\*\*|`", "", b)) for b in bullets[:4] if b.strip()]
    result["questionsResolved"] = resolved and not result["awaitingHuman"]
    return result
