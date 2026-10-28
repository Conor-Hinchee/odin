# Odin repository

Odin is a Claude Code plugin: `.claude-plugin/` (manifest + marketplace), `skills/odin/` (SKILL.md, scripts, dashboard, references), `hooks/hooks.json`, `tests/`.

- Commit directly to `main` with clear, atomic messages.
- Before committing, run `python3 -m unittest discover tests` and `claude plugin validate .`.
- Keep the code free of machine- or employer-specific values: paths, repo names, ports and integrations belong in the user's Odin config (`skills/odin/scripts/odin_config.py`), never in code or SKILL.md.
- Test plugin changes locally with `claude --plugin-dir .` (use `/reload-plugins` after edits).
