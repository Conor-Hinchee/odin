# Heimdall's pre-PR gate and the PR body

## Rendering the diff review

When code changed, render the contextual diff into the Heimdall card:

```bash
python3 "$ODIN/odin-state.py" update-phase --file "odin-states/<task-slug>.html" \
  --phase heimdall --status awaiting_human \
  --title "🛡️ Iteration N — Heimdall Verification & Pre-PR Review" \
  --summary "Tests and linters pass. Review the diff to approve opening the PR." \
  --with-diff --diff-context 12 \
  --diff-rationale "<what changed and why>" \
  --file-note "<path>=<why this file changed>" \
  --html "<test and lint results>"
```

The review shows 12 lines of context, a rationale banner, per-file notes, split and unified views with word-level highlights, and line or whole-file comments.

## The gate

1. If tests pass and no PR exists yet, pause at `awaiting_human`, ask the user to approve opening the PR (chat and dashboard), and arm the watcher ([wakeup.md](wakeup.md)).
2. On approval, the first call marks Heimdall `active`. Then push the branch and open the PR.
3. Run `update-phase --phase heimdall --status done --with-diff --pr-url "<url>"`. This stamps `data-pr-url` and the 🔗 PR badge.

If the user waives the PR at the gate, note that in the Heimdall card; sign-off can then proceed without one.

## The PR body

- Include a `### Prompt Summary` bullet list of the user's prompts.
- Never write a bare `#<digits>` for numbered rules or steps ("Rule 12", not "Rule #12"), because GitHub links those to unrelated issues.
- If the user's Odin config has a `pr` block, the plugin's PreToolUse hook (`odin-pr-hook.py`) adds the configured label, a session telemetry section and the verbatim prompts to `gh pr create`, and asks the user to confirm the rewritten command. Don't add those sections by hand.
