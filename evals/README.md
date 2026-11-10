# Odin evals

Runs fixed dev tasks through Odin in headless Claude Code and grades what each run leaves behind. Plan and rationale: [PLAN.md](PLAN.md). What the first runs taught us: [SPIKE.md](SPIKE.md).

## Run

```bash
python3 -m unittest discover -s evals/tests             # free: null fails, oracle passes, for every case
python3 evals/run.py --cases all --reps 3 --jobs 3       # paid: about $0.20–0.40 per trial on Sonnet 5.5
python3 evals/summarize.py evals/results/<run-id>
```

Useful flags for `run.py`:
- `--cases a,b` runs a subset.
- `--mode waiver|gated` overrides each case's own mode.
- `--model` defaults to `claude-sonnet-5-5`.
- `--odin-dir` points at another Odin checkout, to compare versions.
- `--timeout` is the hard ceiling per trial and `--idle-timeout` is the hang detector.

## What a trial is

1. The case's `repo/` is copied into a fresh git repo with a local bare `origin`, then the case's `setup.sh` runs (for example, to leave another session's uncommitted work behind).
2. `claude -p "/odin:odin <prompt>"` runs with `--plugin-dir <odin-dir>`, the installed Odin turned off, its own `XDG_CONFIG_HOME` and `ODIN_ROOT`, a fake `gh` on `PATH` (`bin/gh`), notifications off (`ODIN_NOTIFY=0`), and a per-trial budget cap.
3. In `gated` mode, `partner.py` answers every approval checkpoint through `write_wakeup_event()`, the same path as the dashboard. In `waiver` mode, the prompt waives the checkpoints.
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

The `expect` fields (`pause_at_plan`, `parallel_ok`) are read by the protocol checker (milestone 2), which isn't built yet.
