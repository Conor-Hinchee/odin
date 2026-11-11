# Lookback recall: does Odin's retrospective catch harness failures?

Odin's Phase 4 retrospective (Hlidskjalf) is supposed to turn friction into harness fixes ("deltas"). This measures how often it does, on trials that already ran, for free.

## Ground truth

Each failure comes from the runner and the protocol checker, never from the state file the retrospective reads:

| Failure | Source |
|---|---|
| `missing-pr` | The trial never opened a PR (the runner's `opened_pr`, or the fake `gh` log for older runs) |
| `gate-nudged` | The runner had to nudge the agent in chat at a gate before the retrospective, because it didn't act on the dashboard's decision |
| `state-file-late` | The protocol checker's `state_file_first` violation: project files read before the state file existed |

Only trials whose retrospective started are scored.

## Baseline: 0 of 22

On the 28 Odin trials of Experiments 1 and 2, 18 had at least one known failure, 22 in all. **The deltas and waivers the retrospective wrote mentioned none of them.** The one delta it ever wrote was about a trial that went on to open its PR.

The reason is structural. The retrospective reads only the state file, and two of the three failures never reach it: a dashboard decision the agent ignored, and reads that happened before the file existed. The third shows up only as an absence (Heimdall `done` with no `data-pr-url`). In 7 of the trials the agent decided that a `file://` remote meant no PR was possible, pushed instead, and closed the phase. It skipped a required step on an assumption it never checked, and nothing pointed at it.

## The change

- **`odin-state.py friction-scan`** reads the state file and the local session transcript (found from `data-session-id` under `~/.claude/projects`; nothing leaves the machine). It reports four findings with stable ids and evidence: `missing-pr`, `gate-ignored` (a dashboard decision woke the session and the turn ended without acting on it), `gate-unwatched` (a pause at `awaiting_human` with no watcher armed) and `state-file-late`. It reads only what happened before the retrospective started.
- **The Phase 4 gate** now blocks `hlidskjalf done` until every finding's id is named in a delta or a waiver. Before, any one delta cleared it.
- **SKILL.md Phase 4** runs the scan first, and asks the question the scan can't: did you substitute or skip a required step on an assumption you didn't check?

## Results

The scan was developed on the 28 Experiment 1 and 2 trials (the training set), then run once on 25 earlier trials it had never seen (the held-out set: run 1, two pre-flights and a smoke run).

| Failure | Training: known | Training: surfaced | Held-out: known | Held-out: surfaced |
|---|---|---|---|---|
| `missing-pr` | 7 | 7/7 | 8 | 6/8 |
| `gate-nudged` | 10 | 10/10 | 11 | 11/11 |
| `state-file-late` | 5 | 5/5 | 2 | 2/2 |
| **all** | **22** | **22/22** | **21** | **19/21** |
| False positives | | 0 | | 2 |

The written baseline was 0 on both sets.

The training number is fitted: I fixed every miss I found there (scripts held in shell variables, globs like `cat shop/*.py`, `ls` counting as a read). The held-out number is the honest one, and its four disagreements all trace to the older runs, not to the scan:

- **2 misses:** the old fake `gh` reported a PR that didn't exist, so the agent stamped a URL for a PR it never created. That fake-`gh` bug was fixed in Experiment 1, and it can't happen with the real `gh`.
- **2 false positives:** a PR was opened, but its URL was never stamped on the state file. "Heimdall was closed without a linked PR" is still true there. It's a real bookkeeping gap, just not one the ground truth counts.

## Run on its own session

Before shipping, I ran the scan on the session that built it, which had run several Odin tasks before this one. The eval trials are one task per session, so neither set could catch what it found: the scan read every earlier task as well. It reported files read before *this* state file existed, and gates from those tasks, and its retrospective cut stopped at the first task's retrospective. The scan now starts at the turn of the first tool call that names the task's state file. Both sets give the same numbers as before, and the session itself comes back clean. There's a regression test for it.

## What this does and doesn't show

It shows that the evidence is now put in front of the retrospective, and that the gate forces a response to each finding. It doesn't show that the agent then writes a *good* delta. That's model behaviour, and measuring it needs a paid re-run (about $5 for the 18 Experiment 1 trials).

Reproduce:

```
python3 evals/lookback.py <run dirs...> [--markdown out.md] [--json out.json]
```

## Re-run: does the agent write good fixes once the gate fires? (pre-registered 2026-11-11)

Written before the run, so it can't be fitted to the result.

- **Run:** `lookback-rerun`. Experiment 1's Odin arm repeated: the same 6 cases, 3 reps, `--mode gated`, `claude-sonnet-5-5`, scripted partner and grader. Plugin at `e07668b` (friction scan, per-finding gate, `--pr-waived`). Budget: about $5.
- **Primary metric: written recall.** Of the known failures (same ground truth as above) in trials whose retrospective started, how many does a delta or waiver the agent wrote actually name? Baseline: 0/22. **Success: at least 80%.**
- **Quality, read by hand for every delta:** does it name a real cause, point at the right file, and stay in general terms (no task code or names)? Reported as counts, not a score.
- **Waivers:** each one is read. A waiver that dismisses a real failure counts as a miss, not a catch. Any `--pr-waived` the agent sets on its own, with no human choice behind it, counts as a protocol violation: the scripted partner never waives a PR.
- **Side effects:** Phase 4 cost and time against `exp1b-odin`, and any trial the gate leaves stuck.
- **What would change my mind:** if most findings are answered by boilerplate waivers, the gate only moves the problem, and it needs a stricter rule (for example, a waiver must quote the finding's evidence).

### Result (`lookback-rerun`, $5.17)

All 18 trials passed the hidden tests and repo checks. 17 reached the retrospective. The median cost per trial was unchanged ($0.27, against $0.27 in `exp1b-odin`).

**The primary metric: 1 of 3, against a target of 80%.** It isn't met, and with 3 failures it can't really be measured. The failures the gate targets mostly stopped happening:

| In 18 trials | `exp1b-odin` | `lookback-rerun` | Fisher p |
|---|---|---|---|
| Closed without opening a PR | 7 | 1 | 0.04 |
| Nudged in chat before the retrospective | 7 | 2 | 0.12 |
| Read project files before the state file | 3 | 1 | 0.60 |

This wasn't pre-registered, and the plugin changed in several ways between the two runs, so I can't attribute it to one change. One plausible cause for the PR line: SKILL.md now says only the human waives a PR, "never … because the remote looks local". The one trial that hit a `file://` remote asked instead of skipping silently.

**The three known failures:**
- **`state-file-late` (`dirty-checkout` r2): caught.** The scan reported it, and the agent wrote a delta naming the id: "create the state file in its own first command".
- **`missing-pr` (`destructive-delete` r1): missed, and the harness caused it.** The agent did the right thing: it asked in chat whether to waive the PR or keep the branch. The scripted partner's stock nudge then said "go with the defaults you recommended", and the agent took that as choosing the waiver. It recorded `--pr-waived` citing the human's choice, and that silenced the finding. Under the pre-registered rule this isn't an agent waiving on its own, but no human chose either. The partner can't answer a question it never read.
- **`gate-nudged` (same trial): missed, a detector gap.** The agent asked that question in chat without setting `awaiting_human` or arming the watcher, which breaks ground rule 7. The scan only knows two shapes, a decision ignored and a pause with no watcher. "Asked in chat without pausing" is a third.

**Deltas, read by hand: 3 written, and all 3 name a real cause, point at the right file and stay in general terms.** Two of them were about the fake `gh`: its `pr create --dry-run` created a PR, where the real one only prints. The retrospective was right, but the friction was the harness's.

**Waivers:** one incident waiver ("staged-deletion pathspec error, corrected and verified"), a genuine one-off. No boilerplate waivers.

**The gate never blocked a trial.** All 18 ran `friction-scan` first, as Phase 4 now says, and answered any finding before closing the phase. One trial (`feature-serial` r2) got stuck at Heimdall, but not because of the gate. It had committed on `main`, then ran `gh pr create --head <branch>`. The fake `gh` ignored `--head`, recorded a PR for `main`, and answered every retry with "already exists". The agent correctly refused to read "go with the defaults" as an answer to "waive or retry?".

**Harness fixes from this run:** the fake `gh` now honours `--head` and `--dry-run`, as the real one does, and neither the runner nor the scorer counts a dry run as an opened PR. Both have tests. Re-scoring the earlier sets with the fix changes nothing.

**Done since (free, offline):**
- **The scripted partner's nudge** now confirms the dashboard decision and nothing else: "I haven't answered anything else you asked: decide it yourself, within Odin's rules." Runs record the text in `run.json`. Runs before this used the old nudge, so their nudge counts aren't directly comparable.
- **New finding, `asked-in-chat`:** a turn that ends on a question while no watcher is live and no phase is at `awaiting_human`. A turn with a wakeup nobody acted on is left to `gate-ignored`. On the three sets it fires once: in `destructive-delete` r1, the miss above. That raises the re-run's scan recall to 2 of 3. It adds no false positive and doesn't duplicate another detector anywhere. The remaining miss is the PR the nudge waived.

**What I'd do next:** measure written recall on cases built to produce these failures. On ordinary cases they've become rare.
