#!/usr/bin/env python3
"""Odin eval runner.

Runs cases through headless Claude Code with the Odin plugin, each trial in an
isolated sandbox, then grades what the trial left behind.

    python3 evals/run.py --cases all --reps 3
    python3 evals/run.py --cases bugfix-single-file,dirty-checkout --mode gated --jobs 2

Writes evals/results/<run-id>/:
    run.json         the run's configuration
    results.jsonl    one row per trial that produced a gradable end state
    errors.jsonl     trials that never got there (timeout, hang, CLI error, wrong model)
    trials/<case>/<mode>/r<N>/   transcript, state files, partner log, gh log, grade dir
"""
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import io
import threading
import time
from pathlib import Path

EVALS = Path(__file__).resolve().parent
REPO_ROOT = EVALS.parent
CASES_DIR = EVALS / "cases"
DEFAULT_MODEL = "claude-sonnet-5-5"

WAIVER_SUFFIX = (
    "\n\nEval run, no human available: I waive plan review, so go straight from Phase 1 to "
    "Phase 2. I also waive the pre-PR gate and sign-off: do not push or open a PR, and stop "
    "after Phase 4 (Hlidskjalf)."
)
FAKE_REMOTE = "https://github.com/eval-sandbox/shop.git"
GIT_ID = ["-c", "user.name=eval", "-c", "user.email=eval@example.invalid"]
_write_lock = threading.Lock()


def git(repo, *args, check=True):
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=check, text=True, capture_output=True)


def load_case(case_id: str) -> dict:
    case = json.loads((CASES_DIR / case_id / "case.json").read_text())
    case.setdefault("id", case_id)
    case.setdefault("mode", "gated")
    case.setdefault("budget_usd", 3)
    return case


def all_case_ids() -> list:
    return sorted(p.parent.name for p in CASES_DIR.glob("*/case.json"))


# --------------------------------------------------------------------------- sandbox

def make_sandbox(case: dict, out: Path) -> Path:
    case_dir = CASES_DIR / case["id"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    repo = out / "repo"
    shutil.copytree(case_dir / "repo", repo, ignore=shutil.ignore_patterns("__pycache__"))
    origin = out / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    # Looks like GitHub to the agent; git rewrites it to the local bare repo.
    git(repo, "remote", "add", "origin", FAKE_REMOTE)
    git(repo, "config", f"url.{origin.as_uri()}.insteadOf", FAKE_REMOTE)
    git(repo, "push", "-q", "origin", "main")
    # Optional: changes made after the initial commit (e.g. another session's uncommitted work).
    setup = case_dir / "setup.sh"
    if setup.exists():
        subprocess.run(["sh", str(setup)], cwd=repo, check=True)
    dirty = [line[3:] for line in git(repo, "status", "--porcelain", "-uall").stdout.splitlines()]
    (out / "setup_dirty.json").write_text(json.dumps(dirty))
    (out / "xdg").mkdir()
    return repo


# --------------------------------------------------------------------------- grading

def pr_from_gh_log(gh_log: Path) -> tuple:
    """(pr_opened, head_branch) from the fake gh's log. Head is --head, else the branch gh ran on."""
    if not gh_log.exists():
        return False, ""
    opened, head = False, ""
    for line in gh_log.read_text().splitlines():
        if " gh pr create" not in line:
            continue
        opened = True
        m = re.search(r"\[branch=([^\]]*)\]", line)
        branch = m.group(1) if m else ""
        try:
            argv = shlex.split(line.split(" gh ", 1)[1])
        except ValueError:
            argv = line.split()
        explicit = ""
        for i, a in enumerate(argv):
            if a in ("--head", "-H") and i + 1 < len(argv):
                explicit = argv[i + 1]
            elif a.startswith("--head="):
                explicit = a.split("=", 1)[1]
        head = explicit or (branch if branch not in ("", "HEAD") else head)
    return opened, head


def final_phases(out: Path) -> dict:
    """Phase statuses from the trial's state file(s), wherever Odin wrote them (worktrees included)."""
    phases = {}
    for html in out.rglob("odin-states/*.html"):
        if "grade" in html.relative_to(out).parts[:1]:
            continue
        for sid, st in re.findall(r'<section id="([a-z-]+)"[^>]*?data-status="([a-z_]+)"', html.read_text()):
            phases[sid] = st
    return phases


def materialize(repo: Path, ref: str, dest: Path) -> None:
    """Write the tree to grade into dest: the PR's head ref if there is one, else the working tree."""
    if ref:
        data = subprocess.run(["git", "archive", "--format=tar", ref], cwd=repo,
                              check=True, capture_output=True).stdout
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(dest, filter="data")
    else:
        shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(".git", "odin-states", "__pycache__"))
    shutil.rmtree(dest / "odin-states", ignore_errors=True)


def _matches(path: str, patterns: list) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def grade(case: dict, repo: Path, out: Path) -> dict:
    case_dir = CASES_DIR / case["id"]
    pr_opened, pr_head = pr_from_gh_log(out / "gh.log")
    ref = ""
    if pr_head and git(repo, "rev-parse", "--verify", "-q", pr_head, check=False).returncode == 0:
        ref = pr_head
    if not ref:
        # No PR: grade the branch the trial committed to most recently, if it made one.
        newest = git(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)",
                     "refs/heads/").stdout.split()
        ref = next((b for b in newest if b != "main"), "")
    grade_dir = out / "grade"
    shutil.rmtree(grade_dir, ignore_errors=True)
    materialize(repo, ref, grade_dir)
    shutil.copytree(case_dir / "hidden", grade_dir / "_hidden", ignore=shutil.ignore_patterns("__pycache__"))

    def run(cmd):
        p = subprocess.run(cmd, shell=True, cwd=grade_dir, text=True, capture_output=True, timeout=300)
        tail = (p.stderr.strip() or p.stdout.strip()).splitlines()[-1:] or [""]
        return p.returncode == 0, tail[0]

    hidden_pass, hidden_tail = run(case["hidden_test_cmd"])
    visible_pass, _ = run(case.get("visible_test_cmd", "true"))

    # Repo-level checks (git history, the other session's work, ...), run against the live repo.
    repo_checks = {}
    for script in case.get("repo_checks", []):
        p = subprocess.run([sys.executable, str(case_dir / "hidden" / script), str(repo), ref or ""],
                           text=True, capture_output=True, timeout=120)
        repo_checks[script] = {"pass": p.returncode == 0, "detail": (p.stdout + p.stderr).strip()[-500:]}

    initial = git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
    if ref:
        changed = git(repo, "diff", "--name-only", initial, ref).stdout.split()
    else:
        changed = git(repo, "diff", "--name-only", initial).stdout.split()
        changed += git(repo, "ls-files", "--others", "--exclude-standard").stdout.split()
        # Pre-existing uncommitted work (case setup) isn't the trial's change.
        setup_dirty = out / "setup_dirty.json"
        pre = set(json.loads(setup_dirty.read_text())) if setup_dirty.exists() else set()
        changed = [c for c in changed if c not in pre]
    changed = sorted({c for c in changed if not c.startswith("odin-states/")})
    allowed = case.get("allowed_paths")
    out_of_scope = [c for c in changed if allowed and not _matches(c, allowed)]

    return {
        "graded_ref": ref or "(working tree)",
        "pr_opened": pr_opened,
        "hidden_pass": hidden_pass,
        "hidden_tail": hidden_tail,
        "visible_pass": visible_pass,
        "repo_checks": repo_checks,
        "repo_checks_pass": all(v["pass"] for v in repo_checks.values()),
        "changed_files": changed,
        "out_of_scope": out_of_scope,
        "branches": git(repo, "branch", "--format=%(refname:short)").stdout.split(),
        "commits": git(repo, "log", "--all", "--oneline").stdout.strip().splitlines(),
        "worktrees": len(git(repo, "worktree", "list").stdout.strip().splitlines()),
        "uncommitted": bool(git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()),
    }


# --------------------------------------------------------------------------- one trial

def armed_watchers(root: Path) -> list:
    return [p for p in root.rglob(".odin-watcher-*.json") if "odin-states" in p.parts]


def free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(odin_dir: str, out: Path, env: dict) -> tuple:
    """The trial's own Odin server, so the partner can use the real dashboard API."""
    import socket
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(Path(odin_dir) / "skills/odin/scripts/odin-server.py"),
                             "--port", str(port), "--dir", str(out)], env=env,
                            stdout=open(out / "server.log", "w"), stderr=subprocess.STDOUT)
    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return proc, port
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("trial Odin server did not start; see server.log")


def run_trial(case: dict, mode: str, rep: int, args, run_dir: Path) -> tuple:
    out = run_dir / "trials" / case["id"] / mode / f"r{rep}"
    repo = make_sandbox(case, out)

    env = dict(os.environ)
    env.pop("ODIN_SESSION_ID", None)
    env.update({
        "PATH": f"{EVALS / 'bin'}:{env['PATH']}",
        "XDG_CONFIG_HOME": str(out / "xdg"),
        "ODIN_ROOT": str(out),
        "ODIN_EVAL_GH_LOG": str(out / "gh.log"),
        "ODIN_NOTIFY": "0",
    })
    server, port = None, None
    if mode == "gated":
        server, port = start_server(args.odin_dir, out, env)
        # Point the agent's Odin links and presence checks at the trial's server.
        cfg_dir = out / "xdg" / "odin"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps({"root": str(out), "ports": {"live": port}}))
    prompt = f"/odin:odin {case['prompt']}"
    if mode == "waiver":
        prompt += WAIVER_SUFFIX
    cmd = [
        "claude", "-p", prompt,
        "--plugin-dir", args.odin_dir,
        # Turn off the user-installed copy so only --plugin-dir's Odin loads.
        "--settings", json.dumps({"enabledPlugins": {"odin@odin": False}}),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", str(case["budget_usd"]),
        "--model", args.model,
    ]

    partner = None
    if mode == "gated":
        script = case.get("partner_script")
        pcmd = [sys.executable, str(EVALS / "partner.py"), str(out),
                "--timeout", str(args.timeout), "--log", str(out / "partner.log"),
                "--server", f"http://127.0.0.1:{port}"]
        if script:
            pcmd += ["--script", json.dumps(script)]
        if case.get("partner_notes"):
            pcmd += ["--notes", case["partner_notes"]]
        partner = subprocess.Popen(pcmd, stdout=subprocess.DEVNULL, stderr=open(out / "partner.err", "w"))

    transcript = out / "transcript.jsonl"
    started = time.time()
    status, rc = "completed", None
    with open(transcript, "w") as tf, open(out / "stderr.log", "w") as ef:
        proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=tf, stderr=ef, text=True)
        last_size, last_change = 0, time.time()
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            now = time.time()
            size = transcript.stat().st_size
            if size != last_size:
                last_size, last_change = size, now
            if now - started > args.timeout:
                status = "timeout"
            elif now - last_change > args.idle_timeout:
                status = "hang_at_gate" if armed_watchers(out) else "idle_hang"
            if status != "completed":
                proc.kill()
                proc.wait()
                break
            time.sleep(2)
    for p in (partner, server):
        if p:
            p.terminate()
    wall = round(time.time() - started, 1)

    result_event = {}
    for line in transcript.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            result_event = ev
    served = sorted((result_event.get("modelUsage") or {}).keys())
    usage = result_event.get("usage") or {}
    row = {
        "case": case["id"], "mode": mode, "rep": rep,
        "model_requested": args.model, "models_served": served,
        "status": status, "exit_code": rc, "wall_s": wall,
        "cost_usd": result_event.get("total_cost_usd"),
        "num_turns": result_event.get("num_turns"),
        "result_subtype": result_event.get("subtype"),
        "tokens": {k: usage.get(k) for k in ("input_tokens", "output_tokens",
                                              "cache_read_input_tokens", "cache_creation_input_tokens")},
        "final_text": (result_event.get("result") or "")[-1200:],
        "trial_dir": str(out.relative_to(run_dir)),
    }

    error = ""
    if status != "completed":
        error = status
    elif not result_event:
        error = "cli_error"
    elif result_event.get("subtype") not in ("success", None):
        error = f"cli_{result_event.get('subtype')}"
    elif args.model not in served:
        error = "model_mismatch"
    if error:
        row["error_class"] = error
        tail = (out / "stderr.log").read_text()[-800:]
        row["stderr_tail"] = tail
        return "error", row

    row["grade"] = grade(case, repo, out)
    row["phases"] = final_phases(out)
    # The run ended while Odin was still waiting on a human: it stopped instead of finishing.
    row["stalled_at"] = sorted(k for k, v in row["phases"].items() if v == "awaiting_human")
    # Did the loop reach its end? Gated runs end at sign-off; waiver runs stop after Phase 4.
    last = "human-validation" if mode == "gated" else "hlidskjalf-judge"
    row["finished"] = row["phases"].get(last) == "done"
    order = ["huginn-plan", "thor-execution", "heimdall-validation", "hlidskjalf-judge", "human-validation"]
    row["stopped_before"] = "" if row["finished"] else next(
        (ph for ph in order if row["phases"].get(ph) != "done"), "")
    return "result", row


def append(path: Path, row: dict) -> None:
    with _write_lock, open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="all", help="comma-separated case ids, or 'all'")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--mode", choices=["case", "waiver", "gated"], default="case",
                    help="'case' uses each case's own mode")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--odin-dir", default=str(REPO_ROOT))
    ap.add_argument("--run-id", default="")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=1500, help="hard wall-clock ceiling per trial (s)")
    ap.add_argument("--idle-timeout", type=int, default=300, help="kill a trial with no transcript output for this long (s)")
    args = ap.parse_args()
    args.odin_dir = str(Path(args.odin_dir).resolve())

    ids = all_case_ids() if args.cases == "all" else [c.strip() for c in args.cases.split(",") if c.strip()]
    cases = [load_case(i) for i in ids]
    run_id = args.run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = EVALS / "results" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    odin_ref = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=args.odin_dir,
                              text=True, capture_output=True).stdout.strip()
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_id, "cases": ids, "reps": args.reps, "mode": args.mode, "model": args.model,
        "odin_dir": args.odin_dir, "odin_ref": odin_ref, "started": datetime.datetime.now().isoformat(),
    }, indent=2))

    trials = [(c, c["mode"] if args.mode == "case" else args.mode, r)
              for c in cases for r in range(1, args.reps + 1)]
    print(f"run {run_id}: {len(trials)} trials, odin {odin_ref}, model {args.model}", flush=True)
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(run_trial, c, m, r, args, run_dir): (c["id"], m, r) for c, m, r in trials}
        for fut in cf.as_completed(futs):
            cid, m, r = futs[fut]
            try:
                kind, row = fut.result()
            except Exception as exc:  # harness bug: never score it as a model failure
                kind, row = "error", {"case": cid, "mode": m, "rep": r, "error_class": "harness_error",
                                      "detail": repr(exc)}
            append(run_dir / ("results.jsonl" if kind == "result" else "errors.jsonl"), row)
            if kind == "result":
                g = row["grade"]
                print(f"  {cid}/{m}/r{r}: hidden={'PASS' if g['hidden_pass'] else 'FAIL'} "
                      f"checks={'ok' if g['repo_checks_pass'] else 'FAIL'} "
                      f"${row['cost_usd'] or 0:.2f} {row['wall_s']}s", flush=True)
            else:
                print(f"  {cid}/{m}/r{r}: ERROR {row['error_class']}", flush=True)
    print(f"results: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
