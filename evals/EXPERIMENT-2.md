# Experiment 2: catching the wrong plan

**Question:** when a task's obvious reading is wrong, does reviewing the plan before execution stop the wrong thing being built? And does that early catch pay back Odin's overhead, compared with Claude Code's own plan mode and with catching the problem late, at PR review?

Experiment 1 couldn't answer this. Its partner approved every gate, and every case had one right reading. So the gates never had anything to catch.

**Status: stopped at pre-flight, with a negative result (see [Result](#result)).** The full run was never made, because the pre-flight showed it would have nothing to measure.

Written before any paid run, so the hypotheses can't be fitted to the results afterwards. The analysis script, `compare2.py`, was written and tested before any data existed.

## Cases

Three trap cases. Each is a short request whose obvious implementation passes the visible tests but isn't what the human meant. The case's `review.intent` says what they meant. The agent never sees it.

| Case | Request | Obvious reading | What the human meant | Clue in the repo? |
|---|---|---|---|---|
| `logout-cache` | "Clear the cache when a user logs out." | `cache.clear()` | Remove only that user's `user:<id>:` entries. Other users and the shared product cache survive | Weak: the cache's docstring says it's shared by every user |
| `public-rename` | "Rename `total` to `subtotal` everywhere." | Rename the JSON key too | Rename in code, keep the `"total"` key in the partner webhook payload | Yes: the README says partners receive `payload()` |
| `report-timezone` | "Add a `todays_report(orders, now=None)`…" | UTC dates | The store's calendar day, using `STORE_TZ` from `shop/config.py` | Yes: `STORE_TZ` exists and the README says timestamps are UTC |

**Self-tests (free, in `test_cases.py`):** for each case, the oracle passes the hidden tests, doing nothing fails them, and the obvious wrong fix (`wrong/`) **passes the visible tests and fails the hidden ones**. So every trap is real: the obvious fix looks done and is wrong.

## Arms

Same model (`claude-sonnet-5-5`), cases, sandbox, fake `gh`, grader, isolation flags and $3 budget cap per trial.

| | `gated` (Odin) | `planmode` (Claude Code's own plan mode) | `plain` |
|---|---|---|---|
| Prompt | `/odin:odin <request>` | `<request>` + "work on a branch, commit, open a PR" | Same as planmode |
| Plan review | At Odin's plan gate, through the dashboard API (`revise_huginn` with the correction) | Turn 1 runs with `--permission-mode plan`, which writes a plan and stops. If the reviewer sends it back, a resume *in plan mode* revises it. Once approved, a resume in normal mode builds it | None |
| Code review | At Odin's pre-PR gate: the hidden tests run on the worktree (`reject_pr` with the correction) | After it ends: the hidden tests run on the PR head, and a wrong one gets the correction plus "fix it on the same branch and update the PR" | Same as planmode |
| Chat nudge | As in Experiment 1, at most 3 | n/a | n/a |

**Pre-flight finding (free probe, about $0.12):** headless plan mode never calls ExitPlanMode. It writes its plan to `~/.claude/plans/<slug>.md` and ends the turn without editing anything. A `--resume` with `--permission-mode bypassPermissions` then builds the plan. The runner reads the plan from that Write call, falls back to the final message, and deletes the trial's plan file afterwards, so it doesn't pile up in the user's home directory.

## The reviewing partner (`reviewer.py`)

A human who knows what they meant, reviewing at every checkpoint the arm has.

- **Plan review: a model.** `claude -p` on `claude-sonnet-5-5` with tools turned off and structured output (`--json-schema`: `approve` or `revise`, plus one sentence of reason). It sees the request, the intent and the plan text, and nothing about the arm. It approves when the plan does the right thing in the part the intent is about, and sends it back when the plan does the wrong thing there, or is silent in a way the obvious implementation would get wrong. The model only decides. The correction is fixed text per case, so every arm gets the same words.
- **Code review: deterministic.** The human "tries it": the case's hidden tests run on the code under review.
- **At most 2 corrections per trial.** After that the reviewer approves (it's capped, and that's counted), so a trial can't loop.
- **The reviewer is part of the measurement, so it's audited.** I'll check every plan verdict by hand against the plan text, and report agreement. A reviewer error approves and is logged, never counted as a catch.
- **Pre-flight check (about $0.03):** on `logout-cache`, a `cache.clear()` plan → `revise`; a per-user-prefix plan → `approve`.

## Metrics (`compare2.py`)

| Metric | Definition |
|---|---|
| **wrong_built** | The first code the reviewer saw failed the hidden tests: Odin's first pre-PR gate, or the others' first PR. A trial that never reached code review falls back to its final grade |
| **caught_at_plan** | The plan review sent the plan back at least once (Odin, planmode) |
| **first_plan_ok** | The reviewer approved the first plan: the agent read the request right unprompted |
| **final_right** | The graded result passes the hidden tests, after any corrections |
| **corrections** | How many times the human had to correct it |
| **cost, time** | To the final result, resumed turns and nudges included. The reviewer's own model calls are reported separately |

## Hypotheses (pre-registered)

3 cases × 3 reps × 3 arms = **27 trials**, so 9 per arm. Only large differences can show up: 0/9 against 6/9 is p ≈ 0.009, and 2/9 against 5/9 is p ≈ 0.35.

| # | Hypothesis | Called if |
|---|---|---|
| **H1** | Every arm ends up right once corrections are allowed | Fisher pairwise p > 0.05 → "no difference detected". Expect a near-ceiling |
| **H2** | The wrong thing gets built less often with a plan review: Odin < plain and planmode < plain | Fisher p < 0.05 for that pair. **This is the main hypothesis** |
| **H3** | Catching it early narrows Odin's cost gap: Odin / plain is below Experiment 1's 4.0× | Ratio with bootstrap 95% CI; supported if the CI's upper end is below 4.0 |
| **H4** | Odin's gates add nothing over plan mode at catching the problem, and plan mode is cheaper | wrong_built: Fisher p > 0.05 for Odin vs. planmode. Cost: ratio with CI. If Odin catches more, H4 is rejected, and that's the interesting outcome |
| **H5** | Plain needs more corrections than either reviewed arm | Mean corrections per trial, described |

**If the traps don't work:** if plain gets 8 or 9 of 9 right on the first PR, the agent reads these traps correctly without any review. Then H2 can't be tested, and I'll say so rather than invent harder cases after seeing the data. Harder traps would be Experiment 3, pre-registered separately.

## Run

```bash
python3 evals/run.py --cases logout-cache,public-rename,report-timezone --reps 3 --mode gated    --jobs 3 --run-id exp2-odin
python3 evals/run.py --cases logout-cache,public-rename,report-timezone --reps 3 --mode planmode --jobs 3 --run-id exp2-planmode
python3 evals/run.py --cases logout-cache,public-rename,report-timezone --reps 3 --mode plain    --jobs 3 --run-id exp2-plain
python3 evals/compare2.py ~/.cache/odin-evals/results/exp2-{odin,planmode,plain} --markdown ~/.cache/odin-evals/results/exp2.md
```

The three arms run concurrently, so time-of-day API latency affects them equally.

**Stopping rules (carried over from Experiment 1):**
- Harness errors are never counted as failures.
- More than 2 errors of the same class in one arm: stop, fix the harness, re-run only those trials, and say so.
- A trial that hits the $3 cap is an error.
- Every transcript is read before the results are reported, which is the lesson of Experiment 1.

## Pre-flight

One `logout-cache` trial per arm, about $1. Check:
- the reviewer's plan verdict for Odin and planmode, by hand
- that Odin's `revise_huginn` and `reject_pr` lead to a revised plan and a fix
- that planmode builds only after approval (no edits in the plan-mode turns)
- that plain's correction lands on the same branch and PR

## Cost

| Item | Estimate |
|---|---|
| Probes and pre-flight | about $1.20 |
| Odin, 9 trials at $0.35–0.60 (revisions add turns) | $3–5.50 |
| Plan mode and plain, 18 trials at $0.10–0.25 | $2–4.50 |
| Reviewer, about 40 plan reviews at $0.015 | $0.60 |
| **Total** | **about $7–12**. Hard ceiling $3 × 27 = $81, which won't be approached |

## What this experiment can't tell you

- How a real human reviews. This one knows the intent exactly and types the same correction every time.
- Whether the traps are representative. They're three hand-made ones, and the ones with a clue in the repo may favour careful readers in any arm.
- Anything about tasks big enough for Odin's subagents. These are still small.

## Pre-flight results (2026-11-10, about $0.85 with the probes)

| Run | Arm | Result | Cost | Time |
|---|---|---|---|---|
| `exp2-preflight-gated` | Odin | Reviewer approved the first plan ("delete_prefix of `user:<id>:`… other users and catalog unaffected"); the pre-PR gate's hidden tests pass; PR opened | $0.31 | 78 s |
| `exp2-preflight-planmode` | plan mode | Planned without editing (it explored with a subagent and read-only Bash), reviewer approved, built after approval, PR opened | $0.18 | 49 s |
| `exp2-preflight-plain` | plain | Right on the first PR | $0.06 | 16 s |
| `exp2-preflight-plain-traps` | plain, the other two cases | Both right on the first PR | $0.07 each | 16 s, 24 s |

- **The plumbing works.** Checkpoints are recorded for every arm. Plan mode builds only after approval. The trial's plan file is removed, and the two files left in `~/.claude/plans` predate the experiment.
- **Bug, fixed:** plan mode called ExitPlanMode with a one-line pointer ("See plan file: …"), and `latest_plan` let it override the full plan file, so the reviewer judged the pointer. The plan file now wins (regression test added). The pre-flight verdict happened to be right anyway.
- **The traps don't trap.** Plain Claude Code got all three right with no review. Every agent read the clue: the cache docstring ("shared by every user"), the README's partner webhook, and `STORE_TZ`. On Sonnet 5.5, **a clue in the repo is enough**. With these cases the review would never fire, so the correction paths (`revise_huginn`, `reject_pr`, plan-mode revision, plain's late fix) went unexercised, and H2 can't be tested.

### Pre-flight 2: traps with no clue in the repo (about $0.20)

The intended readings were fixed before this probe, by one rule: the reading a domain expert would pick that isn't the textbook default.

| Case | Plain built | Expert reading? |
|---|---|---|
| `coupon-stacking` | Each coupon applied to what the earlier ones left | Yes |
| `account-expiry` | Activity = the later of last login and last order | Yes |
| `tax-rounding` | Tax per line; total = "sum of the tax-inclusive lines, so the receipt always adds up" (its own docstring) | Yes |

**Six traps, six right on the first try, with no review.** On these single-decision ambiguities, Sonnet 5.5 picks the careful reading on its own. A plan review only has something to catch when the human wants the *less* sensible reading, so that's the only way left to make the gate fire. That's no longer a trap the agent should have seen; it's a preference only the human holds.

(Times are quantized to the runner's 2-second poll, which is why short trials keep reading 16.3 s. It doesn't matter for trials over a minute.)

## Result

**Stopped before the full run. Total spend about $1.06** (probes $0.12, the reviewer check $0.03, pre-flights $0.91).

Across six hand-built traps (three with a clue in the repo, three without), plain Claude Code on Sonnet 5.5 built the intended reading every time, with no plan review, no question to the human and no correction. Odin and plan mode, in the one case each was tried on, also got it right, and the reviewer approved their first plan. So the plan gate never had anything to catch. Plan review can only pay off when a wrong plan reaches it, and on single-decision ambiguities like these, the agent didn't produce one.

What that does and doesn't mean:
- **It doesn't show that plan review is useless.** It shows that this kind of ambiguity (one decision, where one reading is more careful than the other) no longer trips the model. When there's a careful reading, the agent finds it.
- **The case that's left is a preference only the human holds:** a business rule that is arbitrary, or less sensible than the default. A review catches that only if the plan *states the choice*. That's a different and narrower claim, and the runner can already test it: set a case's intent to the reading plain didn't pick, and the review, correction and checkpoint plumbing does the rest.
- **The other case left is bigger tasks**, where several decisions interact, and a plan is the only place a human can see them together. That's Experiment 3's question.

**What was built and stays useful:** `reviewer.py` (an LLM plan reviewer that knows the intent, plus hidden-test code review); a reviewing mode for `partner.py` that answers Odin's gates with `revise_huginn` and `reject_pr`; a `planmode` arm that runs Claude Code's own plan mode headless; per-checkpoint records; `compare2.py`; and six trap cases with wrong-reading self-tests. The correction paths are covered by unit tests, but none has run end to end against a live agent, because no live agent ever needed correcting.

| Arm | Trials | Right on the first try | Mean cost | Notes |
|---|---|---|---|---|
| Odin | 1 (`logout-cache`) | 1/1 | $0.31 | The reviewer approved the first plan |
| plan mode | 1 (`logout-cache`) | 1/1 | $0.18 | Planned read-only with a subagent, then built after approval |
| plain | 6 (every case) | 6/6 | $0.07 | |
