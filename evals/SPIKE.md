# Milestone 0: headless spike (2026-11-10)

Two runs of `bugfix-single-file` (multi-item coupon bug, 4 hidden tests) through `evals/run.py`, Odin loaded with `--plugin-dir` from this worktree, the installed `odin@odin` disabled for the run.

| Run | Mode | Hidden tests | Phases done | Cost | Wall | Model |
|---|---|---|---|---|---|---|
| `waiver-1` | review, PR and sign-off waived in prompt | 4/4 pass | 1–4 | $0.17 | 29 s | claude-sonnet-5-5 (CLI default, not pinned) |
| `gated-1` | no waiver; gates answered by script | 4/4 pass | 1–5, PR via `gh` shim | $0.34 | 309 s (about 4 min of it was waiting at plan review) | claude-sonnet-5-5 |

## What we learned

1. **Gates work headless, no stream-json needed.** In `claude -p`, when Odin backgrounds `wait-for-human` and ends its turn, the process stays alive. When a wakeup arrives and the watcher exits, the session resumes on its own. `evals/partner.py` answers gates by calling `write_wakeup_event()`, the same path as the dashboard. All three gates (plan, pre-PR, sign-off) were answered this way, and the branch was pushed to the local bare `origin` and the PR "opened" through the shim.
2. **Much cheaper than the plan guessed.** $0.17–0.34 per trial on Sonnet 5.5, against the $1–3 estimate. 120 trials is roughly $20–40 on Sonnet, and probably a few times that on Opus. Pin `--model` from now on: the runs used the CLI's default model.
3. **Real Odin bug: duplicate watcher at plan review.** In `gated-1`, Huginn armed `wait-for-human` twice: once as `nohup … >/dev/null &` inside a Bash call, then again as a proper background command. Both poll the same wakeup file and whichever reads it first deletes it. If the `nohup` copy wins, the session never wakes and the run hangs until timeout. This time the right one won. → protocol check ("exactly one watcher per pause, run as a background command").
   **Fixed on this branch:** `wait-for-human` refuses to start when its stdout is `/dev/null`, and a newer watcher for the same task stops the older one, which then prints a "watcher stopped" notice instead of exiting silently. SKILL.md now says to arm the watcher once, as its own background command. Two new tests in `tests/test_odin.py`. Re-run `gated-2`: one watcher per gate, all 5 phases done, hidden tests pass, $0.37, 97 s with the partner started by `run.py`.
4. **Ground rule 1 held in both runs.** The state file was created and the notification sent before any project source was read.
5. **Waiver mode skips the branch.** `waiver-1` committed straight to `main`, while `gated-1` made `feat/<slug>`. That's fine, but the outcome grader has to grade the branch the PR points at (or `HEAD` of the branch Odin made), not the primary working tree.
6. **`odin-states/` lands untracked inside the target repo.** Graders have to exclude it from scope-creep counts (they do now).
7. **The trust notice leaks into the agent's behaviour.** Claude Code labels background-task results "not user input", so at sign-off the agent added "tell me if that sign-off wasn't yours" but went ahead anyway. That's harmless here, but protocol grading shouldn't count that hedge as a failure, and a real user would see it too.
8. **Desktop notifications fire during eval runs.** `odin-notify.sh` has no off switch, so each trial pings the Mac around eight times. Before batch runs, add an env toggle (for example `ODIN_NOTIFY=0`) or put a no-op `odin-notify` stub on the sandbox `PATH`. **Fixed:** `ODIN_NOTIFY=0` now silences it, and the runner sets it.

## Changes to the plan

- §3: drop the stream-json fallback. The scripted partner is the default, and waiver mode stays as a cheap variant.
- Cost: re-estimate after one pinned Opus run.
- Runner: pin `--model`, grade the PR head branch, silence notifications, and detect a hang at a gate (watcher armed, no wakeup consumed for N seconds) as its own error class.

# Smoke runs, six cases (2026-11-10)

Runs `smoke-1` to `smoke-4`, one rep per case on `claude-sonnet-5-5`, about $5 in all. Every failure in the first three runs was a bug in the eval, not in Odin:

| Run | Pass | What it exposed | Fix |
|---|---|---|---|
| smoke-1 | 3/6 | `dirty-checkout`: Odin correctly moved to a worktree and ran `gh pr create --fill`, with no `--head`, so the grader scored the unfixed `main` | The fake `gh` logs the branch it ran on; the grader uses it |
| smoke-1 | | `destructive-delete`, `feature-serial`: Huginn asked open questions, and the partner's "Approved by the eval partner" didn't answer them, so Odin asked again in chat | The partner's notes say to go with the defaults Huginn recommended |
| smoke-2 | 2/3 | `dirty-checkout`: Odin saw that `origin` was a local path, pushed the branch, and skipped the PR | `origin` looks like a GitHub URL (`insteadOf` maps it to the local bare repo); with no PR, grade the newest committed branch |
| smoke-3 | 0/1 | Odin checked the state file for the approval, found none, and refused. The partner only wrote the wakeup file, but the real dashboard records the decision in the state file first | Each gated trial gets its own Odin server; the partner POSTs the dashboard's own actions (`save_partner_plan`, `heimdall_gate_decision`, `update_project`) |
| smoke-4 | **6/6** hidden, **5/6** finished | `waived-review` stopped after the pre-PR approval ("If you didn't approve, say so") with no PR | New metric: **finished the loop**. This one is real Odin behaviour, not an eval bug |

## Findings about Odin itself

- **Dashboard approvals don't always resume the session.** Claude Code labels the watcher's output "not user input", and the agent sometimes asks for the approval again in chat instead of continuing. It happens more when the approval note doesn't address the open questions. In smoke-4 that was 1 of 15 checkpoints. A real user who clicks Approve would hit the same thing. This is worth tracking as its own metric before trying to change it: the notice is a safety feature, and any fix belongs in how Odin records and checks the approval (it re-reads the state file), not in telling the agent to ignore the notice.
- **Rule 9 held in `dirty-checkout`**: every run moved to a worktree, and none committed or lost the other session's work.
- **`waived-review` behaved as asked**: no pause at Phase 1, then a pause at the pre-PR gate.
