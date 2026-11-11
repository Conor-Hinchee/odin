#!/usr/bin/env python3
"""How many known harness failures does Odin's retrospective (Hlidskjalf) catch? Free: reads finished trials.

    python3 evals/lookback.py ~/.cache/odin-evals/results/exp1b-odin [more runs...] [--markdown out.md]

Ground truth per trial, from the runner and the protocol checker, never from the state file:
- missing-pr: the trial never opened a PR
- gate-nudged: the runner had to nudge it at a gate before the retrospective (a nudge at sign-off
  comes after the retrospective, so it can't be expected to see it). The scan catches this two ways:
  gate-ignored (it got the decision and didn't act) and gate-unwatched (it paused with no watcher)
- state-file-late: the protocol checker's state_file_first violation

Two ways of catching, scored separately:
- written: a harness delta or waiver the retrospective actually wrote mentions the failure (keyword match)
- surfaced: odin_friction.scan() reports it from the trial's state file and transcript, cut where the
  retrospective started
"""
import argparse
import json
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS.parent / "skills" / "odin" / "scripts"))
import odin_friction as F  # noqa: E402

TYPES = ["missing-pr", "gate-nudged", "state-file-late"]
DETECTS = {"missing-pr": {"missing-pr"}, "gate-nudged": {"gate-ignored", "gate-unwatched"},
           "state-file-late": {"state-file-late"}}
KEYWORDS = {  # what a written delta or waiver about each failure would say
    "missing-pr": ("pull request", " pr ", "pr gate", "pre-pr", "remote", "gh pr", "missing-pr"),
    "gate-nudged": ("dashboard", "wakeup", "not user input", "approval", "re-confirm", "reconfirm", "watcher",
                    "gate-ignored", "gate-unwatched"),
    "state-file-late": ("state file first", "before the state file", "bootstrap", "ground rule 1", "state-file-late"),
}


def opened_pr(row: dict, trial: Path) -> bool:
    """The runner's opened_pr; older runs predate it, so fall back to the fake gh's log."""
    if row.get("opened_pr") is not None:
        return bool(row["opened_pr"])
    log = trial / "gh.log"
    return log.exists() and any("pr create" in l and "--help" not in l and "--dry-run" not in l for l in log.read_text().splitlines())


def retro_started(row: dict) -> bool:
    """Only trials whose retrospective ran can be expected to have looked back."""
    return (row.get("phases") or {}).get("hlidskjalf-judge") in ("active", "done")


def expected(row: dict, trial: Path | None = None) -> set:
    out = set()
    if not (opened_pr(row, trial) if trial else row.get("opened_pr")):
        out.add("missing-pr")
    if any(n.get("gate") != "human-validation" for n in row.get("nudges") or []):
        out.add("gate-nudged")
    if "state_file_first" in ((row.get("protocol") or {}).get("violations") or []):
        out.add("state-file-late")
    return out


def state_file(trial: Path) -> Path | None:
    files = [p for p in trial.rglob("odin-states/*.html") if p.name != "index.html" and "grade" not in p.parts]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def written(content: str) -> set:
    import importlib.util
    spec = importlib.util.spec_from_file_location("odin_state", EVALS.parent / "skills/odin/scripts/odin-state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ledger = mod.extract_ledger(content)
    texts = [json.dumps(d) for d in mod._collect_all_deltas(ledger)]
    texts += [str(ledger.get("incident_waiver") or "")]
    texts += [str(it.get("incident_waiver") or "") for it in ledger.get("iterations", [])]
    blob = " ".join(texts).lower().replace("\n", " ")
    return {t for t in TYPES if any(k in f" {blob} " for k in KEYWORDS[t])}


def score(run_dirs: list) -> dict:
    rows_out, skipped = [], []
    for run in run_dirs:
        run = Path(run)
        for line in (run / "results.jsonl").read_text().splitlines():
            row = json.loads(line)
            if row.get("mode") != "gated":
                continue
            trial = run / row["trial_dir"]
            if not retro_started(row):
                skipped.append(f"{run.name}/{row['case']}/r{row['rep']}")
                continue
            sf = state_file(trial)
            content = sf.read_text(errors="replace") if sf else ""
            found = F.scan(content, sf, trial / "transcript.jsonl") if sf else []
            rows_out.append({"trial": f"{run.name}/{row['case']}/r{row['rep']}", "expected": sorted(expected(row, trial)),
                             "written": sorted(written(content)) if sf else [],
                             "surfaced": sorted({t for t in TYPES for f in found if f["id"] in DETECTS[t]}),
                             "findings": sorted(f["id"] for f in found), "evidence": found})
    return {"trials": rows_out, "skipped": skipped}


def table(res: dict) -> str:
    rows = res["trials"]
    out = [f"# Lookback recall ({len(rows)} trials)", "",
           "| Failure | Known | Written by the retrospective | Surfaced by friction-scan | False positives (scan) |",
           "|---|---|---|---|---|"]
    for t in TYPES + ["all"]:
        types = TYPES if t == "all" else [t]
        known = sum(1 for r in rows for x in types if x in r["expected"])
        wr = sum(1 for r in rows for x in types if x in r["expected"] and x in r["written"])
        su = sum(1 for r in rows for x in types if x in r["expected"] and x in r["surfaced"])
        fp = sum(1 for r in rows for x in types if x not in r["expected"] and x in r["surfaced"])
        label = f"**{t}**" if t == "all" else f"`{t}`"
        out.append(f"| {label} | {known} | {wr}/{known} | {su}/{known} | {fp} |")
    if res.get("skipped"):
        out += ["", f"Not scored (the retrospective never started): {', '.join(res['skipped'])}"]
    out += ["", "## Misses and false positives", ""]
    for r in rows:
        miss = [x for x in r["expected"] if x not in r["surfaced"]]
        extra = [x for x in r["surfaced"] if x not in r["expected"]]
        if miss or extra:
            out.append(f"- `{r['trial']}`: missed {miss or '–'}, extra {extra or '–'}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--markdown", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    res = score(args.runs)
    text = table(res)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
