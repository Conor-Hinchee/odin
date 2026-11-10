#!/usr/bin/env python3
"""Odin eval runner (milestone 0: spike).

Runs one case through headless Claude Code with the Odin plugin in an isolated
sandbox, then grades the end state with the case's hidden tests.

    python3 evals/run.py <case-id> [--mode waiver|gated] [--out DIR] [--odin-dir DIR]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EVALS = Path(__file__).resolve().parent
REPO_ROOT = EVALS.parent

WAIVER_SUFFIX = (
    "\n\nEval run, no human available: I waive plan review, so go straight from Phase 1 to "
    "Phase 2. I also waive the pre-PR gate and sign-off: do not push or open a PR, and stop "
    "after Phase 4 (Hlidskjalf)."
)


def sh(cmd, cwd, env=None, check=True):
    return subprocess.run(cmd, cwd=cwd, env=env, check=check, text=True, capture_output=True)


def make_sandbox(case_dir: Path, out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    repo = out / "repo"
    shutil.copytree(case_dir / "repo", repo)
    origin = out / "origin.git"
    sh(["git", "init", "--bare", "-q", str(origin)], cwd=out)
    sh(["git", "init", "-q", "-b", "main"], cwd=repo)
    sh(["git", "add", "-A"], cwd=repo)
    sh(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.invalid",
        "commit", "-q", "-m", "initial"], cwd=repo)
    sh(["git", "remote", "add", "origin", str(origin)], cwd=repo)
    sh(["git", "push", "-q", "origin", "main"], cwd=repo)
    (out / "xdg").mkdir()
    return {"repo": repo, "origin": origin}


def grade(case: dict, case_dir: Path, repo: Path, out: Path) -> dict:
    grade_dir = out / "grade"
    shutil.copytree(repo, grade_dir, ignore=shutil.ignore_patterns(".git", "odin-states"))
    shutil.copytree(case_dir / "hidden", grade_dir / "hidden")
    hidden = subprocess.run(case["hidden_test_cmd"], shell=True, cwd=grade_dir,
                            text=True, capture_output=True)
    visible = subprocess.run(case["visible_test_cmd"], shell=True, cwd=grade_dir,
                             text=True, capture_output=True)
    diff = sh(["git", "diff", "--stat", "main@{0}", "--"], cwd=repo, check=False)
    first = sh(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=repo).stdout.strip()
    changed = sh(["git", "diff", "--name-only", first], cwd=repo, check=False).stdout.split()
    untracked = sh(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo).stdout.split()
    return {
        "hidden_pass": hidden.returncode == 0,
        "hidden_tail": hidden.stderr.strip().splitlines()[-1:] or [""],
        "visible_pass": visible.returncode == 0,
        "changed_files": sorted(set(changed + untracked)),
        "branches": sh(["git", "branch", "--format=%(refname:short)"], cwd=repo).stdout.split(),
        "commits": sh(["git", "log", "--all", "--oneline"], cwd=repo).stdout.strip().splitlines(),
        "worktrees": sh(["git", "worktree", "list"], cwd=repo).stdout.strip().splitlines(),
        "diff_stat": diff.stdout.strip(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case")
    ap.add_argument("--mode", choices=["waiver", "gated"], default="waiver")
    ap.add_argument("--out", required=True)
    ap.add_argument("--odin-dir", default=str(REPO_ROOT))
    ap.add_argument("--model", default="")
    ap.add_argument("--timeout", type=int, default=1500)
    args = ap.parse_args()

    case_dir = EVALS / "cases" / args.case
    case = json.loads((case_dir / "case.json").read_text())
    out = Path(args.out).resolve()
    sb = make_sandbox(case_dir, out)

    env = dict(os.environ)
    env.update({
        "PATH": f"{EVALS / 'bin'}:{env['PATH']}",
        "XDG_CONFIG_HOME": str(out / "xdg"),
        "ODIN_ROOT": str(out),
        "ODIN_EVAL_GH_LOG": str(out / "gh.log"),
    })
    prompt = f"/odin:odin {case['prompt']}"
    if args.mode == "waiver":
        prompt += WAIVER_SUFFIX
    cmd = [
        "claude", "-p", prompt,
        "--plugin-dir", args.odin_dir,
        # Turn off the user-installed copy so only --plugin-dir's Odin loads.
        "--settings", json.dumps({"enabledPlugins": {"odin@odin": False}}),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", str(case.get("budget_usd", 3)),
    ]
    if args.model:
        cmd += ["--model", args.model]

    partner = None
    if args.mode == "gated":
        partner = subprocess.Popen(
            [sys.executable, str(EVALS / "partner.py"), str(sb["repo"] / "odin-states"),
             "--timeout", str(args.timeout), "--log", str(out / "partner.log")],
            stdout=subprocess.DEVNULL, stderr=open(out / "partner.err", "w"))

    started = time.time()
    status = "completed"
    with open(out / "transcript.jsonl", "w") as tf, open(out / "stderr.log", "w") as ef:
        try:
            proc = subprocess.run(cmd, cwd=sb["repo"], env=env, stdout=tf, stderr=ef,
                                  text=True, timeout=args.timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc, status = None, "timeout"
        finally:
            if partner:
                partner.terminate()
    wall = time.time() - started

    result_event = {}
    for line in (out / "transcript.jsonl").read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            result_event = ev
    row = {
        "case": args.case, "mode": args.mode, "status": status, "exit_code": rc,
        "wall_s": round(wall, 1),
        "cost_usd": result_event.get("total_cost_usd"),
        "num_turns": result_event.get("num_turns"),
        "result_subtype": result_event.get("subtype"),
        "usage": result_event.get("usage"),
        "models": sorted((result_event.get("modelUsage") or {}).keys()),
        "final_text": (result_event.get("result") or "")[-1500:],
        "states": sorted(str(p.relative_to(out)) for p in out.rglob("odin-states/*.html")),
    }
    row["grade"] = grade(case, case_dir, sb["repo"], out)
    (out / "row.json").write_text(json.dumps(row, indent=2))
    print(json.dumps(row, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
