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
