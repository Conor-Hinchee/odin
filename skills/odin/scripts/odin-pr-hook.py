#!/usr/bin/env python3
r"""Odin PreToolUse hook (Claude Code & Antigravity): PR label & session telemetry for `gh pr create` / `gh pr edit`.

Does nothing unless your Odin config has a "pr" block, e.g.:

    "pr": {"label": "ai-generated", "heading": "## 🤖 Session Telemetry",
           "require_prompt_summary": true, "sanitize_hash_refs": true,
           "include_verbatim_prompts": false}

When enabled it:
1. Adds `--label "<label>"` to `gh pr create` (if a label is configured).
2. Denies `gh pr create` whose `--body` lacks a `### Prompt Summary` section (require_prompt_summary),
   unless an active Odin state file has a Heimdall-curated `approved_prompt_summary`,
   or when the body says nothing about the change beyond it (require_change_summary). The eval harness
   found Odin PR bodies that were only the Prompt Summary, so a GitHub reviewer learned nothing about the diff.
3. Syncs `### Prompt Summary` with Heimdall's `approved_prompt_summary` when available, and appends a
   telemetry section with the Session ID, model, and (only when `include_verbatim_prompts` is true)
   a collapsible `<details>` block of the session's verbatim prompts read from the transcript.
4. Rewrites non-issue `Rule #12`-style references to `Rule 12` in PR titles/bodies and
   `git commit -m` messages so GitHub doesn't autolink unrelated issues (sanitize_hash_refs).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import odin_transcript  # noqa: E402
from odin_config import load_config  # noqa: E402

PR_CONFIG: dict = {}
LABEL = ""
TELEMETRY_HEADING = "## 🤖 Session Telemetry"
REQUIRE_PROMPT_SUMMARY = True
REQUIRE_CHANGE_SUMMARY = True
SANITIZE_HASH_REFS = True
INCLUDE_VERBATIM_PROMPTS = False
VERBATIM_SUMMARY_TAG = "<summary>💬 Verbatim Session Prompts"
VERBATIM_DETAILS_RE = re.compile(
    r"<details>\s*<summary>💬 Verbatim Session Prompts[\s\S]*?</details>",
    re.IGNORECASE,
)


def configure(pr_config: dict | None) -> None:
    global PR_CONFIG, LABEL, TELEMETRY_HEADING, REQUIRE_PROMPT_SUMMARY, REQUIRE_CHANGE_SUMMARY, SANITIZE_HASH_REFS, INCLUDE_VERBATIM_PROMPTS
    PR_CONFIG = pr_config if isinstance(pr_config, dict) else {}
    LABEL = str(PR_CONFIG.get("label") or "")
    TELEMETRY_HEADING = str(PR_CONFIG.get("heading") or "## 🤖 Session Telemetry")
    REQUIRE_PROMPT_SUMMARY = bool(PR_CONFIG.get("require_prompt_summary", True))
    REQUIRE_CHANGE_SUMMARY = bool(PR_CONFIG.get("require_change_summary", True))
    SANITIZE_HASH_REFS = bool(PR_CONFIG.get("sanitize_hash_refs", True))
    INCLUDE_VERBATIM_PROMPTS = bool(PR_CONFIG.get("include_verbatim_prompts", False))


def is_antigravity_payload(payload: dict) -> bool:
    return "toolCall" in payload or "conversationId" in payload


def detect_agent_model(transcript_path: str | None, payload: dict | None = None) -> str:
    """Model name from the latest assistant message in the session transcript."""
    path = transcript_path or ""
    model = odin_transcript.last_model(path)
    is_agy = bool(
        (payload and is_antigravity_payload(payload))
        or "/antigravity/" in path
        or path.endswith("/transcript.jsonl")
    )
    if is_agy:
        if not model and payload:
            m_name = str(payload.get("modelName") or "").strip()
            if m_name and m_name.lower() != "auto":
                model = m_name
        return f"Antigravity ({model})" if model else "Antigravity"
    return f"Claude Code ({model})" if model else "Claude Code"


def clean_user_prompt(raw: str) -> str:
    """Strip harness-injected tags (system reminders, slash-command wrappers) from a prompt."""
    if not raw:
        return ""
    cleaned = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?command-(?:name|message|args)>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<local-command-stdout>[\s\S]*?</local-command-stdout>", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def extract_verbatim_prompts(
    transcript_path: str | None, session_id: str | None
) -> list[dict[str, str]]:
    """Read the person's prompts from the session transcript."""
    path = transcript_path or ""
    if not path or not os.path.isfile(os.path.expanduser(path)):
        path = odin_transcript.find_transcript(session_id or "")
    if not path:
        return []
    prompts: list[dict[str, str]] = []
    for obj in odin_transcript.read_lines(os.path.expanduser(path)):
        if not odin_transcript.is_human_prompt(obj):
            continue
        text = clean_user_prompt(odin_transcript.prompt_text(obj))
        if text:
            ts = str(obj.get("timestamp") or obj.get("created_at") or "")
            prompts.append({"created_at": ts, "text": text})
    return prompts


def build_verbatim_details_block(prompts: list[dict[str, str]]) -> str:
    """Render collapsible <details> block for verbatim session prompts."""
    if not prompts:
        return ""
    lines = [
        "<details>",
        f"<summary>💬 Verbatim Session Prompts ({len(prompts)})</summary>",
        "",
    ]
    for idx, p in enumerate(prompts, start=1):
        ts = f" (`{p['created_at']}`)" if p.get("created_at") else ""
        text = p["text"].replace("```", "'''")
        lines.append(f"{idx}. **Prompt {idx}**{ts}:")
        lines.append("   ```text")
        for subline in text.splitlines():
            lines.append(f"   {subline}")
        lines.append("   ```")
        lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


NON_ISSUE_HASH_RE = re.compile(
    r"\b(Rules?|Steps?|Items?|Phases?|Points?|Bullets?|Sections?|Lines?|"
    r"Iterations?|Questions?|Scenarios?|Options?|Parts?|Checks?|Gates?|"
    r"Stages?|Tasks?|Requirements?|Guidelines?|Principles?)\s+#(\d+)\b",
    re.IGNORECASE,
)

HEREDOC_SUBSHELL_RE = re.compile(
    r"^\s*\$\(\s*cat\s+<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?\s*\r?\n([\s\S]*?)\r?\n[ \t]*\1\s*\)\s*$"
)


def unwrap_heredoc_subshell(text: str) -> tuple[str, bool]:
    """Unwrap `$(cat <<'EOF'\\n...\\nEOF\\n)` before shlex.quote re-quotes the token in single quotes."""
    if not text or "<<" not in text:
        return text, False
    m = HEREDOC_SUBSHELL_RE.match(text)
    if m:
        return m.group(2), True
    return text, False


def sanitize_non_issue_hash_refs(text: str) -> tuple[str, bool]:
    """Strip '#' from non-issue ordinal nouns (e.g. 'Rule #12' -> 'Rule 12').

    In GitHub Flavored Markdown (PR bodies, titles, and commit messages), bare
    '#<digits>' tokens auto-link and cross-reference repository Issues/PRs
    (often linking to ancient PRs from years ago).
    """
    if not SANITIZE_HASH_REFS or not text or "#" not in text:
        return text, False
    sanitized, count = NON_ISSUE_HASH_RE.subn(r"\1 \2", text)
    return sanitized, count > 0


def has_prompt_summary(body: str) -> bool:
    """Check whether the PR body includes a Prompt Summary section/label."""
    return bool(
        re.search(r"(?i)(###?\s*Prompt\s+Summary|\*\*Prompt\s+Summary\*\*)", body)
    )


def has_change_summary(body: str) -> bool:
    """Does the body say anything about the change besides the Prompt Summary, telemetry and attribution?"""
    text = re.sub(r"(?is)<details\b.*?</details>", "", body or "")
    # Drop the Prompt Summary and telemetry sections, up to the next heading of any level.
    text = re.sub(r"(?ims)^#{1,4}\s*(Prompt\s+Summary|🤖\s*Session\s+Telemetry|Session\s+Telemetry)\b.*?(?=^#{1,4}\s|\Z)",
                  "", text)
    text = re.sub(r"(?im)^\s*\*\*Prompt\s+Summary\*\*.*$", "", text)
    text = re.sub(r"(?im)^.*(Generated with \[?Claude Code|Co-Authored-By:).*$", "", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s.*$", "", text)  # headings alone don't describe anything
    return bool(re.search(r"[A-Za-z0-9]", text))


def _extract_bullets_from_state_html(content: str) -> tuple[str, str, list[str]]:
    """Return (data_session_id, ledger_session_id, approved_prompt_summary) from an Odin state HTML string."""
    m_attr = re.search(r'data-session-id=["\']([^"\']*)["\']', content, re.IGNORECASE)
    data_sid = m_attr.group(1).strip() if m_attr else ""

    m_ledger = re.search(
        r'<script[^>]*id=["\']odin-iteration-ledger["\'][^>]*>([\s\S]*?)</script>',
        content,
        re.IGNORECASE,
    )
    if not m_ledger:
        return data_sid, "", []
    try:
        ledger = json.loads(m_ledger.group(1).strip())
    except (ValueError, TypeError):
        return data_sid, "", []
    if not isinstance(ledger, dict):
        return data_sid, "", []

    ledger_sid = str(ledger.get("session_id") or "").strip()
    raw_items = ledger.get("approved_prompt_summary")
    if not (isinstance(raw_items, list) and raw_items):
        iterations = ledger.get("iterations")
        if isinstance(iterations, list) and iterations:
            for it_obj in reversed(iterations):
                if not isinstance(it_obj, dict):
                    continue
                cand = it_obj.get("approved_prompt_summary")
                if not (isinstance(cand, list) and cand):
                    phases = it_obj.get("phases")
                    if isinstance(phases, dict):
                        heimdall = phases.get("heimdall")
                        if isinstance(heimdall, dict):
                            cand = heimdall.get("approved_prompt_summary")
                            if not (isinstance(cand, list) and cand):
                                h_sum = str(heimdall.get("summary") or "")
                                m_ps = re.search(r"(?i)###?\s*Prompt\s+Summary\s*\n((?:\s*(?:\d+\.|[-*])\s+.+(?:\n|$))+)", h_sum)
                                if m_ps:
                                    cand = [
                                        re.sub(r"^\s*(?:\d+\.|[-*])\s+", "", ln).strip()
                                        for ln in m_ps.group(1).splitlines()
                                        if ln.strip()
                                    ]
                if isinstance(cand, list) and cand:
                    raw_items = cand
                    break

    if not isinstance(raw_items, list):
        return data_sid, ledger_sid, []
    bullets = [str(s).strip() for s in raw_items if str(s).strip()]
    return data_sid, ledger_sid, bullets


def _state_html_files_under(base_dir: str) -> list[str]:
    patterns = [
        os.path.join(base_dir, "odin-states", "*.html"),
        os.path.join(base_dir, "*", "odin-states", "*.html"),
        os.path.join(base_dir, "*", "*", "odin-states", "*.html"),
    ]
    seen: set[str] = set()
    files: list[str] = []
    for pat in patterns:
        for path in glob.glob(pat):
            if os.path.basename(path).lower() == "index.html":
                continue
            norm_parts = path.replace("\\", "/").split("/")
            if "archive" in norm_parts or "odin-staging" in norm_parts:
                continue
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                files.append(path)
    return files


def load_approved_prompt_summary(
    session_id: str | None, cwd: str | None = None
) -> list[str]:
    """Load Heimdall-curated approved_prompt_summary bullets from an active Odin state file."""
    target_sid = (session_id or "").strip()
    if target_sid == "unknown-session":
        target_sid = ""

    cwd_files: list[str] = []
    if cwd:
        cwd_abs = os.path.abspath(os.path.expanduser(cwd))
        cwd_files = _state_html_files_under(cwd_abs)
    else:
        cwd_abs = ""

    cfg_root = ""
    try:
        cfg_root = str(load_config(cwd_abs or None).get("root") or "")
        if cfg_root:
            cfg_root = os.path.abspath(os.path.expanduser(cfg_root))
    except Exception:
        cfg_root = ""

    if cwd_files:
        candidate_files = cwd_files
        allow_single_fallback = True
    else:
        candidate_files = _state_html_files_under(cfg_root) if cfg_root else []
        allow_single_fallback = bool(
            os.environ.get("ODIN_ROOT")
            or (
                cwd_abs
                and cfg_root
                and (
                    cwd_abs == cfg_root
                    or cwd_abs.startswith(cfg_root.rstrip(os.sep) + os.sep)
                )
            )
        )

    sid_matches: list[tuple[float, list[str]]] = []
    with_summary: list[tuple[float, list[str]]] = []

    for path in candidate_files:
        try:
            mtime = os.path.getmtime(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        data_sid, ledger_sid, bullets = _extract_bullets_from_state_html(content)
        if not bullets:
            continue
        with_summary.append((mtime, bullets))
        if target_sid and target_sid in (data_sid, ledger_sid):
            sid_matches.append((mtime, bullets))

    if sid_matches:
        sid_matches.sort(key=lambda x: x[0], reverse=True)
        return sid_matches[0][1]

    if allow_single_fallback and len(with_summary) == 1:
        return with_summary[0][1]

    return []


PROMPT_SUMMARY_SECTION_RE = re.compile(
    r"(?im)^#{2,3}[ \t]*Prompt[ \t]+Summary\b[^\n]*(?:\n[\s\S]*?)?(?=\n#{1,6}[ \t]+|\n<details\b|\Z)"
)


def sync_approved_prompt_summary(
    body: str, approved_bullets: list[str]
) -> tuple[str, bool]:
    """Replace or append the `### Prompt Summary` section using Heimdall's curated bullets."""
    clean_bullets = [str(b).strip() for b in (approved_bullets or []) if str(b).strip()]
    if not clean_bullets:
        return body, False

    lines = ["### Prompt Summary"] + [
        f"{idx}. {bullet}" for idx, bullet in enumerate(clean_bullets, start=1)
    ]
    canonical = "\n".join(lines)

    m = PROMPT_SUMMARY_SECTION_RE.search(body)
    if m:
        replacement = f"{canonical}\n" if m.end() < len(body) else canonical
        new_body = body[: m.start()] + replacement + body[m.end() :]
        return new_body, new_body != body

    m_det = re.search(r"(?i)<details\b", body)
    if m_det:
        prefix = body[: m_det.start()].rstrip()
        suffix = body[m_det.start() :]
        new_body = f"{prefix}\n\n{canonical}\n\n{suffix}" if prefix else f"{canonical}\n\n{suffix}"
    else:
        prefix = body.rstrip()
        new_body = f"{prefix}\n\n{canonical}" if prefix else canonical
    return new_body, new_body != body


def enrich_pr_body(
    body: str,
    conversation_id: str,
    prompts: list[dict[str, str]],
    transcript_path: str = "",
    payload: dict | None = None,
    approved_summary: list[str] | None = None,
) -> tuple[str, bool]:
    """Ensure the PR body has the canonical Session ID, curated Prompt Summary, and optional Verbatim Prompts block."""
    updated = body.rstrip()
    changed = False
    if "\\`" in updated:
        updated = updated.replace("\\`", "`")
        changed = True

    if approved_summary is None:
        cwd = None
        if isinstance(payload, dict):
            ws_paths = payload.get("workspacePaths")
            cwd = payload.get("cwd") or (
                ws_paths[0] if isinstance(ws_paths, list) and ws_paths else None
            )
        approved_summary = load_approved_prompt_summary(conversation_id, cwd)

    if approved_summary:
        updated, summary_synced = sync_approved_prompt_summary(updated, approved_summary)
        if summary_synced:
            changed = True

    updated, hash_sanitized = sanitize_non_issue_hash_refs(updated)
    if hash_sanitized:
        changed = True

    sid = conversation_id or "unknown-session"
    agent_model = detect_agent_model(transcript_path, payload)

    # Replace placeholder session IDs if present
    placeholder_pat = re.compile(
        r"(\*\*Session ID\*\*:\s*`?)(?:<session-id>|<conversation-id>|TBD|AUTO)(`?)",
        re.IGNORECASE,
    )
    if placeholder_pat.search(updated):
        updated = placeholder_pat.sub(f"**Session ID**: `{sid}`", updated)
        changed = True

    model_placeholder_pat = re.compile(
        r"(\*\*Agent\s*/\s*Model\*\*:\s*`?)(?:<agent-or-model-name>|<model-name>|Claude Code|Antigravity|TBD|AUTO)(`?)",
        re.IGNORECASE,
    )
    if model_placeholder_pat.search(updated):
        updated = model_placeholder_pat.sub(
            f"**Agent / Model**: `{agent_model}`", updated
        )
        changed = True

    if TELEMETRY_HEADING not in updated:
        # Insert heading before ### Prompt Summary if present, else append
        m = re.search(r"(?m)^###?\s*Prompt\s+Summary\b", updated, re.IGNORECASE)
        if m:
            prefix = updated[: m.start()].rstrip()
            suffix = updated[m.start() :]
            updated = (
                f"{prefix}\n\n{TELEMETRY_HEADING}\n"
                f"- **Session ID**: `{sid}`\n"
                f"- **Agent / Model**: `{agent_model}`\n\n{suffix}"
            )
            changed = True
        else:
            updated = (
                f"{updated}\n\n{TELEMETRY_HEADING}\n"
                f"- **Session ID**: `{sid}`\n"
                f"- **Agent / Model**: `{agent_model}`"
            )
            changed = True
    else:
        if not re.search(r"\*\*Session ID\*\*:", updated, re.IGNORECASE):
            updated = updated.replace(
                TELEMETRY_HEADING,
                f"{TELEMETRY_HEADING}\n- **Session ID**: `{sid}`",
                1,
            )
            changed = True
        elif sid != "unknown-session" and sid not in updated:
            updated = re.sub(
                r"(\*\*Session ID\*\*:\s*`?)[^`\n]+(`?)",
                f"**Session ID**: `{sid}`",
                updated,
                count=1,
            )
            changed = True

        if not re.search(r"\*\*Agent\s*/\s*Model\*\*:", updated, re.IGNORECASE):
            updated = re.sub(
                r"(- \*\*Session ID\*\*:\s*`[^`\n]+`)",
                rf"\1\n- **Agent / Model**: `{agent_model}`",
                updated,
                count=1,
            )
            changed = True

    if not INCLUDE_VERBATIM_PROMPTS:
        padded_details_re = re.compile(
            r"\s*<details>\s*<summary>💬 Verbatim Session Prompts[\s\S]*?</details>\s*",
            re.IGNORECASE,
        )
        if padded_details_re.search(updated):
            updated = padded_details_re.sub("\n\n", updated).rstrip()
            changed = True
    elif prompts:
        details_block = build_verbatim_details_block(prompts)
        if details_block:
            if VERBATIM_SUMMARY_TAG not in updated:
                updated = f"{updated.rstrip()}\n\n{details_block}"
                changed = True
            else:
                m_det = VERBATIM_DETAILS_RE.search(updated)
                if m_det and m_det.group(0) != details_block:
                    updated = (
                        updated[: m_det.start()]
                        + details_block
                        + updated[m_det.end() :]
                    )
                    changed = True

    return updated, changed


def process_gh_command_tokens(
    tokens: list[str],
    conversation_id: str,
    prompts: list[dict[str, str]],
    transcript_path: str = "",
    payload: dict | None = None,
    approved_summary: list[str] | None = None,
) -> tuple[list[str] | None, str | None, bool]:
    """Inspect and enrich a single `gh pr create` or `gh pr edit` token list.

    Returns (new_tokens, deny_reason, modified).
    """
    gh_idx = -1
    for i in range(len(tokens) - 2):
        if (
            os.path.basename(tokens[i]) == "gh"
            and tokens[i + 1] == "pr"
            and tokens[i + 2] in ("create", "edit")
        ):
            gh_idx = i
            break

    if gh_idx == -1:
        return tokens, None, False

    subcmd = tokens[gh_idx + 2]
    is_create = subcmd == "create"

    has_label = False
    body_idx: int | None = None
    body_inline_prefix: str | None = None
    body_val: str | None = None

    i = gh_idx + 3
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--label", "-l", "--add-label") and i + 1 < len(tokens):
            if LABEL and LABEL in tokens[i + 1]:
                has_label = True
            i += 2
            continue
        if tok.startswith("--label=") or tok.startswith("--add-label="):
            if LABEL and LABEL in tok.split("=", 1)[1]:
                has_label = True
            i += 1
            continue
        if tok in ("--body", "-b") and i + 1 < len(tokens):
            body_idx = i + 1
            body_val = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--body="):
            body_idx = i
            body_inline_prefix = "--body="
            body_val = tok.split("=", 1)[1]
            i += 1
            continue
        i += 1

    if is_create and REQUIRE_CHANGE_SUMMARY and body_val is not None and not has_change_summary(body_val):
        return (
            None,
            (
                "The PR body says nothing about the change: add a `## Summary` before the "
                "`### Prompt Summary` that says what changed and why (behaviour changes, defaults you "
                "chose for open questions, deleted files or APIs, tests added). Reuse the pre-PR review's "
                "--diff-rationale and --file-note text. GitHub reviewers can't see the Odin state file."
            ),
            False,
        )
    if approved_summary is None:
        cwd = None
        if isinstance(payload, dict):
            ws_paths = payload.get("workspacePaths")
            cwd = payload.get("cwd") or (
                ws_paths[0] if isinstance(ws_paths, list) and ws_paths else None
            )
        approved_summary = load_approved_prompt_summary(conversation_id, cwd)

    if is_create and REQUIRE_PROMPT_SUMMARY:
        if body_val is None or (not has_prompt_summary(body_val) and not approved_summary):
            if INCLUDE_VERBATIM_PROMPTS:
                hook_adds = (
                    f"the Session ID (`{conversation_id or 'session_id'}`), the model, "
                    + (f'`--label "{LABEL}"`, ' if LABEL else "")
                    + "and a collapsible `<details>` block of verbatim prompts.)"
                )
            else:
                hook_adds = (
                    f"the Session ID (`{conversation_id or 'session_id'}`), the model"
                    + (f', and `--label "{LABEL}"`.)' if LABEL else ".)")
                )
            return (
                None,
                (
                    "Missing PR session telemetry: `gh pr create` must include a "
                    "`### Prompt Summary` bullet list in `--body` summarizing the user prompts "
                    f"for this PR. (The Odin PreToolUse hook then adds {hook_adds}"
                ),
                False,
            )

    new_tokens = list(tokens)
    modified = False

    # Sanitize --title / -t for accidental non-issue #<digits> autolinks
    t_idx = gh_idx + 3
    while t_idx < len(new_tokens):
        tok = new_tokens[t_idx]
        if tok in ("--title", "-t") and t_idx + 1 < len(new_tokens):
            san_title, t_changed = sanitize_non_issue_hash_refs(new_tokens[t_idx + 1])
            if t_changed:
                new_tokens[t_idx + 1] = san_title
                modified = True
            t_idx += 2
            continue
        if tok.startswith("--title="):
            raw_title = tok.split("=", 1)[1]
            san_title, t_changed = sanitize_non_issue_hash_refs(raw_title)
            if t_changed:
                new_tokens[t_idx] = f"--title={san_title}"
                modified = True
            t_idx += 1
            continue
        t_idx += 1

    if body_val is not None and body_idx is not None:
        unwrapped_body, heredoc_unwrapped = unwrap_heredoc_subshell(body_val)
        if has_prompt_summary(unwrapped_body) or approved_summary:
            enriched_body, body_changed = enrich_pr_body(
                unwrapped_body,
                conversation_id,
                prompts,
                transcript_path,
                payload,
                approved_summary=approved_summary,
            )
            if body_changed or heredoc_unwrapped:
                if body_inline_prefix:
                    new_tokens[body_idx] = f"{body_inline_prefix}{enriched_body}"
                else:
                    new_tokens[body_idx] = enriched_body
                modified = True
        else:
            san_body, b_changed = sanitize_non_issue_hash_refs(unwrapped_body)
            if not INCLUDE_VERBATIM_PROMPTS:
                padded_details_re = re.compile(
                    r"\s*<details>\s*<summary>💬 Verbatim Session Prompts[\s\S]*?</details>\s*",
                    re.IGNORECASE,
                )
                if padded_details_re.search(san_body):
                    san_body = padded_details_re.sub("\n\n", san_body).rstrip()
                    b_changed = True
            if b_changed or heredoc_unwrapped:
                if body_inline_prefix:
                    new_tokens[body_idx] = f"{body_inline_prefix}{san_body}"
                else:
                    new_tokens[body_idx] = san_body
                modified = True

    if is_create and LABEL and not has_label:
        insert_pos = gh_idx + 3
        new_tokens[insert_pos:insert_pos] = ["--label", LABEL]
        modified = True

    return new_tokens, None, modified


def process_git_commit_tokens(tokens: list[str]) -> tuple[list[str], bool]:
    """Sanitize non-issue #<digits> references in `git commit -m/--message`."""
    git_idx = -1
    commit_idx = -1
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) == "git":
            git_idx = i
            for j in range(i + 1, len(tokens)):
                if tokens[j] == "commit":
                    commit_idx = j
                    break
            if commit_idx != -1:
                break

    if git_idx == -1 or commit_idx == -1:
        return tokens, False

    new_tokens = list(tokens)
    modified = False
    i = commit_idx + 1
    while i < len(new_tokens):
        tok = new_tokens[i]
        if tok in ("-m", "--message") and i + 1 < len(new_tokens):
            unwrapped_msg, h_unwrapped = unwrap_heredoc_subshell(new_tokens[i + 1])
            san_msg, changed = sanitize_non_issue_hash_refs(unwrapped_msg)
            if changed or h_unwrapped:
                # Unwrapped heredocs only need swapping in; re-quoting happens only if something else rebuilds the command.
                new_tokens[i + 1] = san_msg
                modified = modified or changed
            i += 2
            continue
        if tok.startswith("--message="):
            raw_msg = tok.split("=", 1)[1]
            unwrapped_msg, h_unwrapped = unwrap_heredoc_subshell(raw_msg)
            san_msg, changed = sanitize_non_issue_hash_refs(unwrapped_msg)
            if changed or h_unwrapped:
                new_tokens[i] = f"--message={san_msg}"
                modified = modified or changed
            i += 1
            continue
        if tok.startswith("-m") and len(tok) > 2 and not tok.startswith("--"):
            unwrapped_msg, h_unwrapped = unwrap_heredoc_subshell(tok[2:])
            san_msg, changed = sanitize_non_issue_hash_refs(unwrapped_msg)
            if changed or h_unwrapped:
                new_tokens[i] = f"-m{san_msg}"
                modified = modified or changed
            i += 1
            continue
        i += 1

    return new_tokens, modified


def _deny(reason: str, is_agy: bool = False) -> dict:
    if is_agy:
        return {"decision": "deny", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _noop(is_agy: bool = False) -> dict | None:
    return {"decision": "allow"} if is_agy else None


def handle_pre_tool_use(payload: dict) -> dict | None:
    """Process a Claude Code or Antigravity PreToolUse payload."""
    is_agy = is_antigravity_payload(payload)
    if not PR_CONFIG:
        return _noop(is_agy)

    if is_agy:
        tool_call = payload.get("toolCall") or {}
        tool_name = str(tool_call.get("name") or "")
        tool_args = tool_call.get("args") or {}
        cmd_line = str(tool_args.get("CommandLine") or "")
        expected_tool = "run_command"
        conversation_id = str(payload.get("conversationId") or "").strip()
        transcript_path = str(payload.get("transcriptPath") or "").strip()
    else:
        tool_name = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        cmd_line = str(tool_input.get("command") or "")
        expected_tool = "Bash"
        conversation_id = str(payload.get("session_id") or "").strip()
        transcript_path = str(payload.get("transcript_path") or "").strip()

    has_gh_pr = bool(re.search(r"\bgh\s+pr\s+(create|edit)\b", cmd_line))
    has_git_commit = bool(re.search(r"\bgit\b[^\n;|&]*\bcommit\b", cmd_line))
    if tool_name != expected_tool or not (has_gh_pr or has_git_commit):
        return _noop(is_agy)

    if not conversation_id and transcript_path:
        m = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            transcript_path,
            re.IGNORECASE,
        )
        if m:
            conversation_id = m.group(1)

    prompts = extract_verbatim_prompts(transcript_path, conversation_id)
    ws_paths = payload.get("workspacePaths")
    cwd = payload.get("cwd") or (
        ws_paths[0] if isinstance(ws_paths, list) and ws_paths else None
    )
    approved_summary = load_approved_prompt_summary(conversation_id, cwd)

    # Normalize backslash-newline line continuations outside quotes before shlex.split
    normalized_cmd = re.sub(r"\\\r?\n\s*", " ", cmd_line)
    try:
        tokens = shlex.split(normalized_cmd, posix=True)
    except ValueError:
        # Fallback regex check if complex shell syntax (e.g. heredoc) is used
        if (
            REQUIRE_PROMPT_SUMMARY
            and re.search(r"\bgh\s+pr\s+create\b", cmd_line)
            and not has_prompt_summary(cmd_line)
            and not approved_summary
        ):
            return _deny(
                "Missing PR session telemetry: `gh pr create` must include `### Prompt Summary` bullets.",
                is_agy,
            )
        return _noop(is_agy)

    # Check if tokens contain shell chaining (`&&`, `;`, `||`); segment by operator
    segments: list[list[str]] = []
    operators: list[str] = []
    cur: list[str] = []
    for t in tokens:
        if t in ("&&", "||", ";"):
            segments.append(cur)
            operators.append(t)
            cur = []
        else:
            cur.append(t)
    segments.append(cur)

    any_modified = False
    rebuilt_segments: list[str] = []
    for seg in segments:
        new_seg, deny_reason, mod = process_gh_command_tokens(
            seg,
            conversation_id,
            prompts,
            transcript_path,
            payload,
            approved_summary=approved_summary,
        )
        if deny_reason:
            return _deny(deny_reason, is_agy)
        assert new_seg is not None
        new_seg, commit_mod = process_git_commit_tokens(new_seg)
        if mod or commit_mod:
            any_modified = True
        rebuilt_segments.append(" ".join(shlex.quote(x) for x in new_seg))

    if not any_modified:
        return _noop(is_agy)

    rebuilt_cmd = rebuilt_segments[0]
    for op, seg_str in zip(operators, rebuilt_segments[1:]):
        rebuilt_cmd = f"{rebuilt_cmd} {op} {seg_str}"

    if is_agy:
        return {
            "decision": "allow",
            "overwrite": {"CommandLine": rebuilt_cmd},
        }

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "Odin added PR session telemetry to this command.",
            "updatedInput": {**(payload.get("tool_input") or {}), "command": rebuilt_cmd},
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Odin PR PreToolUse hook & session telemetry formatter"
    )
    parser.add_argument("--session-id", help="Session/Conversation ID (for CLI helper mode)")
    parser.add_argument("--transcript", help="Explicit path to a session transcript .jsonl (for CLI helper mode)")
    parser.add_argument(
        "--print-verbatim",
        action="store_true",
        help="Print the collapsible <details> block of verbatim prompts for --session-id",
    )
    args = parser.parse_args()

    if args.print_verbatim:
        prompts = extract_verbatim_prompts(args.transcript, args.session_id)
        print(build_verbatim_details_block(prompts))
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    is_agy = is_antigravity_payload(payload)
    if is_agy:
        command = str(((payload.get("toolCall") or {}).get("args") or {}).get("CommandLine") or "")
    else:
        command = str((payload.get("tool_input") or {}).get("command") or "")

    if not re.search(r"\bgh\s+pr\b|\bgit\b[^\n;|&]*\bcommit\b", command):
        if is_agy:
            print(json.dumps({"decision": "allow"}))
        return 0  # fast path: this hook runs on every Bash / run_command call

    ws_paths = payload.get("workspacePaths")
    cwd = payload.get("cwd") or (ws_paths[0] if isinstance(ws_paths, list) and ws_paths else None)
    configure(load_config(cwd).get("pr"))
    result = handle_pre_tool_use(payload)
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
