# Evaluating an agent workflow: Odin vs. plain Claude Code

Odin is a Claude Code plugin that runs development tasks through a fixed loop: a plan, human approval, execution (optionally fanned out to subagents), verification, a pre-PR review, and human sign-off. Each task gets a live state file and a local dashboard for approvals. Odin adds a lot of process. I wanted to know what that process buys, so I built an eval harness for it and ran a pre-registered experiment against plain Claude Code.

**Short version:** on small, well-specified tasks, plain Claude Code got the same correct result at a quarter of the cost and time. The eval found two real defects in Odin, and one of them is now fixed and re-measured. Most of the work, though, went into finding bugs in the harness itself. Five of them would have changed a headline number. A second experiment then tried to give Odin's plan review something to catch, and couldn't: plain Claude Code read all six hand-built traps correctly on its own.

About $24 of API spend in all, every run of both experiments included.

## The harness

A **trial** is one headless `claude -p` run on a small fixture repo. Each trial gets:
- a fresh git repo with a local bare `origin`, and a `setup.sh` that can leave traps behind, such as another session's uncommitted work
- a fake `gh` on `PATH` that logs every call and fakes PR creation
- a per-trial budget cap and a hang detector
- Claude Code's isolation flags (`--setting-sources project,local --strict-mcp-config`), so my own plugins, hooks and MCP servers stay out of both arms

In Odin's `gated` mode, a scripted partner (`partner.py`) answers each approval gate through that trial's own dashboard API, the way a human would click "approve".

Grading has three layers, from cheapest and most trustworthy to most expensive:

| Layer | What it checks | How |
|---|---|---|
| **Outcome** | Hidden tests (copied in only after the run), visible tests, git-level repo checks ("never commit another session's work"), scope | Deterministic, on the PR's head branch |
| **Protocol** | Odin's own rules: the state file exists before source is read, no edits before plan approval, no PR before review approval, staging files by name, subagent guardrails | Parse the stream-json transcript into an ordered list of tool calls, subagents included, and compare positions relative to gates |
| **Judge** | What a script can't check: is the plan any good, and does the change description match the diff? | One Claude API call per property per trial |

Every case has an `oracle/` reference fix. The free self-tests check that doing nothing fails every case and the oracle passes it, so a broken grader shows up before any paid run.

### The LLM judge

The judge (`judge.py`) uses `claude-opus-5-5` through the Anthropic Python SDK. It returns structured output: a Pydantic `Verdict` with reasoning, pass/fail and quoted evidence.

- **Narrow, checkable properties only.** The model under test is also Claude, so the judge never answers "which run is better". It answers questions like "is every claim in this description true of this diff, with nothing significant left out?"
- **The artifact is untrusted data.** The self-test includes known negatives (an empty plan, "I don't know", a plan for the wrong task) and a prompt injection. All of them must fail.
- **Blinded.** The judge never sees the arm, the model or the outcome score. Odin's PR bodies carry a `### Prompt Summary` section and a "Generated with Claude Code" line, which would identify the arm, so both are stripped first.
- **Errors aren't fails.** A refusal, an unparsed answer or an API error is recorded as a judge error and left out of the pass rate. An empty description fails by rule, with no API call.
- **Cheap to re-run.** The rubric sits in a single cached system prompt, identical across properties, so the cache prefix never varies. Verdicts are cached by a hash of the inputs plus the rubric version. The API client is created only when there's an uncached call, so re-grading old runs needs no key. Server-side fallbacks are on.
- **Not yet calibrated.** The plan was to trust judge numbers only once they agree with about 30 hand labels at about 90% or better. The labels aren't filled in yet, so every judge number below is read as a comparison between arms under the same judge, not as an absolute rate.

## The experiment

The hypotheses, metrics and stopping rules were written down before any run ([EXPERIMENT-1.md](EXPERIMENT-1.md)), so they couldn't be fitted to the results afterwards.

- **Arms:**
  - **Odin:** `/odin:odin <task>`, with all its gates.
  - **Plain:** the same task, plus "work on a new branch, commit, and open a PR". Without that suffix, plain Claude Code leaves the change uncommitted, and the comparison would only measure that Odin opens PRs.
- **Design:** same model (`claude-sonnet-5-5`), same 6 cases, 3 reps each, so 18 trials per arm.
- **Statistics:** Fisher's exact test for pass rates, bootstrap CIs for cost and time ratios, Wilson intervals for proportions.
- **Size:** with 18 trials per arm, only large differences can show up, and I expected correctness to hit a ceiling. I designed around the metrics that could still separate the arms.

### Results (run 2)

| Hypothesis | Odin | Plain | Verdict |
|---|---|---|---|
| H1: Odin costs more | $0.27 median, 72 s | $0.07, 16 s | **4.0×** the cost (95% CI 3.5–4.5) and **4.5×** the time (3.7–5.0) |
| H2: no correctness difference | 18/18 | 18/18 | No difference detected. Both at ceiling, as predicted |
| H3: Odin is safer in a dirty checkout | 3/3 | 3/3 | **Not supported.** Plain also left the other session's work alone |
| H4: Odin asks before going beyond the request | Removed the at-risk function 2/3, raised it first 2/3 | Removed it 3/3, asked 0/3 | Described, not tested. Odin surfaced the risk; plain never did |
| H5: Odin's PR descriptions are more accurate | **5/10** | **17/17** | **The opposite.** Fisher p = 0.003 |

## What the eval found in Odin

**1. PR descriptions that describe nothing.** H5's failures were mostly PR bodies made up only of the Prompt Summary: what the user asked for, not what changed. Odin keeps the description of the change in its state file's review card, which a reviewer on GitHub never sees. The fix makes SKILL.md require a leading `## Summary` (every behaviour change, every deletion, the tests added), and Odin's PR hook can now deny a `gh pr create` whose body has nothing else.

Then I re-measured it on the three cases that had failed:

| H5, same three cases | Before (run 2) | After | Plain |
|---|---|---|---|
| PR description matches the diff | 0/5 | **8/9** | 9/9 |

The one remaining fail is real: a Summary claimed "no public APIs deleted", but the diff renamed a public constant.

**2. Approvals the agent doesn't trust.** When a human approves in the dashboard, a background watcher wakes the session. Claude Code labels that wake-up as "not user input", which is the right call for a safety boundary. In 7 of 18 trials (39%) the agent then refused to act on the approval and asked for it again in chat. The harness counts this with a "chat nudge": it resumes the session with one message ("yes, that was me"), up to 3 times. In 6 of those 7 trials, the agent distrusted a second gate as well. This is a real cost in human attention that the outcome metrics never show. The fix belongs in how the approval reaches the agent, for example by having it check the decision recorded in the state file. Telling it to ignore the notice is the wrong fix. This one is still open.

**3. A review record that erased itself.** This one came from an earlier smoke run. The judge kept failing review cards for having no description of the change, but the transcripts showed the agents *had* written one. Re-rendering the diff after approval was wiping the rationale the human had approved. The judge was reading the artifact correctly, and the artifact was wrong. Fixed, with a regression test.

## What the eval found in itself

Here's what I'd tell anyone building agent evals: **audit the transcripts before you believe the numbers.** Each of these bugs produced plausible-looking results:

- **The agents were reading my repo's instructions.** Run 1's sandboxes sat inside the Odin repo, and Claude Code reads `CLAUDE.md` from every parent directory. Isolation flags don't cover that. Half of the plain transcripts quoted my repo's pre-commit rules, and those rules steer exactly the git behaviour H3 and H4 measure. Run 1 was thrown out. Trials now run outside the repo, and the runner refuses to start if any parent directory holds an instruction file.
- **Cost was triple-counted.** `claude -p` emits a result event at every turn end, and `total_cost_usd` is cumulative across the session, including across `--resume`, while turns and tokens are per turn. Summing them overstated a $0.29 trial as $0.89.
- **The fake `gh` lied.** It reported a PR that had never been created, so an agent skipped `gh pr create`. It now answers per branch, like the real one.
- **The nudge never fired.** The stall check looked for a status the dashboard had already moved past. The 11 stalled trials were resumed in place.
- **The protocol checker had false positives.** A chat nudge wasn't counted as the approval it was, and a multi-line plan containing the words "(git rm)" was read as a deletion. Shell splitting is now quote-aware.
- **PR bodies written to a file were invisible.** Some agents passed `--body-file`, which the fake `gh` logged only as a path, so the judge would have scored a good description as empty. It now keeps a copy.

Each fix has a regression test. All of them are free to run: 71 tests for the eval harness and 66 for Odin.

## Limitations

- **Small tasks.** These are 10–50-line changes. Odin was built for multi-hour refactors, and its subagent fan-out never triggered. The experiment says nothing about hard tasks.
- **Small n.** 18 trials per arm, and 9 in the re-run.
- **Uncalibrated judge, from the same model family** as the agents it grades.
- **The scripted partner approves everything,** so a review that catches a bad plan is never tested.
- **The sandbox gives itself away.** `git push` prints the local `file://` remote, and Odin sometimes concludes "no GitHub here" and skips the PR. That makes "opened a PR" unreliable for Odin.

## Experiment 2: could a plan review catch anything?

Experiment 1's partner approved everything, so the plan gate never had a chance to catch a wrong plan. Experiment 2 ([EXPERIMENT-2.md](EXPERIMENT-2.md)) was meant to give it one. I built cases whose obvious implementation passes the visible tests but isn't what the human meant, and a reviewing partner that knows the intent. The partner uses a model (`claude -p`, no tools, structured output) to approve plans or send them back, and runs the hidden tests on code. There were three arms: Odin, Claude Code's own plan mode, and plain.

The pre-flight ended it. Across **six traps**, three with a clue in the repo and three with none, **plain Claude Code built the intended reading every time**, with no review at all: it applied coupons in turn, counted an order as activity, and rounded tax per line "so the receipt always adds up". The plan gate never had a wrong plan to catch. I stopped at about $1 instead of spending $10 on a run whose answer was already clear, and wrote down why.

The useful result is the boundary it draws. On a single ambiguous decision where one reading is more careful, the model finds the careful one. A plan review earns its keep only for preferences that live in the human's head, or for decisions on bigger tasks that interact. That narrows what Odin's gates have to prove, and the harness now has the reviewer, the plan-mode arm and the correction plumbing to test either one.

## Next

- Calibrate the judge against hand labels.
- Redesign how dashboard approvals reach the agent, then re-measure the nudge rate.
- Add harder cases that get off the correctness ceiling: a cross-module refactor, and a task big enough to fan out, with several decisions that interact (Experiment 3).
- Test plan review on preferences only the human holds: the Experiment 2 plumbing runs this as is, once a case's intent is set to the reading the agent doesn't pick.

## Reproduce

Commands and setup: [README.md](README.md). Pre-registration, every run and every audit: [EXPERIMENT-1.md](EXPERIMENT-1.md) and [EXPERIMENT-2.md](EXPERIMENT-2.md). Earlier lessons: [SPIKE.md](SPIKE.md).
