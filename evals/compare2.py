#!/usr/bin/env python3
"""Experiment 2: Odin vs. Claude Code plan mode vs. plain, on trap cases. Free: reads results only.

    python3 evals/compare2.py <odin-run> <planmode-run> <plain-run> [--markdown out.md]

Written before any paid run, with the metrics pre-registered in EXPERIMENT-2.md:
- wrong_built: the first code the reviewer saw (Odin's pre-PR gate, the others' first PR) failed the hidden tests
- caught_at_plan: the plan review sent the plan back at least once
- final_right: the graded result passes the hidden tests (after any corrections)
- corrections: how many times the human had to correct it
- cost and time to the final result, resumed turns included
"""
import argparse
import itertools
import statistics
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS))
from compare import bootstrap_ratio, fisher_exact  # noqa: E402
from summarize import fmt_rate, passed, read_jsonl  # noqa: E402


def metrics(row: dict) -> dict:
    cps = row.get("checkpoints") or []
    plans = [c for c in cps if c.get("kind") == "plan"]
    prs = [c for c in cps if c.get("kind") == "pr" and "hidden_pass" in c]
    first_pr = prs[0]["hidden_pass"] if prs else None
    final = passed(row)
    return {
        # No checkpoint saw code (it stopped early): fall back to the final grade.
        "wrong_built": (not first_pr) if first_pr is not None else not final,
        "caught_at_plan": any(c.get("correction") for c in plans),
        "first_plan_ok": (plans[0].get("verdict") == "approve") if plans else None,
        "final_right": final,
        "corrections": sum(1 for c in cps if c.get("correction")),
        "capped": any(c.get("capped") for c in cps),
        "reviewer_errors": sum(1 for c in cps if c.get("error")),
        "cost": row.get("cost_usd") or 0.0,
        "wall": row.get("wall_s") or 0.0,
    }


def load(run_dir: Path) -> dict:
    rows = read_jsonl(run_dir / "results.jsonl")
    return {"dir": run_dir, "rows": rows, "m": [metrics(r) for r in rows],
            "errors": read_jsonl(run_dir / "errors.jsonl"),
            "mode": (rows[0]["mode"] if rows else "?")}


ARM = {"gated": "odin", "planmode": "planmode", "plain": "plain"}


def count(arm, key):
    vals = [m[key] for m in arm["m"] if m[key] is not None]
    return sum(bool(v) for v in vals), len(vals)


def compare(arms: list) -> str:
    names = [ARM.get(a["mode"], a["mode"]) for a in arms]
    out = [f"# Experiment 2: {' vs. '.join(names)}", ""]
    out += ["| | " + " | ".join(names) + " |", "|---|" + "---|" * len(arms)]
    for label, key in (("H2 wrong thing built (first code reviewed)", "wrong_built"),
                       ("Plan sent back at least once", "caught_at_plan"),
                       ("First plan approved", "first_plan_ok"),
                       ("H1 right in the end", "final_right"),
                       ("Hit the correction cap", "capped")):
        out.append(f"| {label} | " + " | ".join(fmt_rate(*count(a, key)) for a in arms) + " |")
    for label, key in (("H5 corrections per trial (mean)", "corrections"),):
        out.append(f"| {label} | " + " | ".join(
            f"{statistics.mean(m[key] for m in a['m']):.2f}" if a["m"] else "–" for a in arms) + " |")
    for label, key, unit in (("Median cost", "cost", "$"), ("Median time", "wall", "s")):
        cells = []
        for a in arms:
            xs = [m[key] for m in a["m"]]
            cells.append((f"${statistics.median(xs):.2f}" if unit == "$" else f"{statistics.median(xs):.0f} s") if xs else "–")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append(f"| Harness errors | " + " | ".join(str(len(a["errors"])) for a in arms) + " |")
    out.append(f"| Reviewer errors | " + " | ".join(str(sum(m["reviewer_errors"] for m in a["m"])) for a in arms) + " |")

    out += ["", "## Pairwise", "", "| Pair | H2 wrong built: Fisher p | H1 right in the end: Fisher p | H3 cost ratio (95% CI) | Time ratio (95% CI) |",
            "|---|---|---|---|---|"]
    for (i, a), (j, b) in itertools.combinations(enumerate(arms), 2):
        cells = []
        for key in ("wrong_built", "final_right"):
            ka, na = count(a, key)
            kb, nb = count(b, key)
            cells.append(f"{fisher_exact(ka, na - ka, kb, nb - kb):.3f}" if na and nb else "–")
        for key in ("cost", "wall"):
            r, lo, hi = bootstrap_ratio([m[key] for m in a["m"]], [m[key] for m in b["m"]])
            cells.append(f"{r:.1f}× ({lo:.1f}–{hi:.1f})" if r else "–")
        out.append(f"| {names[i]} / {names[j]} | " + " | ".join(cells) + " |")

    out += ["", "## Per case (wrong built / right in the end)", "", "| Case | " + " | ".join(names) + " |",
            "|---|" + "---|" * len(arms)]
    cases = sorted({r["case"] for a in arms for r in a["rows"]})
    for c in cases:
        cells = []
        for a in arms:
            ms = [m for r, m in zip(a["rows"], a["m"]) if r["case"] == c]
            cells.append(f"{sum(m['wrong_built'] for m in ms)}/{len(ms)} · {sum(m['final_right'] for m in ms)}/{len(ms)}")
        out.append(f"| `{c}` | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--markdown", default="")
    args = ap.parse_args()
    text = compare([load(Path(r)) for r in args.runs])
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
