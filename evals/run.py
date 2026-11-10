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
sys.path.insert(0, str(EVALS))
import protocol  # noqa: E402
REPO_ROOT = EVALS.parent
CASES_DIR = EVALS / "cases"
DEFAULT_MODEL = "claude-sonnet-5-5"
# Trials run outside this repo. Claude Code reads CLAUDE.md from every parent directory, so a sandbox under
# evals/results/ inherited Odin's own repo instructions (Exp1 finding: half the plain transcripts quoted them).
RESULTS_ROOT = Path(os.environ.get("ODIN_EVAL_RESULTS") or Path.home() / ".cache" / "odin-evals" / "results")
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude")

WAIVER_SUFFIX = (
    "\n\nEval run, no human available: I waive plan review, so go straight from Phase 1 to "
    "Phase 2. I also waive the pre-PR gate and sign-off: do not push or open a PR, and stop "
    "after Phase 4 (Hlidskjalf)."
)
PLAIN_SUFFIX = (
    "\n\nWork on a new branch, commit your change, and open a PR with `gh pr create` when you're done."
)
# Keep the user's own ~/.claude settings, plugins, hooks and MCP servers out of every arm.
ISOLATION = ["--setting-sources", "project,local", "--strict-mcp-config"]
# What a user types when Odin stops at a gate and asks in chat for an approval the dashboard already gave.
NUDGE = "Yes, that approval in the dashboard was mine. Go ahead, and go with the defaults you recommended."
MAX_NUDGES = 3
FAKE_REMOTE = "https://github.com/eval-sandbox/shop.git"
GIT_ID = ["-c", "user.name=eval", "-c", "user.email=eval@example.invalid"]
_write_lock = threading.Lock()


def instructions_above(path: Path) -> list:
    """Agent instruction files in any parent directory of a trial (the user's own ~/.claude is handled by
    --setting-sources and is the same for every arm)."""
    found, user_dir = [], Path.home() / ".claude"
    p = path.resolve()
    for d in [p, *p.parents]:
        for name in INSTRUCTION_FILES:
            f = d / name
            if f.exists() and f != user_dir:
                found.append(str(f))
    return found


# A real `gh pr create`, not `gh pr create --help` (Exp1: a help call was scored as an opened PR).
PR_CREATE = re.compile(r"\bgh pr create\b(?!.*(?:\s--help\b|\s-h\b))")


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
        if not PR_CREATE.search(line):
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


def build_cmd(case: dict, mode: str, args) -> list:
    """The claude command line for one trial. `plain` is Claude Code with no plugins at all."""
    if mode == "plain":
        prompt, plugin = case["prompt"] + PLAIN_SUFFIX, []
    else:
        prompt = f"/odin:odin {case['prompt']}" + (WAIVER_SUFFIX if mode == "waiver" else "")
        plugin = ["--plugin-dir", args.odin_dir]
    return [
        "claude", "-p", prompt, *plugin,
        # Turn off the user-installed copy so only --plugin-dir's Odin (or none) loads.
        "--settings", json.dumps({"enabledPlugins": {"odin@odin": False}}),
        *ISOLATION,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", str(case["budget_usd"]),
        "--model", args.model,
    ]


def resume_cmd(cmd: list, session_id: str, message: str) -> list:
    """The same command line, resuming `session_id` with `message` as the next user turn."""
    out = list(cmd)
    out[out.index("-p") + 1] = message
    return out + ["--resume", session_id]


def read_events(transcript: Path) -> list:
    evs = []
    for line in transcript.read_text().splitlines():
        try:
            evs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return evs


def session_id(events: list) -> str:
    return next((e.get("session_id") for e in events if e.get("type") == "system" and e.get("session_id")), "")


PHASE_ORDER = ["huginn-plan", "thor-execution", "heimdall-validation", "hlidskjalf-judge", "human-validation"]


def stalled_gate(mode: str, out: Path) -> str:
    """Where a gated `claude -p` stopped early: it exited before sign-off with no watcher armed.

    Then Odin is waiting on chat, whatever the state file says. (Exp1: the dashboard's approval had already
    moved the plan to done and Phase 2 to active, so the state file showed no awaiting_human at all.)
    """
    if mode != "gated" or armed_watchers(out):
        return ""
    phases = final_phases(out)
    if phases.get("human-validation") == "done":
        return ""
    waiting = [k for k in PHASE_ORDER if phases.get(k) == "awaiting_human"]
    return waiting[0] if waiting else next((k for k in PHASE_ORDER if phases.get(k) != "done"), "unknown")


def totals(results: list) -> dict:
    """Totals over every result event in a trial.

    `claude -p` emits a result each time the agent ends a turn (every gate in a gated run, and every resume).
    `total_cost_usd` is cumulative for the session, including across `--resume`, so the trial's cost is the last
    one. `num_turns` and `usage` are per turn, so they add up. (Measured on 2.1.310: 0.135 → 0.205 → 0.260 → 0.288
    within one process; 0.0584 → 0.0629 across a one-word resume.)
    """
    usage = {}
    for r in results:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
    costs = [r.get("total_cost_usd") for r in results if r.get("total_cost_usd") is not None]
    return {
        "cost_usd": round(costs[-1], 6) if costs else None,
        "num_turns": sum(r.get("num_turns") or 0 for r in results) if results else None,
        "models_served": sorted({m for r in results for m in (r.get("modelUsage") or {})}),
        "usage": usage,
    }


def opened_pr(out: Path) -> bool:
    log = out / "gh.log"
    return log.exists() and any(PR_CREATE.search(l) for l in log.read_text().splitlines())


def loop_status(mode: str, phases: dict, out: Path) -> dict:
    """finished / stopped_before / stalled_at. Plain Claude Code has no phases: it finished if it opened a PR."""
    if mode == "plain":
        done = opened_pr(out)
        return {"stalled_at": [], "finished": done, "stopped_before": "" if done else "pr"}
    order = ["huginn-plan", "thor-execution", "heimdall-validation", "hlidskjalf-judge", "human-validation"]
    last = "human-validation" if mode == "gated" else "hlidskjalf-judge"
    finished = phases.get(last) == "done"
    return {
        # The run ended while Odin was still waiting on a human: it stopped instead of finishing.
        "stalled_at": sorted(k for k, v in phases.items() if v == "awaiting_human"),
        "finished": finished,
        "stopped_before": "" if finished else next((ph for ph in order if phases.get(ph) != "done"), ""),
    }


def drive(cmd: list, repo: Path, env: dict, out: Path, transcript: Path, started: float, args) -> tuple:
    """Run one `claude -p` invocation, appending to the transcript, with the hard and idle timeouts."""
    status, rc = "completed", None
    with open(transcript, "a") as tf, open(out / "stderr.log", "a") as ef:
        proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=tf, stderr=ef, text=True)
        last_size, last_change = transcript.stat().st_size, time.time()
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
    return status, rc


def run_trial(case: dict, mode: str, rep: int, args, run_dir: Path, resume: dict | None = None) -> tuple:
    """One trial. With `resume` (its earlier results row), continue a stalled session in place with a nudge."""
    out = run_dir / "trials" / case["id"] / mode / f"r{rep}"
    repo = out / "repo" if resume else make_sandbox(case, out)

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
    cmd = build_cmd(case, mode, args)

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
    status, rc, nudges = "completed", None, []
    prior_wall = 0.0
    if resume:
        prior_wall = resume.get("wall_s") or 0.0
        nudges = list(resume.get("nudges") or [])
        sid = session_id(read_events(transcript))
        nudges.append({"gate": stalled_gate(mode, out) or "unknown", "t": prior_wall, "late": True})
        cmd = resume_cmd(build_cmd(case, mode, args), sid, NUDGE)
    else:
        transcript.write_text("")
    while True:
        status, rc = drive(cmd, repo, env, out, transcript, started, args)
        if status != "completed" or len(nudges) >= MAX_NUDGES:
            break
        gate = stalled_gate(mode, out)
        sid = session_id(read_events(transcript))
        if not gate or not sid:
            break
        # Odin stopped at a gate and asked in chat. Answer the way a user would, and count it.
        nudges.append({"gate": gate, "t": round(time.time() - started, 1)})
        cmd = resume_cmd(build_cmd(case, mode, args), sid, NUDGE)
    for p in (partner, server):
        if p:
            p.terminate()
    wall = round(prior_wall + time.time() - started, 1)

    events = read_events(transcript)
    results = [e for e in events if e.get("type") == "result"]
    result_event = results[-1] if results else {}
    t = totals(results)
    served = t["models_served"]
    usage = t["usage"]
    row = {
        "case": case["id"], "mode": mode, "rep": rep,
        "model_requested": args.model, "models_served": served,
        "status": status, "exit_code": rc, "wall_s": wall,
        "cost_usd": t["cost_usd"],
        "num_turns": t["num_turns"],
        "invocations": 1 + len(nudges),
        "turn_ends": len(results),
        "nudges": nudges,
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
    row.update(loop_status(mode, row["phases"], out))
    row["opened_pr"] = opened_pr(out)
    if mode == "plain":  # Odin's own rules don't apply to plain Claude Code
        row["protocol"] = {"violations": [], "pass": None, "checks": {}}
        return "result", row
    try:
        row["protocol"] = protocol.check_trial(out, case, CASES_DIR / case["id"])
    except Exception as exc:  # a checker bug must not drop the trial
        row["protocol"] = {"error": repr(exc), "violations": [], "pass": None, "checks": {}}
    return "result", row


def append(path: Path, row: dict) -> None:
    with _write_lock, open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def resume_stalled(args) -> int:
    """Continue stalled sessions where they stopped (what the nudge would have done), not fresh re-runs."""
    run_dir = RESULTS_ROOT / args.resume_stalled
    path = run_dir / "results.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    todo = [i for i, r in enumerate(rows)
            if r["mode"] == "gated" and stalled_gate("gated", run_dir / r["trial_dir"])]
    print(f"resume {args.resume_stalled}: {len(todo)} stalled trials", flush=True)
    shutil.copy(path, path.with_suffix(".jsonl.pre-resume"))
    errors = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(run_trial, load_case(rows[i]["case"]), "gated", rows[i]["rep"], args, run_dir, rows[i]): i
                for i in todo}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                kind, row = fut.result()
            except Exception as exc:
                kind, row = "error", {**rows[i], "error_class": "harness_error", "detail": repr(exc)}
            if kind == "result":
                rows[i] = row
                g = row["grade"]
                print(f"  {row['case']}/r{row['rep']}: hidden={'PASS' if g['hidden_pass'] else 'FAIL'} "
                      f"nudges={len(row['nudges'])} finished={row['finished']} ${row['cost_usd'] or 0:.2f}", flush=True)
            else:  # an unfinished resume is an error, never a graded fail
                errors.append(row)
                print(f"  {rows[i]['case']}/r{rows[i]['rep']}: ERROR {row.get('error_class')}", flush=True)
                rows[i] = None
    path.write_text("".join(json.dumps(r) + "\n" for r in rows if r is not None))
    for e in errors:
        append(run_dir / "errors.jsonl", e)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="all", help="comma-separated case ids, or 'all'")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--mode", choices=["case", "waiver", "gated", "plain"], default="case",
                    help="'case' uses each case's own mode; 'plain' is Claude Code with no plugins")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--odin-dir", default=str(REPO_ROOT))
    ap.add_argument("--run-id", default="")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=1500, help="hard wall-clock ceiling per trial (s)")
    ap.add_argument("--idle-timeout", type=int, default=300, help="kill a trial with no transcript output for this long (s)")
    ap.add_argument("--resume-stalled", default="", metavar="RUN_ID",
                    help="nudge every gated trial in RUN_ID that stopped before sign-off, in place, and re-grade it")
    args = ap.parse_args()
    args.odin_dir = str(Path(args.odin_dir).resolve())
    if args.resume_stalled:
        return resume_stalled(args)

    ids = all_case_ids() if args.cases == "all" else [c.strip() for c in args.cases.split(",") if c.strip()]
    cases = [load_case(i) for i in ids]
    run_id = args.run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    leaks = instructions_above(run_dir)
    if leaks:
        print("refusing to run: agent instructions in a parent of the trial dirs would load into every trial:\n  "
              + "\n  ".join(leaks) + "\nSet ODIN_EVAL_RESULTS to a directory outside any project.", file=sys.stderr)
        return 2
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
