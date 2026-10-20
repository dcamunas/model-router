# Changelog

Format from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.4.3] - 2026-10-20

Twenty-one fixes from an independent bug hunt over the whole package, each with a test.

### Fixed

- Runner: a judge that cannot be reached no longer loses the worker's attempt; it is recorded with source `judge-error`, the answer is kept and the task ends with status `error`. The retry after a truncated answer now uses the model's output cap when doubling would exceed it.
- Feedback store: it learns only from verified verdicts (checker, judge, panel); unverified passes, refusals, truncations and judge errors are no longer counted, and the first-attempt flag belongs to the start cell only (a cell reached after a refusal was counted as a first attempt). `router feedback` moves a count only when the verdict actually changes, so repeating a correction no longer drains other tasks' passes. The file is written atomically and merged under a lock, so two processes sharing it keep each other's counts; an unreadable file starts empty and is replaced on the next save.
- Task: zero or negative `max_tokens`, `max_attempts` and `budget_usd` are rejected (a zero budget used to mean the risk default).
- JSON checkers unwrap fenced blocks in every common shape: no newline before the closing fence, a space after the language tag, CRLF line endings, prose around the block.
- Classification: `borrador` and `medición` no longer count as high risk and `crítica` only as a whole word; `análisis` and `revísalo` land on their kinds; "fix this bug" is code, not review.
- Hook: a transcript line of an unexpected shape, an assistant entry without a model, or a log directory that cannot be written no longer silence the routing for that call.
- CLI: `install`, `uninstall` and `doctor` give one-line errors for a settings.json that is not an object or whose hooks have an unexpected shape; `uninstall` checks the skill before touching the hooks, so a foreign skill leaves the profile untouched; a missing `--file` or `--system` file is a one-line error; `bench` with no terminal to confirm is cancelled instead of a traceback.
- `claude -p` backend: JSON that is not an object, or a non-zero exit without an error flag, become a one-line client error; the served model is the requested one when the CLI lists it, else the busiest one, so a fallback is not reported falsely.
- Ledger: an override no longer mutates the in-memory records; `find` returns the record as written and `last_override` gives the latest human verdict.

## [0.4.2] - 2026-10-20

### Changed

- The default judge is `opus/xhigh` and judges are capped at `xhigh` (`max` is still lowered). 0.4.1 shipped `opus/high` with the cap at `high`; a judge at xhigh writes about 1.6 times the thinking of one at high, so verification of critical tasks costs that much more, and `router config --judge opus/high` brings the cheaper setting back.

## [0.4.1] - 2026-10-20

### Changed

- The `full` judge is one fixed cell, `opus/high` by default, set with `router config --judge` or `MODEL_ROUTER_JUDGE`, in a fresh context. When the worker is on the judge's model, the judge moves to the next model up at low effort, or to the model below when there is nothing above. It was the cheapest cell clearly more capable than the worker, which at the top of the lattice meant Fable at xhigh or max judging Fable: one verdict cost $0.42 in the first real bench. No judge runs above `high` effort. The `light` judge is the worker's own cell in a fresh context, capped the same way, and the panel is those two.
- The hook's cross-check for critical reviews never moves the review to a lower tier than the planned cell.

### Added

- `evals/v1.yaml`: a starter evaluation set of fifteen made-up tasks for `router bench`, reviewed so that a correct answer passes each checker and a lazy one does not.
- `router config --judge` and a `judge for full verification` line in `router doctor` and `router config`.

## [0.4.0] - 2026-10-20

### Added

- `panel` verification mode: two judges on different cells, cheapest first, blind to each other. The answer passes only when both pass, and the second judge is not asked when the first fails. The policy uses it on its own at critical risk for `analysis`, `write` and `review` tasks with no checker; `Policy(panel_kinds=...)` changes the kinds, `--verify full` forces one judge and `--verify panel` forces two on any task. It is the part of an LLM council that pays when every model is Claude: a second independent opinion, without shared answers, ranking or chairman.
- `judges_for` on policies and `judges` on every attempt (ledger and `--json` included); `router plan` prints both judges of a panel.
- The simulation compares `router` with `router (no panel)` and assumes the judges of one answer share their random draw half the time (`JUDGE_CORRELATION`), so correlated judges are priced in.

## [0.3.0] - 2026-10-19

### Changed

- Everything the user sees is now English: command line, hook notices, skill, attempt reasons, backend errors and the plugin listing. The classification rules still understand Spanish prompts.
- Verdict labels are `pass` and `fail` (were `OK` and `KO`). The simulation's strategies are named by their cells (`fable/xhigh`, `sonnet>opus`, `router`, `router+feedback`).
- The example tasks file is `tasks.example.yaml`, in English.

### Removed

- The Spanish README and changelog.

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
- Tests of `router run` and `router feedback` that go from the command line to the ledger.
- This file.

## [0.1.0] - 2026-10-19

First public version: lattice of (model, effort) cells, verification by risk, ladder, ledger, feedback, simulation, `claude -p` backend without an API key, hook and skill for Claude Code, `router install`.
