# Odin eval harness: plan

Status: milestones 0–3 done, plus the outcome grader, the protocol checker and the LLM judge (built and unit-tested offline; not yet run against the API or calibrated). Next: self-test and calibrate the judge, then the first experiment. See [SPIKE.md](SPIKE.md) and [README.md](README.md). Branch `feat/eval-harness` (worktree `../odin-evals`).

## The question this answers

The README promises two things that nothing currently measures:

1. **The loop helps.** Plan, build, verify, reflect should finish small dev tasks more reliably than plain Claude Code.
2. **The harness gets better over time.** Approved Mímir deltas should make later runs better, not just different.

The harness turns both into numbers: run a fixed set of tasks through Odin, grade what each run leaves behind, and compare variants (SKILL.md before and after a change, Odin vs. no plugin, model A vs. model B).

What exists today and why it isn't this:

- `tests/` (about 1,800 lines) is unit tests for the state, server and hook code. It never runs an agent.
- Phase 4 (`hlidskjalf-judge`) writes a retrospective in prose. It has no score, no rubric and no comparison across runs.
- Odin makes no direct LLM API calls. The judge in this harness is the first one.

## Shape

```
evals/
  PLAN.md                  this file
  README.md                how to run it, cost, how to read results
  cases/<case-id>/
    case.json              prompt, tags, mode, budget, expected behaviours
    repo/                  fixture repo, copied fresh per trial
    hidden/                hidden tests + grader assets, never copied into the sandbox
  run.py                   runner: sandbox -> headless Odin -> collect artifacts
  partner.py               scripted human for the approval gates (milestone 5)
  grade/
    outcome.py             hidden tests, diff scope, end state
    protocol.py            Odin ground-rule compliance, from transcript + state file
    judge.py               Claude API rubric judge (structured output)
    rubrics/*.md
  report.py                results.jsonl -> report.html with CIs and per-case rows
  tests/                   unit tests for the graders (oracle and null runs)
  results/<run-id>/        gitignored: results.jsonl, errors.jsonl, transcripts, state files
```

No new dependencies for the plugin itself. Only `evals/judge.py` needs `anthropic`, installed in a venv under `evals/`.

## 1. Cases

8 to 12 synthetic fixture repos, small enough that a run costs a dollar or two. All invented. Per `CLAUDE.md`, nothing comes from `odin-states/` or any work codebase; past tasks may suggest a *pattern*, never code or names.

Each case targets one Odin behaviour, and the set covers both directions (should pause / should not, should split / should not):

| Case | Tests |
|---|---|
| `bugfix-single-file` | Baseline: plan → fix → verify on a one-file bug with a hidden regression test |
| `feature-parallel` | Two independent modules. Does Huginn propose disjoint Valkyrie ownership, and do Valkyries stay in their files and never commit? |
| `feature-serial` | Looks splittable but isn't (shared file). Does Huginn correctly *not* fan out? |
| `dirty-checkout` | Pre-existing uncommitted changes "from another session". Ground rule 9: move to a worktree, never stage them |
| `flaky-baseline` | A pre-existing flaky test fails once. Incident-to-delta gate: does Hlidskjalf record a delta or a waiver? |
| `ambiguous-request` | Under-specified ask. Does Huginn list open questions instead of guessing? |
| `destructive-delete` | "Remove the old X". Plan review must not be skipped (deletion is never trivial) |
| `waived-review` | Prompt explicitly waives plan review. Odin should *not* pause (the other direction of the case above) |
| `wrong-first-attempt` | Hidden test only passes after a revision; the scripted partner rejects at sign-off once. Tests `step-back` and iteration history |

Prompts state symptoms the way a user would ("checkout total is wrong when a coupon is applied"), not the failing test name or file.

## 2. Runner (`run.py`)

Per (case, variant, rep):

1. Copy `cases/<id>/repo` to a temp dir, `git init`, commit, add a local bare repo as `origin` so push works. Put a fake `gh` shim first on `PATH` that records `gh pr create` and prints a fake URL, so the pre-PR gate can complete without GitHub.
2. Give the trial its own `XDG_CONFIG_HOME` and Odin root, so it never touches the live `:8788` server, real config or real `odin-states/` (the test suite already isolates XDG config this way).
3. Run headless:
   `claude -p "/odin:odin <prompt>" --plugin-dir <variant's odin checkout> --output-format stream-json --permission-mode bypassPermissions --max-budget-usd <case budget> --model <variant model>`
4. Hard wall-clock ceiling per trial (independent of stream liveness). On timeout, budget cap, API error or crash, write the row to `errors.jsonl` with a failure class, not to `results.jsonl`.
5. Collect into `results/<run-id>/<case>/<variant>/<rep>/`: the stream-json transcript, the final state file, `git log`, the final diff, `gh` shim log, cost and tokens from the result event, and the served model (assert it matches what was asked for).
6. Then copy `hidden/` in and grade.

Variants are just `{odin_ref, model, plugin: on|off}`, so "SKILL.md at commit A vs commit B" is a worktree per ref. The `plugin: off` variant runs the same prompt in plain Claude Code. That's the ablation that shows whether the loop itself is doing anything.

## 3. The human gates

Odin pauses three times for a human. Headless runs need an answer at each one.

- **Milestones 1–4: waiver mode.** The prompt waives plan review and asks Odin to stop after Phase 4. Cheapest path, but it never exercises the gates.
- **Milestone 5: scripted partner (`partner.py`).** It watches the trial's `odin-states/` for `.odin-watcher-<slug>.json` (written by `wait-for-human` with the phase it's waiting on) and replies through the same path the dashboard uses: `write_wakeup_event()` in `odin-state.py` with `approve_huginn`, `approve_pr`, `approve_signoff`, or a scripted `reject_signoff` with notes for `wrong-first-attempt`. Each case's decisions live in `case.json`.

**Spike result:** `claude -p` stays alive while the watcher runs and resumes when it exits, so the scripted partner works as-is and no stream-json fallback is needed. See SPIKE.md.

## 4. Grading

Three layers, recorded as separate metrics, never blended into one score.

**Outcome (deterministic, the headline).** From the end state, not the transcript:
- hidden tests pass (pass/fail)
- the diff stays inside the files the plan named (scope creep count)
- iterations used, incidents logged, failed checks
- PR opened through the shim (when the mode expects one)

**Protocol compliance (deterministic, Odin-specific).** Parsed from the stream-json transcript (reuse `odin_transcript.py` where it fits) and the final state file:
- state file created before the first read of project source (ground rule 1)
- phase statuses moved in order with no batched updates at the end (rule 5)
- paused at `awaiting_human` when it should and not when review was waived (rule 7, both directions)
- no `git add -A` / `git add .`; never staged changes it didn't make (rule 9)
- Valkyries edited only their files and never committed
- Hlidskjalf recorded a delta or waiver whenever an incident or failed check was logged

**Judge (Claude API, for what a check can't see).** `evals/judge.py`, `anthropic` Python SDK, `claude-opus-5-5`, structured output via `client.messages.parse()` against a Pydantic schema, server-side `fallbacks` enabled. One call per property, not one holistic score:
- *Plan:* are the test commands real and runnable here? Is the Definition of Done checkable? Is the parallelism call right for this repo?
- *Delta:* does `--target-path` point at a real source file? Is the failure pattern general (no task names or code)? Would the proposed change have prevented this miss?
- *Diff review:* do the rationale and per-file notes match what the diff actually does?

Judge hygiene: the rubric is fixed and cached with prompt caching (stable system prompt first, the artifact last); candidate text is treated as untrusted data; the judge never sees which variant produced the artifact. Because the model under test is also Claude, self-preference is a real risk, so the judge only grades properties with a checkable answer, never "which run is better". Calibrate on about 30 plans and deltas labelled by hand; don't let a judge metric steer decisions below about 90% agreement. Calls are synchronous, not batched: the Batches API rejects server-side `fallbacks`, and with results cached by input hash a re-grade only pays for new inputs anyway.

*As built (`evals/judge.py`):* five properties, `plan_test_commands`, `plan_definition_of_done`, `plan_covers_request`, `plan_parallelism_call` and `review_matches_diff`. The review artifact is the Heimdall card text plus the PR description the agent wrote (from the fake `gh` log), judged against the real `git diff`. Delta properties wait until a run produces a harness delta (none of the smoke runs did).

**Before the first paid run:** an oracle (apply the reference fix, all graders should pass) and a null (empty diff, graders should fail), plus three known-bad inputs for the judge (empty, "I don't know", a confident answer to the wrong question). These go in `evals/tests/` and run with the normal `python3 -m unittest discover`.

## 5. Reporting

`report.py` writes `report.html`: per-variant pass rate with 95% CIs, cost per *completed* task, wall-clock, protocol-violation counts, judge scores, and per-case rows linking to each transcript and state file.

Be honest about resolution. 10 cases × 3 reps gives a noise floor of about ±18 points on pass rate. So the headline comparisons are **paired** (same case, same rep index, two variants), with continuous metrics (cost, iterations, violations) next to pass rate. A small difference gets reported as "no measurable change", not as a win.

## 6. Experiments for the write-up

1. **Odin vs. plain Claude Code** on the same cases: pass rate, cost, iterations. Does the loop earn its overhead?
2. **Does Mímir work?** Pick one approved delta that changed SKILL.md (from git history), and run the commit before vs. the commit after.
3. **Model sweep** (optional): same Odin ref on Opus 5.5 vs. Sonnet 5.5, to show cost against quality.

## Milestones

| # | Deliverable | Done when |
|---|---|---|
| 0 | Headless spike | One `bugfix-single-file` run completes in a sandbox; gate behaviour in print mode is known and written down |
| 1 | Runner + outcome grader + 2 cases | `run.py` produces `results.jsonl` / `errors.jsonl`; oracle passes, null fails |
| 2 | Protocol grader | All rule checks implemented with unit tests on recorded transcripts |
| 3 | Full case set | 8–12 cases, each with an oracle that passes |
| 4 | Judge | Rubrics, structured output, calibration set with agreement reported |
| 5 | Scripted partner | Gates exercised end to end, including one reject → `step-back` |
| 6 | Baseline + experiments | `report.html` for experiments 1 and 2 |
| 7 | Write-up | README section + a post (what was measured, what moved, what didn't, what the eval can't tell you) |

## Cost

Measured on the spike: $0.17–0.34 per trial on Sonnet 5.5. Experiments 1 and 2 at 10 cases × 3 reps × 2 variants is about 120 trials, roughly $20–40 on Sonnet (re-measure on Opus), plus a few dollars of judge calls. Every paid run is triggered by hand and capped with `--max-budget-usd` per trial. Nothing paid runs in CI.

## Open questions

- Waiver mode only, or is the scripted partner (milestone 5) worth it for the first write-up?
- Where the write-up goes: the Odin README, the personal site, or both.
- Budget ceiling for the baseline runs.
