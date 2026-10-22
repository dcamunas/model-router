# Changelog

Format from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.17.0] - 2026-10-22

What a repository with a team needs to run the router well.

### Added

- Risk floors per repository, in `.claude/model-router.json`: rules of a pattern and a risk, searched in a
  brief's description and first lines. They only raise. The hook, `router plan` and `router run` follow them;
  `router project` lists them with how often each fired; `router doctor` flags rules it cannot use.
- `router agents`: advice for the agents whose model and effort are fixed by hand, per agent and cell in a
  repository, from runs and repeats (another subagent starting soon after on an alike prompt). It says
  measuring, raise, lower or keep, prices a change of model, and never edits an agent.
- `router project --team`: turns the plugin on for everyone in a repository through its `.claude/settings.json`
  (marketplace and plugin, every other key kept), and prints the command each person runs once.
  `router doctor` says when the repository has it on and the profile lacks it.
- `router plan --workflow`: the options of a workflow `agent()` call, `{model: 'opus', effort: 'medium'}`.
  The hook does not see workflow agents; `router plan` needs no text when the kind and the risk are given.
- When not to launch a subagent, in the session context and the skill.

### Changed

- The router never chooses `max` effort on its own: the lattice stops at `xhigh`, so ladders, feedback and
  per-repository raises never land on it, and a `max` cell from a calibration or a tuning runs at `xhigh`.
  A cell named by hand, an agent's own effort and a bench matrix given with `--cells` still take it.
  `router cells` and `router bench --matrix` list 16 cells, not 20.

### Fixed

- A session opened before a plugin update stopped routing until a new session started, and a SessionStart
  from it (a compact, a resume) put the older version back under every newer session. The hook now runs the
  newer environment as it is and never downgrades.

## [0.16.1] - 2026-10-22

### Removed

- The marketplace. The catalogue `dcamunas` is its own repository now,
  [dcamunas/claude-plugins](https://github.com/dcamunas/claude-plugins); this one is the plugin and the
  package. `claude plugin marketplace add dcamunas/claude-plugins` is the line that changes.

## [0.16.0] - 2026-10-22

### Removed

- Two-judge verification and the door to Codex. They are their own project now:
  [model-tribunal](https://github.com/dcamunas/model-tribunal). Gone from here: the `panel` and `tribunal`
  verification modes, `arbiter` in plans, records and the SQLite index (schema 4; `router db rebuild` runs on
  its own), the `MODEL_ROUTER_TRIBUNAL`, `MODEL_ROUTER_ARBITER` and `MODEL_ROUTER_CODEX_HOME` variables, the
  `external`, `codex_home`, `arbiter` and `qa` keys of config.json (an old one is ignored), and the
  scoreboard's agreement columns. The router verifies with one judge, as it always did on its own.

### Added

- `catalog.register` and `catalog.unregister`: another package may add a model it serves itself, so its
  cells can be named, priced and recorded; the router never routes to it.
- `Verifier.ask`: one judge, one prompt, one verdict, for a package that composes more judges on it.
- `MODEL_ROUTER_CHILD=1` in every session the router starts itself (a worker, a judge), so a hook that
  watches a person's turns can leave them alone.

### Changed

- The README is a storefront now: a pitch, a flow figure in four symbols (light and dark), a quick
  start, what you get, and a table that sends each question to its page. The detail moved, not cut,
  to `docs/`: install, Claude Code, command line and library, how it decides, calibration, privacy and
  background.
- `docs/showcase/flow.py` draws the figure; the package description matches the README's pitch.

## [0.15.1] - 2026-10-22

### Changed

- The 0.15.0 code brought to the repository's own rules. What the router decides does not change:
  - keyword-only arguments from the third one on, named constants instead of bare numbers, full names
    instead of abbreviations, no private helpers shared across modules, no `assert` in library code;
  - functions split so each does one thing: `shared.prune` apart from `shared.write_own`, the hook's sharing
    apart from its tuning, and `router project` computing in one place and printing in others;
  - a start-cell change is a typed record (`projects.Change`), not a loose dict;
  - `projects.raise_failing` requires the local evidence, so the check that keeps the team's counts from
    raising a start on their own cannot be skipped by a forgotten argument.

- One clock for the learning code (`model_router.clock`). The tests freeze it instead of
  reading the real time, and three timestamp parsers became one.

- Every module that uses `Any` says why next to the import; ruff now checks blind `except Exception` (BLE).

- The hook's module docstring describes all three events and what SessionStart now does.

Small changes in behaviour that come with it:

- At session start, shared counts are written even when tuning fails; before, a tuning error skipped them.

- A measured subagent's time is stored in UTC, not in local time with its offset. Both read the same.

- A transcript time without a zone is read as UTC, as everywhere else. Claude Code writes them with one.

- `router plan` says "tuned for <repo>: starts on X instead of Y", and `router project` prints changes as
  "from X to Y". Its errors now say what to do.

### Fixed

- README: a shared week file is deleted a week after it leaves the window, not as soon as it does.

## [0.15.0] - 2026-10-22

### Added

- Learning per project. The router learns, per git repository, where each kind of task should start.
  - The quality signal is a relaunch: Claude launches again, in the same session and within 30 minutes, a
    task of the same kind and risk with an alike prompt on a higher cell (a more capable model, or the same
    model with more effort). Moving from the read-only agent to the general one is not a relaunch.
  - Prompts are compared through a signature: a MinHash of sixteen numbers, salted per installation, no text.
    Outside a git repository nothing is learned.
  - Only launches where the router chose both model and effort count (a review moved to another model than
    the chat's does not), from the last 13 whole weeks and since the last reset or applied change.
  - The start cell in force, relaunched in 2 of 6 launches or more and at least 30 %, moves one step up at the
    next session start, with a line in Claude Code saying what moved and why.
  - A cheaper start is only proposed, after 10 launches with none relaunched: the cell it was raised from, or
    the next one down if at least 20 % cheaper, never below a risk floor nor onto a failing cell.
  - Tunings live in `~/.model-router/projects/`, written under a lock. A tuned cell remembers the cell it
    replaced and is ignored once the table or the calibration no longer gives that cell.

- Shared counts, opt-in per repository, through `.claude/model-router/` committed in the repo.
  - Each installation adds one file per ISO week, a day after the week closes, and never changes it:
    launches and relaunches per kind, risk and start cell. No prompts, signatures, paths, names or costs, and
    a salted file name.
  - Each installation counts the other installations' files with its own evidence, never its own (under any
    id it used for that repository path; another clone or worktree is a teammate). Only weeks after the last
    reset and inside the window count, at most 30 launches per installation and cell, at its own fail rate.
  - Counts from elsewhere alone never raise a start: a raise needs a relaunch on this machine too, or it is
    only proposed.
  - Files are read and written without following links, malformed entries are skipped, and a shared directory
    that links outside the repository is not used. Any installation deletes files a week after they leave the
    window. `MODEL_ROUTER_SHARE=0` keeps an installation out.

- `router project [PATH] [--apply] [--reset] [--share]`: the subagents routed in a project (with how many
  came from other installations), how many were relaunched, the tuning in force with its last changes, and
  the changes that wait for a hand (cheaper starts, raises on the team's counts alone).

- `Policy(tuning=...)`: a project's start cells, applied after the risk floors. A tuned cell below a floor is
  ignored, and so is one whose model is not available when what stands in is weaker than the start.
  `router plan` applies the current repository's tuning, as the hook does.

## [0.14.0] - 2026-10-22

The router is a Claude Code plugin for Claude models, and its job inside Claude Code is subagents: the model,
the effort and the agent each one needs, and a measurement of what each one really spent.

### Added

- A read-only agent, `model-router:reader` (Read, Grep, Glob, WebFetch, WebSearch; no edits, no shell, no MCP
  servers), shipped by the plugin and copied into the profile as `router-reader` by `router install`. The hook
  sends a general-purpose subagent there when its prompt declares it: `access: read` on the first line, a line
  of declarations only (`kind: review, risk: low, access: read`). Anything else keeps the general agent, and
  `access: write` anywhere in the prompt's head or the description always does. Guessing from the wording was
  tried and dropped after two blind reviews: half of 71 realistic write requests read like reading work. The
  session context and the skill ask Claude to declare access on every subagent. Measured on the same review of
  one file, same model and effort: 0.0029 USD of context on the general agent, 0.0009 USD on the reader, with a
  quarter of the context written to the cache. When the reader gets work it cannot do, it asks to be relaunched
  with `access: write`. `router config --lean off` and `MODEL_ROUTER_LEAN=0` switch it off; `router doctor`
  says whether it is there.

- Subagent measurement: on `SubagentStop` the hook reads the subagent's transcript and records its model,
  turns, tokens (cache reads and writes, once per message id), list-price cost, peak context, tool calls and
  duration, next to the routing decision (joined by a hash of the prompt). The transcript is read again once
  it has settled, because the last message is not in it when the event fires. Context tokens are exact;
  output tokens are a lower bound (each entry keeps the usage of the stream's start), so the cost is a floor
  and `context_cost_usd` is the exact part. Appends and the settling rewrite share a lock; a resumed subagent
  counts once. `router stats` shows it per agent and model; `router install` and the plugin register the
  event, and `router doctor` warns when both are installed.

### Removed

- Models of other companies. The router works with Claude models only: their declarations, their doctor
  checks, and the judges and workers on them are gone. A model list or a judge setting from an older version
  that names one no longer stops the router or the hook.

## [0.13.0] - 2026-10-22

Everything in this version left the router in 0.16.0 and is recorded where it went (see 0.16.0).

## [0.12.0] - 2026-10-22

Everything in this version left the router in 0.16.0 and is recorded where it went (see 0.16.0).

## [0.11.1] - 2026-10-21

### Added

- `router calibrate --version VERSION` keeps the records one router version wrote (`this` = the one running): a verification rule that changes between versions changes what a verdict means, so a calibration measured after 0.11.0 should not mix in verdicts the worker gave itself under 0.10.0.

### Fixed

- `router calibrate --help` crashed on the `%` of its defaults; every subcommand's `--help` is now tested.

## [0.11.0] - 2026-10-21

### Changed

- `light` verification (the default at high risk) is no longer the worker's own cell in a fresh context: it is
  the nearest other model at low effort, the tier above before the one below (haiku is judged by sonnet/low,
  sonnet by opus/low, opus by fable/low, fable by opus/low). It never goes more than one tier down; when the
  only other model is the full judge's, the worker's own cell, effort capped, takes the seat. Measured on a
  private eval of 28 real tasks: Haiku and Sonnet passed their own first attempts on 89 % and 93 %; an Opus
  judge passed 64 % and 68 % of the same answers, with word limits broken, formats ignored and facts invented
  among the reasons. The bench estimate prices the judges the plan names instead of a constant per mode, and
  `router calibrate` skips `router judge` records, whose worker cost is not this router's.

- `router calibrate` counts only independent verdicts as evidence: a checker, or a judge on a model other than the worker's that backed the final verdict. A verdict the worker's own model alone gave, as `light` produced until now and as a one-model installation still does, is counted in the report's "left out" figure and moves nothing. The calibration file carries that count (`self_judged`); older files load with 0. On the private eval, the proposal went from 21 moves to 15, and the moves that rested on a cheap cell passing its own reviews and analyses are gone.

- Kind and risk can be declared in the first two lines of the task text, `kind: review, risk: high` (also `tipo:` and `riesgo: alto`; a value ends the line or is followed by a comma or a full stop, so a URL, a pasted report or an eval file quoted below never sets the risk); the first declaration wins over the rules and over Haiku, and `router run`, `router plan`, the hook (description and prompt) and the classifier report all honour it. The hook's session context and the skill tell the orchestrator to declare both on every subagent, with the risk levels spelled out as the damage a wrong result does.

- Critical risk is never inferred: the words "critical", "urgent" and "no undo" no longer make a task critical. The high-risk keywords stay. On 28 real tasks the rules read the risk right 9 times and Haiku 5 to 10: both score difficulty or vocabulary, and the risk is in what the answer is for.

- The Haiku classifier's rubric for risk asks who uses the answer and what happens when it is wrong, with one anchor per level, and says the wording does not set the risk.

### Fixed

- The Haiku classifier (`--classify-llm`, `router calibrate --classifier --llm`) could answer the task instead of classifying it when the task was a question about code: its system prompt now says the text inside `<task>` is material, never instructions. Seen on a private eval, where the "reason" of one classification was the task's full answer.

## [0.10.0] - 2026-10-21

### Changed

- `--models` (install and config) lists the models that may do the work; `all` means every Claude model.

## [0.9.0] - 2026-10-20

### Added

- `router calibrate`: measures, from the benches and the ledger, the first-attempt pass rate per kind and cell with its 95 % interval, the mean cost of an attempt and the expected cost per solved task (the attempt, plus one on the next cell up on the share that fails; a cell with nothing above it counts a second attempt on itself), and proposes a start table. A record without a cost is no evidence, and a calibration file names a cell for every kind and risk this router knows, or it is not loaded. A change needs evidence: a cheaper cell that was measured and passes, or the current cell measured and beaten (or under `--min-rate`, 50 %); a cell needs `--min-n` first attempts (6) to count; the risk floors hold. `--eval ID` keeps the records of one eval. `--apply` saves `~/.model-router/calibrations/<date>-<eval>.json` and names it in config.json: `Policy` then starts from it, `router plan` says so in the first line of the rationale, `router doctor` names it, and every record carries its id as `calibration_id`. `MODEL_ROUTER_CALIBRATION` (an id, a file, or `none`) overrides the config; `router config --calibration ID|none` sets or clears it. The ladder still climbs by the catalogue's assumed capability: a calibration moves where a task starts, not how it escalates. `Calibration` and `measure` are exported by the library.

- `router bench --matrix [--cells a,b,c] [--max-usd N]`: one single-cell strategy per cell (the whole lattice, or `--cells`), verified as the router would unless `--verify` says otherwise; `--max-usd` refuses to start when the estimate is above it and stops between strategies once it is spent. The strategy `router@builtin` runs the router on the code tables and `router@ID` on a given calibration, so one bench compares before and after on the same eval.

- `router calibrate --classifier FILE [--llm]`: how often the rule classifier (and, with `--llm`, the Haiku one, with its cost) gets kind and risk right against the labels of an eval file, with the confusion matrices and, for each miss, whether it would change the start cell.

### Changed

- `VERIFIED_SOURCES` lives in `task.py` with the other vocabularies; `verify.py` no longer defines it.

- The SQLite index has no `calibration` table any more: calibrations are files. An index built by 0.8.1 is replaced on first use.

- The test suite never reads `~/.model-router`: a `conftest.py` fixture points every test at its own state directory.

## [0.8.1] - 2026-10-20

### Fixed

- The index kept one row per task id, so the same eval task under two bench strategies, or the same bench run twice, left only the last record in `router.db`; the ledger files had them all, and `router stats`, which reads the main ledger, was not affected. Rows are now keyed by ledger file and task id, overrides included. An index built by 0.8.0 is replaced on first use.

- A `router.db` that could not be written (a file that is no database, a lock held for too long) stopped `router run` after the answer had been paid for and written to the ledger. The index is best effort now: the command finishes, warns on stderr, and the next `router stats` rebuilds it.

- A rebuild stopped on a line that was valid JSON but no record (no task id, a list instead of attempts) or on a line cut inside a multibyte character. Such lines are skipped and counted as unreadable, as the README said they were; a null count reads as zero.

- `router db` notices a ledger file that was deleted or rewritten in place: it compares size and modification time, not size alone.

### Changed

- README, "What leaves your machine": the ledger and the index keep the judges' reasons, which may quote a few words of the task or the answer; "only a hash" said too much.

- `router bench` in simulation says on stderr which flags it ignores: `--strategies`, `--repeat`, `--verify` and `--dry-run` apply to a file of tasks.

## [0.8.0] - 2026-10-20

### Added

- Every ledger record now says which router decided it: `router_version`, `calibration_id` (`builtin` until a calibration exists, `fixed` for a bench ladder), the task's `fingerprint` (a stable hash of kind, risk and prompt, so the same task can be compared across strategies and versions) and, for bench runs, `eval_id` (a hash of the eval file) and `repeat`. Old records read as before.

- A SQLite index of the ledger, `~/.model-router/router.db` (`store.py`, `Store` in the library): tasks, attempts, every judge's verdict with its provider, overrides and, for later, calibrations. The JSON-lines files stay the source; the index is written alongside every record, `router stats` reads it (and rebuilds it when something wrote to a ledger without it), `router db` shows its state and `router db rebuild` reloads it. Like the ledger, it stores no prompt or answer.

- `router bench`: `--repeat N` runs every task N times (ids become `id.rN`), `--verify auto|none|light|full` sets the verification of fixed-cell strategies (`auto` = what the router would pick by risk; today they ran unverified), `--dry-run` validates the file and prints the estimate without a call. Each bench ledger starts with a `bench` note naming its strategy, eval and repeat count.

- After a verified fail, the next attempt's prompt carries the reason (`with_failure`), and the attempt records `carried_reason`; `router stats` and the bench summary count the retries that repeat the previous reason.

### Changed

- The README says how this differs from learned routers (RouteLLM, LLMRouter, RouterArena): nothing is trained, and the work is in what happens after the answer.

## [0.7.0] - 2026-10-20

### Added

- `router judge "<task>" --answer FILE [--cell CELL]`: verify an answer written elsewhere (a Claude Code subagent, a person) with the plan's judges. Exit 1 on a fail; `--json` prints every judge's verdict. `Router.judge` is the library call. The skill now verifies subagent results this way by default and launches judge subagents only when the verdict needs tools.

## [0.6.1] - 2026-10-20

### Added

- An animated figure in the README, built from `docs/showcase/build.py`: a real `router run` on a critical
  writing task, with a Mermaid diagram of the loop next to it.

### Changed

- The attempts table of `router run` fits the terminal: the reason column takes what the other columns leave (`COLUMNS` is honoured), between 16 and 70 characters.

## [0.6.0] - 2026-10-20

### Added

- `json_payload` in `task` is the public JSON unwrapping the checkers use.

## [0.5.0] - 2026-10-20

Everything in this version left the router in 0.16.0 and is recorded where it went (see 0.16.0).

## [0.4.3] - 2026-10-20

Twenty-one fixes from an independent bug hunt over the whole package, each with a test.

### Fixed

- Runner: a judge that cannot be reached no longer loses the worker's attempt; it is recorded with source `judge-error`, the answer is kept and the task ends with status `error`. The retry after a truncated answer now uses the model's output cap when doubling would exceed it.

- Feedback store: it learns only from verified verdicts (checker, judge); unverified passes, refusals, truncations and judge errors are no longer counted, and the first-attempt flag belongs to the start cell only (a cell reached after a refusal was counted as a first attempt). `router feedback` moves a count only when the verdict actually changes, so repeating a correction no longer drains other tasks' passes. The file is written atomically and merged under a lock, so two processes sharing it keep each other's counts; an unreadable file starts empty and is replaced on the next save.

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

- The `full` judge is one fixed cell, `opus/high` by default, set with `router config --judge` or `MODEL_ROUTER_JUDGE`, in a fresh context. When the worker is on the judge's model, the judge moves to the next model up at low effort, or to the model below when there is nothing above. It was the cheapest cell clearly more capable than the worker, which at the top of the lattice meant Fable at xhigh or max judging Fable: one verdict cost $0.42 in the first real bench. No judge runs above `high` effort.

- The hook's cross-check for critical reviews never moves the review to a lower tier than the planned cell.

### Added

- `evals/v1.yaml`: a starter evaluation set of fifteen made-up tasks for `router bench`, reviewed so that a correct answer passes each checker and a lazy one does not.

- `router config --judge` and a `judge for full verification` line in `router doctor` and `router config`.

## [0.4.0] - 2026-10-20

### Added

- `judges_for` on policies and `judges` on every attempt (ledger and `--json` included).

- The simulation assumes the judges of one answer share their random draw half the time (`JUDGE_CORRELATION`), so correlated judges are priced in.

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
