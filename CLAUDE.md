# Odin repository

Odin is a Claude Code plugin: `.claude-plugin/` (manifest + marketplace), `skills/odin/` (SKILL.md, scripts, dashboard, references), `hooks/hooks.json`, `tests/`.

- Commit directly to `main` with clear, atomic messages.
- Before committing, run `python3 -m unittest discover tests` and `claude plugin validate .`.
- Test plugin changes locally with `claude --plugin-dir .` (use `/reload-plugins` after edits).

## Keep other work out of this repo

Odin is a personal, public project, but it runs inside other codebases, including work ones. Nothing from those codebases belongs here.

- Never commit secrets: API keys, tokens, passwords, certificates, private keys, `.env` files or connection strings. If one shows up in a diff, take it out before committing, and treat any secret that was ever pushed as leaked.
- Keep machine-, employer- and client-specific details out of code, tests, fixtures and SKILL.md: paths, company and repo names, hostnames, internal URLs, ports and integrations. They belong in the user's Odin config (`skills/odin/scripts/odin_config.py`).
- Bring lessons from other projects back only in general form: describe the pattern, never the code, data or names it came from.
- `odin-states/` holds prompts and transcripts from other projects. It stays gitignored and is never committed.
