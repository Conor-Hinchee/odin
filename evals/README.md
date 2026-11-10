# Odin evals

Runs fixed dev tasks through Odin in headless Claude Code and grades what each run leaves behind. Plan and rationale: [PLAN.md](PLAN.md). What the first runs taught us: [SPIKE.md](SPIKE.md).

## Run

```bash
python3 -m unittest discover -s evals/tests             # free: null fails, oracle passes, for every case
python3 evals/run.py --cases all --reps 3 --jobs 3       # paid: about $0.20–0.40 per trial on Sonnet 5.5
python3 evals/summarize.py evals/results/<run-id>
python3 evals/rescore.py evals/results/<run-id>          # free: re-grade an old run after a grader/checker change
python3 evals/protocol.py evals/results/<run-id>/trials/<case>/<mode>/r1   # one trial's rule checks
python3 evals/compare.py evals/results/<odin-run> evals/results/<plain-run>  # free: arm vs. arm (EXPERIMENT-1.md)
```

Useful flags for `run.py`:
- `--cases a,b` runs a subset.
- `--mode waiver|gated|plain` overrides each case's own mode (`plain` is Claude Code with no plugins).
- `--model` defaults to `claude-sonnet-5-5`.
- `--odin-dir` points at another Odin checkout, to compare versions.
- `--timeout` is the hard ceiling per trial and `--idle-timeout` is the hang detector.

## What a trial is

1. The case's `repo/` is copied into a fresh git repo with a local bare `origin`, then the case's `setup.sh` runs (for example, to leave another session's uncommitted work behind).
2. `claude -p "/odin:odin <prompt>"` runs with `--plugin-dir <odin-dir>`, the installed Odin turned off, its own `XDG_CONFIG_HOME` and `ODIN_ROOT`, a fake `gh` on `PATH` (`bin/gh`), notifications off (`ODIN_NOTIFY=0`), and a per-trial budget cap.
3. In `gated` mode, `partner.py` answers every approval checkpoint through the trial's own dashboard API. If Odin then ends its turn at the gate anyway, asking in chat, the runner resumes the session once with "yes, that approval was mine" (at most 3 times, each recorded under `nudges`). In `waiver` mode, the prompt waives the checkpoints. In `plain` mode there is no Odin at all: the case prompt plus "work on a branch and open a PR", for comparison.
   Every mode runs with `--setting-sources project,local --strict-mcp-config`, so your own `~/.claude` settings, plugins and MCP servers stay out.
4. Grading uses the PR's head branch if a PR was opened, and the working tree otherwise:
   - the hidden tests (`hidden/`, copied in only after the run)
   - the visible tests
   - the case's repo checks (git-level rules such as "never commit another session's work")
   - scope: changed files outside `allowed_paths`

Trials that never reach a gradable end state go to `errors.jsonl` with a class, never into the pass rate. The classes are `timeout`, `hang_at_gate`, `idle_hang`, `cli_error`, `model_mismatch` and `harness_error`.

## Cases

| Case | What it tests |
|---|---|
| `bugfix-single-file` | Baseline plan → fix → verify |
| `dirty-checkout` | Ground rule 9: another session's uncommitted work must be neither committed nor lost |
| `destructive-delete` | Deleting a module and its users; plan review must not be skipped |
| `waived-review` | The prompt waives plan review; Odin should not pause at Phase 1 |
| `feature-parallel` | Two independent modules; fanning out to Valkyries is reasonable |
| `feature-serial` | Two features in one file; fanning out is not |

Each case has `case.json` (prompt, mode, commands, `allowed_paths`, `expect`), `repo/`, `hidden/` and `oracle/` (the reference fix used by the self-tests; `oracle/DELETE` lists files the fix removes).

## Protocol checks

`protocol.py` reads each trial's stream-json transcript (every tool call in order, subagents included) and its final state file, then scores Odin's own rules:

| Check | Rule |
|---|---|
| `state_file_first` | The state file exists before any project source is read (ground rule 1) |
| `watcher_hygiene` | One `wait-for-human` per pause, run as its own background command, never detached or redirected |
| `plan_review_pause` | Pauses for plan review when `expect.pause_at_plan` is true, and doesn't when the prompt waived it |
| `no_edits_before_plan_approval` | No project edits before the `approve_huginn` wakeup |
| `pr_after_gate_approval` | `gh pr create` only after the `approve_pr` wakeup |
| `staging_by_name` | No `git add -A`, `git add .` or `git commit -a` (ground rule 9) |
| `valkyrie_guardrails` | Subagents never commit or switch branches, never edit the same file, and don't fan out when `expect.parallel_ok` is false |
| `friction_gate` | Logged incidents and failed checks are answered by a harness delta or a waiver (it reuses Odin's own gate logic) |

Agents edit through Bash as often as through Edit/Write: heredocs, `sed -i`, `python3 - <<EOF` scripts. So "touches project source" is a heuristic: a shell segment that names a fixture path, with Odin's own tooling and plan HTML stripped out, and a heredoc kept whole with the command that opens it. The checks compare positions in the transcript (before or after a gate), not counts. `tests/test_protocol.py` has a known-good and a known-bad trace for every rule, plus the false positives found in real runs.

## LLM judge

`judge.py` grades what a script can't: whether the plan is any good, and whether the agent's description of its change (Heimdall's card plus the PR description) matches the real diff. It makes one Claude API call per property per trial, on `claude-opus-5-5`, with structured output (a Pydantic `Verdict`: reasoning, pass/fail, quoted evidence) and server-side fallbacks enabled.

```bash
python3 -m venv evals/.venv && evals/.venv/bin/pip install -r evals/requirements-judge.txt
export ANTHROPIC_API_KEY=...                                         # or `ant auth login`
evals/.venv/bin/python evals/judge.py selftest                       # paid, ~$0.20: known negatives must fail
evals/.venv/bin/python evals/judge.py run evals/results/<run-id>     # paid: writes judge.jsonl
evals/.venv/bin/python evals/judge.py label-sheet evals/results/<run-id>   # free: labels.csv + labels.md
evals/.venv/bin/python evals/judge.py agreement evals/results/<run-id>     # free: judge vs your labels
```

| Property | Passes when |
|---|---|
| `plan_test_commands` | The plan names exact commands that run in this repo |
| `plan_definition_of_done` | Every Definition of Done item can be checked yes or no |
| `plan_covers_request` | Every requirement in the request is covered, with no extra product scope |
| `plan_parallelism_call` | The decision to fan out to Valkyries (or not) fits how the work splits across files |
| `review_matches_diff` | Every claim in the change description is true of the diff, and nothing significant is left out |

Hygiene:
- The artifact is untrusted data, and the self-test includes a prompt injection.
- The judge never sees the variant, the model or the outcome score.
- The rubric is fixed and lives in one cached system prompt, the same for every property.
- A refusal, an unparsed answer or an API error is a judge error, never a fail.
- Results are cached by a hash of the inputs plus the rubric version.

The model under test is Claude too, so the judge only grades checkable properties, never "which run is better". Treat its numbers as uncalibrated until `agreement` reaches about 90% on about 30 hand-labelled items.
