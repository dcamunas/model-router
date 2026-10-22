# Changelog

Format from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.14.0] - 2026-10-22

The router is a Claude Code plugin for Claude models, and its job inside Claude Code is subagents: the model,
the effort and the agent each one needs, and a measurement of what each one really spent.

### Added

- A read-only agent, `model-router:reader` (Read, Grep, Glob, WebFetch, WebSearch; no edits, no shell, no MCP
  servers), shipped by the plugin and copied into the profile as `router-reader` by `router install`. The hook
  sends a general-purpose subagent there when its kind is a reading one (search, review, analysis, summarize,
  extract, classify, chat) and nothing in its prompt asks to edit, run or reach a service; `access: read` on
  the first line asks for it, `access: write` keeps the general agent. Measured on the same review of one file,
  same model and effort: 0.0054 USD on the general agent, 0.0023 USD on the reader, with a quarter of the
  context written to the cache. `router config --lean off` and `MODEL_ROUTER_LEAN=0` switch it off; `router
  doctor` says whether it is there.
- Subagent measurement: on `SubagentStop` the hook reads the subagent's transcript and records its model,
  turns, tokens (cache reads and writes, once per message id), list-price cost, peak context, tool calls and
  duration, next to the routing decision (joined by a hash of the prompt). The transcript is read again once
  it has settled, because the last message is not in it when the event fires. `router stats` shows it per
  agent and model; `router install` and the plugin register the event.

### Removed

- Every model of another provider from the router: the OpenAI, Gemini, DeepSeek and xAI doors, `router config
  --external`, `--codex-home` and `--codex-models`, external judges and workers, and their doctor checks. A
  model list or a judge setting from an older version that names one no longer stops the router or the hook.

### Changed

- Codex is the tribunal's alone: `tribunal config --codex-home`, `--codex-models` and `--declare` set it up.
  It now always runs with its shell tool off, in an empty directory: with a shell its read-only sandbox read a
  decoy file outside the repository, and what it reads goes to OpenAI. The tribunal sends both judges the diff
  and the changed files as they are now; the Claude judge can still read the repository, and nothing outside it.

## [0.13.0] - 2026-10-22

The tribunal leaves the router and becomes a plugin of its own. The router picks a model and an effort,
checks with a checker or one judge on another model, escalates and calibrates. Two judges blind to each
other, a Claude model and a Codex model, with an arbiter on a split, are the `tribunal` plugin's: for the
code each turn changes, and for critical texts when you ask. Same repository, same engine, installed apart.

### Added

- The `tribunal` plugin, second entry in the `dcamunas` marketplace (`plugins/tribunal`, with its own
  README). Its `Stop` hook judges the code a turn changed against the request: a Claude judge that is never
  the chat's model, which wrote the change, and a Codex judge, in parallel, blind, read-only inside the
  repository; an arbiter on a split. A turn gets `rounds` reviews (2, at most 5) and a fail goes back to
  Claude with the findings until the last one; a pass is one line; a judge that cannot be reached never
  blocks, and the other judge's verdict is still shown. The change is what the
  working tree became since the request: a snapshot through a temporary index (HEAD's tree, every tracked
  file rehashed, untracked files up to 1 MB), so a commit during the turn still counts and neither the
  index nor the files are touched. The settings are fixed when the request arrives. A turn that only
  touched documentation is not judged; when another session's transcript shows an editing tool used during
  the turn, a fail is reported and not sent back. `SessionEnd` closes a session's turn. A diff over 50,000
  characters reaches the judges as the list of changed files plus its first part. The plugin installs no Python:
  it runs from the model-router plugin's environment.
- The `tribunal` command: `tribunal qa` (a code change, uncommitted or since `--base`), `tribunal judge`
  (a text written elsewhere; `--cell` keeps the author's model off the bench) and `tribunal config`
  (`--mode`, `--claude`, `--codex`, `--arbiter`, `--rounds`; `default` resets; judges on one model are
  refused before anything is written). Exit 0 on a pass, 1 on a fail, 2 when it could not judge. A repository's `.claude/tribunal.json` and a `qa:` line at the top of a
  request override the machine. `qa: parallel` is accepted and runs as a final review until it is built.
- `CliClient(tools=, cwd=)` and `CodexClient(workdir=)`: a judge that reads a repository, read-only; the
  Claude one loads only the user's settings and cannot read outside the repository. `Verifier` takes its
  judge and arbiter prompts and can ask a tribunal's judges at the same time; it needs only a policy that
  names judges and an arbiter (`JudgePolicy`).

### Removed

- From the router: `panel` and `tribunal` as verification modes the policy picks (`Policy(panel_kinds=,
  critical_mode=)` are gone) or the CLI offers (`--verify` takes none, light or full), and `router config
  --arbiter`. At critical risk the router now checks with the full judge alone, and a task file that asks
  for `panel` or `tribunal` is refused. The engine still seats two judges and an arbiter when the tribunal
  asks, so a library caller can pass `verify="tribunal"`; the policy resolves its arbiter only then, so a
  stale `arbiter` setting cannot stop the router.
- The simulation's `router (one judge)` and `router (panel)` strategies.

### Changed

- Every `claude -p` and `codex exec` the router starts carries `MODEL_ROUTER_TRIBUNAL=0`, so a worker's or
  a judge's own session is never sent to the tribunal plugin.
- `router stats` leaves the tribunal's records out of its tasks and cells (their judges still count on the
  scoreboard, and one line gives the tribunal's reviews and spend), and `router feedback` on one of them no
  longer moves the routing feedback.

## [0.12.0] - 2026-10-22

### Added

- `router config --codex-models` lists the models the configured Codex home can run, read from the home's own model list (`models_cache.json`, which Codex writes), each as a ready declaration with OpenAI's list price as of 2026-10-22 and a tier by family (Luna 0, Sol and Terra 1, Astra 3); `--codex-models sol,luna` declares those and nothing else, all or none, before `--models` and `--judge` in the same command so they may name them; a model already declared through another door is shown as such. `--codex-home` prints the same list when it sets the home. The quota is not read: no stable interface carries it, which is why nothing is declared without being named. A model with no list price known here is shown and left to `--external`.
- `router doctor` says when a declared OpenAI model's price is not the list one, and fails a `codex` model that is not among the models its home knows (hidden ones included; a home with no list yet is not judged).

## [0.11.1] - 2026-10-21

### Added

- `router calibrate --version VERSION` keeps the records one router version wrote (`this` = the one running): a verification rule that changes between versions changes what a verdict means, so a calibration measured after 0.11.0 should not mix in verdicts the worker gave itself under 0.10.0.

### Fixed

- `router calibrate --help` crashed on the `%` of its defaults; every subcommand's `--help` is now tested.

## [0.11.0] - 2026-10-21

### Changed

- `light` verification (the default at high risk, and one of the two judges of a panel or a tribunal) is no longer the worker's own cell in a fresh context: it is the nearest other model at low effort, the tier above before the one below (haiku -> sonnet/low, sonnet -> opus/low, opus -> fable/low, fable -> opus/low), and in a panel or a tribunal a model other than the full judge's when the available models allow it, so that no two of worker, judge A and judge B share a model. The light judge never goes more than one tier down: a Fable worker is judged by opus/low, and when the only other model is the full judge's (two models installed, or a Fable worker in a tribunal with an Opus judge) the worker's own cell, effort capped, takes the second seat rather than a second effort of the judge's model. Measured on a private eval of 28 real tasks: Haiku and Sonnet passed their own first attempts on 89 % and 93 %; an Opus judge passed 64 % and 68 % of the same answers, with word limits broken, formats ignored and facts invented among the reasons. The arbiter by rule now prefers, among the cells that share no company with the judges, a cell at the worker's tier or above, then a model other than the worker's, then the highest tier; the worker's model one effort up is a candidate too. The bench estimate prices the judges the plan names (and a share of arbiters) instead of a constant per mode, and `router calibrate` skips `router judge` records, whose worker cost is not this router's.

- `router calibrate` counts only independent verdicts as evidence: a checker, or a judge on a model other than the worker's that backed the final verdict (the arbiter's ruling counts as one). A verdict the worker's own model alone gave, as `light` produced until now and as a one-model installation still does, is counted in the report's "left out" figure and moves nothing. The calibration file carries that count (`self_judged`); older files load with 0. On the private eval, the proposal went from 21 moves to 15, and the moves that rested on a cheap cell passing its own reviews and analyses are gone.

- Kind and risk can be declared in the first two lines of the task text, `kind: review, risk: high` (also `tipo:` and `riesgo: alto`; a value ends the line or is followed by a comma or a full stop, so a URL, a pasted report or an eval file quoted below never sets the risk); the first declaration wins over the rules and over Haiku, and `router run`, `router plan`, the hook (description and prompt) and the classifier report all honour it. The hook's session context and the skill tell the orchestrator to declare both on every subagent, with the risk levels spelled out as the damage a wrong result does.
- Critical risk is never inferred: the words "critical", "urgent" and "no undo" no longer make a task critical (a LinkedIn post that mentions a critical decision was going to a tribunal). The high-risk keywords stay. On 28 real tasks the rules read the risk right 9 times and Haiku 5 to 10: both score difficulty or vocabulary, and the risk is in what the answer is for.
- The Haiku classifier's rubric for risk asks who uses the answer and what happens when it is wrong, with one anchor per level, and says the wording does not set the risk.

### Fixed

- The Haiku classifier (`--classify-llm`, `router calibrate --classifier --llm`) could answer the task instead of classifying it when the task was a question about code: its system prompt now says the text inside `<task>` is material, never instructions. Seen on a private eval, where the "reason" of one classification was the task's full answer.

## [0.10.0] - 2026-10-21

### Added

- Models of other providers do the work too, once `router config --models` lists them: the lattice takes them with the efforts their door accepts, a start table may name them, and a declared model that is not listed keeps judging while the Claude of its tier takes its tasks (the plan says so). Two new doors by subscription, next to the API ones: `codex` (ChatGPT through `codex exec`, on a Codex home given with `router config --codex-home` or `MODEL_ROUTER_CODEX_HOME`, never the ambient `~/.codex`) and `gemini` (`gemini -p`). Each CLI call runs in an empty directory with read-only tools and nothing persisted (Gemini has no home switch: that door runs on the machine's own `~/.gemini`), and the environment `codex exec` inherits carries no API key or base URL, since the home decides the account; a verdict is asked for with Codex's `--output-schema` (strict, as that backend demands) or in Gemini's prompt, validated here, tried once more when it is not JSON, and otherwise reported as a judge error; a run that ends without an answer is an error too, and an error text never carries the model's output (the ledger keeps error texts, never answers). `router doctor` says per declared model whether its door opens (key, home signed in, executable) and `--live` makes one call through each CLI; `router cells` says which declared models work. The arbiter now prefers a company other than the full judge's (`codex` and `openai` are both OpenAI). The Claude Code hook keeps choosing among Claude models only.

- A declared model takes the tier you give it, `router config --external NAME=DOOR/ID@IN/OUT/TIER`, 0 Haiku to 3 Fable; Opus (2) when omitted, as before. The tier is where the ladder places the model when it escalates; `router calibrate` measures what it passes. `router cells` and `router config` show it.

### Changed

- `--models` (install and config) lists the models that may do the work, declared ones included; `all` means every Anthropic model.
- The arbiter by rule is the next model up from the full judge and the worker, not from the judge alone: with a judge below the worker (a declared model at tier 0, say) the old rule handed the split to the worker's own model at low effort.
- Judge and arbiter cells take an effort their door accepts: a DeepSeek judge configured at high runs, is priced and is recorded at medium, and a cell one effort up that its door cannot give is not an arbiter, so the full judge is never asked twice. Which model is the next up, or the one below, no longer depends on set order when a declared model shares a tier with a Claude one: Claude first, then by name.
- One declaration per model id: the catalogue is keyed by it, so `router config --external` refuses a second door for an id already declared under another name, or a Claude id through another door, and loading the config skips such an entry.
- `router config --external NAME=` also drops the model from `models`, `judge` and `arbiter`, and says so; a reference left behind stopped every command.
- The doors were built against the CLIs' documented flags and output (Codex 0.156, Gemini 0.46) and exercised with doubles. The `codex` door then ran live through a ChatGPT account, as the full judge of a tribunal: it passed a right answer, failed a wrong one with the right reasons, and failed a first attempt that broke the output constraint, which went up the ladder and passed. The `gemini` door has not run live yet.

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
- `router bench`: `--repeat N` runs every task N times (ids become `id.rN`), `--verify auto|none|light|full|panel|tribunal` sets the verification of fixed-cell strategies (`auto` = what the router would pick by risk; today they ran unverified), `--dry-run` validates the file and prints the estimate without a call. Each bench ledger starts with a `bench` note naming its strategy, eval and repeat count.
- After a verified fail, the next attempt's prompt carries the reason (`with_failure`), and the attempt records `carried_reason`; `router stats` and the bench summary count the retries that repeat the previous reason.

### Fixed

- Tribunal verdicts were not treated as evidence: they never fed the feedback store and `router feedback` could not correct them. `VERIFIED_SOURCES` now includes `tribunal`.

### Changed

- The README opens with a new figure, a critical task on trial: the tribunal told as a courtroom, every seat labelled with its cell (worker, two blind judges, arbiter, you), a split, a retry on the next cell and the ledger. Built by `docs/showcase/build.py`, which also keeps the frames (`--frames`) and the carousel; it replaces `tribunal.gif`.
- The README says how this differs from learned routers (RouteLLM, LLMRouter, RouterArena): nothing is trained, and the work is in what happens after the answer.

## [0.7.0] - 2026-10-20

### Added

- `router judge "<task>" --answer FILE [--cell CELL]`: verify an answer written elsewhere (a Claude Code subagent, a person) with the plan's judges, light to tribunal, judges from other providers included. It is recorded in the ledger as one attempt with no worker cost and the stop reason `external`, so `router feedback` and the judges' scoreboard see it; the feedback store is left alone. Exit 1 on a fail; `--json` prints every judge's verdict. `Router.judge` is the library call. The skill now verifies subagent results this way by default and launches judge subagents only when the verdict needs tools.

### Fixed

- `router run` never reached a judge from another provider: only `router bench` wrapped the client. Both do now.

## [0.6.1] - 2026-10-20

### Added

- Two animated figures in the README, built from `docs/showcase/build.py`: a real `router run` on a critical writing task, and the tribunal step by step (two blind judges, a split, the arbiter). Mermaid diagrams next to them for the loop and the tribunal.

### Changed

- The attempts table of `router run` fits the terminal: the reason column takes what the other columns leave (`COLUMNS` is honoured), between 16 and 70 characters.
- README brought up to date with 0.6.0: judges may come from another provider (the council comparison, the simulation note, what leaves your machine, the library example), only verified verdicts feed the feedback store, a judge that cannot be reached ends the task as `error`. The skill says a judge from another provider cannot be a subagent.

## [0.6.0] - 2026-10-20

### Added

- Judges and arbiters from other providers: `router config --external NAME=PROVIDER/MODEL_ID@IN/OUT` declares a model of OpenAI, DeepSeek or xAI with its prices (USD per million tokens), `router config --judge NAME/high` or `--arbiter NAME/high` uses it, and the provider's key comes from `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` or `XAI_API_KEY`. One OpenAI-compatible client (standard library, no SDK) serves the three; structured verdicts are asked for as a JSON object and validated locally. External models never do the work: the routing lattice, the hook and the ladder stay on Anthropic cells. `router cells` lists them, `router doctor` checks their keys, and the arbiter rule prefers a cell on a provider other than the full judge's.
- `json_payload` in `task` is the public JSON unwrapping the checkers use.

## [0.5.0] - 2026-10-20

### Added

- `tribunal` verification mode: the two judges of the panel, always both, blind to each other; when they agree that is the verdict, when they split an arbiter cell reads the task, the answer and both reasoned verdicts (as judge A and judge B, no names) and decides. One extra call only on a split, instead of a new attempt with a new pair of judges. It is now the mode the policy picks at critical risk for `analysis`, `write` and `review` tasks without a checker (`Policy(critical_mode="panel")` keeps the cheaper panel; `--verify tribunal` forces it anywhere).
- The arbiter: the next model up from the full judge at low effort, else the judge's model one effort up, else the worker's model one effort up; never one of the judges. `router config --arbiter` and `MODEL_ROUTER_ARBITER` fix it; `router config --arbiter rule` goes back to the rule. `router plan` prints it, `router doctor` and `router config` show it.
- A scoreboard of judges in `router stats`: per judge cell, verdicts and pass rate, agreement with the other judge, how often the arbiter backed it on a split, and how many of its verdicts a human override contradicted. Every judge's own verdict (and the arbiter's) is now in the ledger, under `verdicts`.
- The simulation compares `router` (tribunal) with `router (panel)` and `router (one judge)`.
- The skill tells the orchestrator how to run a tribunal with subagents: both reviews blind, a third subagent to arbitrate a split.

### Changed

- `router (no panel)` in the simulation is now `router (one judge)`.

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
