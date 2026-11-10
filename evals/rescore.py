#!/usr/bin/env python3
"""Re-score an existing run without calling a model.

Recomputes the outcome grade, loop completion and protocol checks for every row in
results.jsonl from the trial directories on disk, so checker and grader fixes
apply to old runs for free. The original file is kept as results.jsonl.bak.

    python3 evals/rescore.py evals/results/<run-id>
"""
import json
import shutil
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS))
import protocol  # noqa: E402
import run  # noqa: E402



def rescore_row(row: dict, run_dir: Path) -> dict:
    out = run_dir / row["trial_dir"]
    case = run.load_case(row["case"])
    row["grade"] = run.grade(case, out / "repo", out)
    row["phases"] = run.final_phases(out)
    row.update(run.loop_status(row["mode"], row["phases"], out))
    row["opened_pr"] = run.opened_pr(out)
    row["protocol"] = ({"violations": [], "pass": None, "checks": {}} if row["mode"] in ("plain", "planmode")
                       else protocol.check_trial(out, case, run.CASES_DIR / row["case"]))
    return row


def main() -> int:
    run_dir = Path(sys.argv[1]).resolve()
    path = run_dir / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    shutil.copy(path, path.with_suffix(".jsonl.bak"))
    rows = [rescore_row(r, run_dir) for r in rows]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"re-scored {len(rows)} rows in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
