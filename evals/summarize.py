#!/usr/bin/env python3
"""Summarize an eval run: pass rates with 95% Wilson intervals, cost, time, errors.

    python3 evals/summarize.py evals/results/<run-id> [--markdown out.md]

A trial "passes" when its hidden tests and every repo check pass. Trials in
errors.jsonl never produced a gradable end state; they're counted separately,
never as failures.
"""
import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def passed(row: dict) -> bool:
    g = row.get("grade") or {}
    return bool(g.get("hidden_pass")) and bool(g.get("repo_checks_pass", True))


def fmt_rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({k / n:.0%}, CI {lo:.0%}–{hi:.0%})" if n else "–"


def summarize(run_dir: Path) -> str:
    meta = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    rows = read_jsonl(run_dir / "results.jsonl")
    errors = read_jsonl(run_dir / "errors.jsonl")

    lines = [f"# Eval run {meta.get('run_id', run_dir.name)}", ""]
    lines.append(f"Odin `{meta.get('odin_ref', '?')}` · model `{meta.get('model', '?')}` · "
                 f"{len(rows)} graded trials · {len(errors)} errors")
    lines.append("")

    k = sum(passed(r) for r in rows)
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    lines.append(f"**Overall pass:** {fmt_rate(k, len(rows))}  ")
    if rows and "finished" in rows[0]:
        f = sum(bool(r.get("finished")) for r in rows)
        lines.append(f"**Finished the loop:** {fmt_rate(f, len(rows))}  ")
    if costs:
        total = sum(costs) + sum(e.get("cost_usd") or 0 for e in errors)
        lines.append(f"**Cost:** ${total:.2f} total, ${sum(costs) / len(costs):.2f} mean per graded trial"
                     + (f", ${sum(costs) / k:.2f} per passing trial" if k else ""))
    lines.append("")

    by = defaultdict(list)
    for r in rows:
        by[(r["case"], r["mode"])].append(r)
    err_by = Counter((e.get("case"), e.get("mode")) for e in errors)
    keys = sorted(set(by) | set(err_by))
    lines += ["| Case | Mode | Pass | Hidden | Repo checks | Out of scope | Finished loop | Stalled at gate | Mean $ | Mean s | Errors |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in keys:
        rs = by.get(key, [])
        n = len(rs)
        hidden = sum(bool(r["grade"]["hidden_pass"]) for r in rs)
        checks = sum(bool(r["grade"].get("repo_checks_pass", True)) for r in rs)
        oos = sum(bool(r["grade"].get("out_of_scope")) for r in rs)
        stalled = sum(bool(r.get("stalled_at")) for r in rs)
        finished = sum(bool(r.get("finished")) for r in rs)
        c = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        w = [r["wall_s"] for r in rs]
        lines.append(f"| {key[0]} | {key[1]} | {fmt_rate(sum(passed(r) for r in rs), n)} | {hidden}/{n} | "
                     f"{checks}/{n} | {oos}/{n} | {finished}/{n} | {stalled}/{n} | {(sum(c) / len(c)) if c else 0:.2f} | "
                     f"{(sum(w) / len(w)) if w else 0:.0f} | {err_by.get(key, 0)} |")

    with_protocol = [r for r in rows if (r.get("protocol") or {}).get("checks")]
    if with_protocol:
        clean = sum(bool(r["protocol"]["pass"]) for r in with_protocol)
        lines += ["", "## Protocol (Odin's own rules)", "",
                  f"**Followed every rule:** {fmt_rate(clean, len(with_protocol))}", "",
                  "| Rule | Pass | Fail | n/a |", "|---|---|---|---|"]
        names = list(with_protocol[0]["protocol"]["checks"].keys())
        for name in names:
            st = Counter(r["protocol"]["checks"].get(name, {}).get("status", "n/a") for r in with_protocol)
            lines.append(f"| {name} | {st['pass']} | {st['fail']} | {st['n/a']} |")
        viol = [(r, name) for r in with_protocol for name in r["protocol"]["violations"]]
        if viol:
            lines += ["", "Violations:", ""]
            for r, name in viol:
                lines.append(f"- `{r['case']}/{r['mode']}/r{r['rep']}` {name}: "
                             f"{r['protocol']['checks'][name]['detail'][:160]}")

    judged = read_jsonl(run_dir / "judge.jsonl")
    if judged:
        lines += ["", "## Judge (Claude API, one property per call)", "",
                  "| Property | Pass | Fail | n/a | Judge errors |", "|---|---|---|---|---|"]
        for prop in sorted({j["property"] for j in judged}):
            js = [j for j in judged if j["property"] == prop]
            v = Counter(j.get("verdict") if j.get("status") == "ok" else j.get("status") for j in js)
            lines.append(f"| {prop} | {v['pass']} | {v['fail']} | {v['n/a']} | {v['error']} |")
        spend = sum(j.get("cost_usd") or 0 for j in judged)
        lines += ["", f"Judge cost: ${spend:.2f} (`{judged[0].get('rubric_version', '?')}` rubric). "
                  "Uncalibrated until `judge.py agreement` reaches about 90% against hand labels."]
        fails = [j for j in judged if j.get("verdict") == "fail"]
        if fails:
            lines += ["", "Judge fails:", ""]
            for j in fails:
                lines.append(f"- `{j['case']}/{j['mode']}/r{j['rep']}` {j['property']}: {j.get('evidence', '')[:160]!r}")

    unfinished = [r for r in rows if "finished" in r and not r["finished"]]
    if unfinished:
        lines += ["", "## Didn't finish the loop", ""]
        for r in unfinished:
            lines.append(f"- `{r['case']}/{r['mode']}/r{r['rep']}`: stopped before {r.get('stopped_before') or '?'}"
                         f" — {r['final_text'][-240:].strip()!r}")

    failures = [r for r in rows if not passed(r)]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            g = r["grade"]
            why = []
            if r.get("stalled_at"):
                why.append(f"stalled awaiting human at {', '.join(r['stalled_at'])}")
            if not g["hidden_pass"]:
                why.append(f"hidden: {g['hidden_tail']}")
            for name, v in (g.get("repo_checks") or {}).items():
                if not v["pass"]:
                    why.append(f"{name}: {v['detail']}")
            lines.append(f"- `{r['case']}/{r['mode']}/r{r['rep']}` ({r['trial_dir']}): " + "; ".join(why))
    if errors:
        lines += ["", "## Errors (not scored)", ""]
        for e in errors:
            lines.append(f"- `{e.get('case')}/{e.get('mode')}/r{e.get('rep')}`: {e.get('error_class')}"
                         + (f" — {e['detail']}" if e.get("detail") else ""))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--markdown", default="")
    args = ap.parse_args()
    text = summarize(Path(args.run_dir))
    if args.markdown:
        Path(args.markdown).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
