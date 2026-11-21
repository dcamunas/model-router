# model-router

Claude Code plugin that picks model and effort for every subagent by its risk. Public on GitHub (MIT), published through the `dcamunas/claude-plugins` marketplace. What it does and where each topic is documented: the table in `README.md`.

## Rules for any session in this repo

1. **Everything is in English and public**: code, docs, commits, CHANGELOG. No personal data, no names of employers or clients, no paths of this machine.
2. **Checks before a commit**, the same ones CI runs: `uv run ruff check src tests docs/showcase && uv run pytest -q && uv run pyright`. Conventions from the `clean-code` skill.
3. **A release touches three places together**: `version` in `pyproject.toml`, `version` in `.claude-plugin/plugin.json`, and a `## [x.y.z] - date` entry in `CHANGELOG.md` (Keep a Changelog, SemVer).
4. **Tests never touch the real profile**: `tests/conftest.py` points `CLAUDE_CONFIG_DIR` at a temporary directory; a new test that reads or writes the profile goes through it too.
5. **Docs follow behaviour**: a change to a rule, command or variable updates its page in `docs/` in the same commit. Figures in `docs/showcase/` are rebuilt with the commands in the README's Development section.
6. **Calibration with real models** runs on the Mac with `claude -p` on the subscription (the backend being measured), never on the NUC.
