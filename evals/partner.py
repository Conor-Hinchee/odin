#!/usr/bin/env python3
"""Scripted human for Odin's approval gates.

Watches a trial directory (recursively, so worktrees Odin creates are covered) for
armed `wait-for-human` watchers and answers each gate the way the dashboard does:
a POST to the trial's own Odin server, which records the decision in the state file
and then writes the wakeup. Without --server it only writes the wakeup file, which
the agent can (rightly) refuse because the state file shows no approval.

    python3 evals/partner.py <trial dir> --server http://127.0.0.1:<port> [--script '{...}']

Each phase maps to a list of decisions, used in order; the last one repeats.

With --review-case <id> (trap cases, Experiment 2) it reviews instead of approving: the plan goes to
reviewer.review_plan, the code at the pre-PR gate to the case's hidden tests, and a wrong one gets the
case's correction (revise_huginn / reject_pr), at most reviewer.MAX_CORRECTIONS times per trial.
"""
import argparse
import importlib.util
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

EVALS = Path(__file__).resolve().parent
SCRIPTS = EVALS.parent / "skills" / "odin" / "scripts"
sys.path.insert(0, str(EVALS))
DEFAULT_SCRIPT = {
    "huginn-plan": ["approve_huginn"],
    "heimdall-validation": ["approve_pr"],
    "human-validation": ["approve_signoff"],
}


def load_state_module():
    spec = importlib.util.spec_from_file_location("odin_state", SCRIPTS / "odin-state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dashboard_body(phase: str, decision: str, rel_id: str, notes: str) -> dict:
    """The request the dashboard's buttons send for each gate (see odin-server.py do_POST)."""
    if phase == "huginn-plan":
        return {"action": "save_partner_plan", "id": rel_id, "decision": decision, "partnerNotes": notes}
    if phase == "heimdall-validation":
        return {"action": "heimdall_gate_decision", "id": rel_id, "decision": decision, "humanNote": notes}
    if phase == "human-validation":
        status = "done" if decision == "approve_signoff" else "reject"
        return {"action": "update_project", "id": rel_id, "phases": {"human-validation": status},
                "humanNote": notes}
    return {"action": "trigger_wakeup", "id": rel_id, "decision": decision, "phase": phase, "notes": notes}


def reviewed_decision(case: dict, phase: str, state_file: Path, root: Path, corrections: int) -> tuple:
    """(decision, notes, record) for a reviewing partner. Past the correction cap it approves, like a tired human."""
    import judge
    import reviewer
    record: dict = {}
    content = state_file.read_text(errors="replace")
    if phase == "huginn-plan":
        record = reviewer.review_plan(case, judge.plan_text(content))
        wrong = record["verdict"] == "revise"
        ok, bad = "approve_huginn", "revise_huginn"
    elif phase == "heimdall-validation":
        m = re.search(r'<body[^>]*\bdata-worktree="([^"]*)"', content)
        src = Path(m.group(1)) if m and m.group(1) and Path(m.group(1)).is_dir() else root / "repo"
        record = {**reviewer.review_code(case, src), "src": str(src)}
        wrong = not record["hidden_pass"]
        ok, bad = "approve_pr", "reject_pr"
    else:
        return "approve_signoff", reviewer.APPROVE_NOTE, record
    if wrong and corrections < reviewer.MAX_CORRECTIONS:
        return bad, case["review"]["correction"], {**record, "correction": True}
    return ok, reviewer.APPROVE_NOTE, {**record, "correction": False, "capped": wrong}


def post(server: str, body: dict) -> dict:
    req = urllib.request.Request(server.rstrip("/") + "/api/projects/action",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--script", default="")
    ap.add_argument("--notes", default=(
        "Approved. For any open questions, go with the defaults you recommended."))
    ap.add_argument("--log", default="")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--server", default="", help="the trial's Odin server; answer through its API")
    ap.add_argument("--review-case", default="", help="review against this case's intent instead of approving")
    args = ap.parse_args()

    script = json.loads(args.script) if args.script else DEFAULT_SCRIPT
    used: dict = {}
    mod = load_state_module()
    root = Path(args.root)
    answered: set = set()
    case = json.loads((EVALS / "cases" / args.review_case / "case.json").read_text()) if args.review_case else None
    corrections = 0
    deadline = time.time() + args.timeout
    log = open(args.log, "a") if args.log else sys.stdout

    while time.time() < deadline:
        watchers = [p for p in root.rglob(".odin-watcher-*.json") if "odin-states" in p.parts] if root.exists() else []
        for wf in sorted(watchers):
            try:
                hb = json.loads(wf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            key = (hb.get("pid"), hb.get("startedAt"))
            if key in answered:
                continue
            phase = hb.get("phase") or ""
            decisions = script.get(phase) or ["approve"]
            n = used.get(phase, 0)
            decision = decisions[min(n, len(decisions) - 1)]
            used[phase] = n + 1
            time.sleep(2)  # let the watcher finish arming (it clears stale wakeups on start)
            notes, review = args.notes, None
            if case:
                try:
                    decision, notes, review = reviewed_decision(case, phase, Path(hb["file"]), root, corrections)
                    corrections += bool(review.get("correction"))
                except Exception as exc:  # a reviewer failure approves, and is logged, so the trial doesn't hang
                    review = {"error": repr(exc)}
            via, err = "wakeup-file", ""
            if args.server:
                try:
                    rel_id = str(Path(hb["file"]).resolve().relative_to(root.resolve()))
                    res = post(args.server, dashboard_body(phase, decision, rel_id, notes))
                    via = "dashboard-api" if res.get("ok") else "dashboard-api-failed"
                    err = "" if res.get("ok") else str(res.get("error"))
                except Exception as exc:  # fall back so the trial doesn't hang
                    via, err = "dashboard-api-failed", repr(exc)
            if via != "dashboard-api":
                mod.write_wakeup_event(hb["file"], decision, phase=phase, notes=notes)
            answered.add(key)
            print(json.dumps({"t": time.strftime("%H:%M:%S"), "phase": phase, "decision": decision,
                              "via": via, "error": err, "watcher_pid": hb.get("pid"),
                              "file": hb.get("file"), "review": review}), file=log, flush=True)
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
