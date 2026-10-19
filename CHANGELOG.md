# Changelog

Versión en español: [CHANGELOG.es.md](CHANGELOG.es.md).

Format from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.2.0] - 2026-10-19

### Added

- Claude Code plugin: the repository is both a marketplace (`dcamunas`) and a plugin (`model-router`). `/plugin install model-router --marketplace dcamunas/model-router` installs the hook and the skill. The hook builds its Python environment with `uv` in the plugin's data directory on the first session and rebuilds it when the version changes; `MODEL_ROUTER_UV` points at `uv` when it is not on the PATH.
- `router config [--models ...] [--profile-dir ...]`: shows or sets the profile for `router run` and the available models without touching hooks or skill, for plugin installs.
- `router doctor` recognises a plugin install and checks its environment.

### Changed

- `router install` becomes the alternative for profiles where marketplaces are not allowed; the plugin is the path documented first.

## [0.1.1] - 2026-10-19

### Fixed

- `router doctor` resolved the chat model's id as `claude-X-5-5`, which is wrong for Fable, and did not show its effort.
- An unknown model in `--models` or in `MODEL_ROUTER_MODELS` ended in an English traceback; it is now a one-line message with the valid names.
- `router install` wrote to `settings.json` before validating the models; it now checks models, hook binary and skill before touching anything.
- A corrupt line in `hook.jsonl` or `ledger.jsonl` crashed `stats` and `doctor`; it is now skipped and reported.
- `router uninstall --help` advertised flags it did not use.

### Changed

- Environment variables under one prefix: `ROUTER_HOOK`, `ROUTER_HOOK_LOG`, `ROUTER_HOOK_QUIET` and `ROUTER_CLAUDE_CONFIG_DIR` become `MODEL_ROUTER_HOOK`, `MODEL_ROUTER_HOOK_LOG`, `MODEL_ROUTER_HOOK_QUIET` and `MODEL_ROUTER_CLAUDE_CONFIG_DIR`. No compatibility with the old names.
- Everything the user sees is Spanish with accents, including the reason of every attempt and the errors of both backends.
- `ClientUnavailable` is renamed `ClientUnavailableError`; the old name stays as an alias in this version.
- Library: optional parameters are keyword-only in `Policy`, `Router`, `LadderPolicy`, `Verifier`, `step_up`, `step_down`, `ladder`, `run_simulation`, `format_report` and `Ledger.override`. `Task.estimated_input_tokens` and `Task.planned_output_tokens` replace the repeated calculations.
- `install.install` no longer accepts `uninstall=True`; there is `install.uninstall`.

### Added

- Docstrings on the whole public API, required by ruff on public modules, classes and functions.
- End-to-end tests of `router run` and `router feedback`.
- This file.

## [0.1.0] - 2026-10-19

First public version: lattice of (model, effort) cells, verification by risk, ladder, ledger, feedback, simulation, `claude -p` backend without an API key, hook and skill for Claude Code, `router install`.
