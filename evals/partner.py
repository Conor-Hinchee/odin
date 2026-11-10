#!/usr/bin/env python3
"""Scripted human for Odin's approval gates.

Watches an Odin states dir for armed `wait-for-human` watchers and answers each
gate through the same path the dashboard uses (`write_wakeup_event`).

    python3 evals/partner.py <odin-states dir> [--script '{"huginn-plan": ["approve_huginn"], ...}']

Each phase maps to a list of decisions, used in order; the last one repeats.
"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "odin" / "scripts"
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("states_dir")
    ap.add_argument("--script", default="")
    ap.add_argument("--notes", default="Approved by the eval partner.")
    ap.add_argument("--log", default="")
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()

    script = json.loads(args.script) if args.script else DEFAULT_SCRIPT
    used: dict = {}
    mod = load_state_module()
    states = Path(args.states_dir)
    answered: set = set()
    deadline = time.time() + args.timeout
    log = open(args.log, "a") if args.log else sys.stdout

    while time.time() < deadline:
        for wf in sorted(states.glob(".odin-watcher-*.json")) if states.exists() else []:
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
            mod.write_wakeup_event(hb["file"], decision, phase=phase, notes=args.notes)
            answered.add(key)
            print(json.dumps({"t": time.strftime("%H:%M:%S"), "phase": phase,
                              "decision": decision, "watcher_pid": hb.get("pid")}), file=log, flush=True)
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
