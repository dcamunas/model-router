# model-router

A model router for Claude: given a task, it picks a model and an effort level, runs the task,
verifies the answer according to the risk, escalates when the answer fails, and writes down what
every solved task cost. You can use it from the command line, as a Python library and inside Claude
Code, where a hook picks model and effort for every subagent launched without one.

Status: experimental. The capability of each cell is a stated assumption, not a measurement; the
ledger and the feedback store exist to replace those assumptions with real verdicts (see
"Assumptions").

The idea comes from a reel by @donimas ("router picks model + effort · verify by risk"). Four
things are different here, and each one has its reason in the table.

## How it differs from the reel

| Reel | Here | Why |
|---|---|---|
| A ladder of models: Sonnet ×1, then Opus ×2, then Fable ×5 | A lattice of (model, effort) cells sorted by cost, with an estimated capability per cell | Anthropic's cost guidance says Fable at `low` often costs less per solved task than Opus at `high`. A ladder of models never tries that cell; the lattice does. |
| Verify "by risk" | Verify by risk with an order of trust: a programmatic checker first (free, and final when it fails), then a `light` judge (the worker's own cell, fresh context), then a `full` judge (one fixed cell, `opus/xhigh` by default, on another model than the worker); at critical risk, when no checker can decide, a `tribunal`: both judges blind to each other and an arbiter if they split, with a scoreboard of who was right | A code checker beats any judge. The `full` judge is a blind cross-check between models, and the tribunal is what is left of an LLM council once you drop the parts that do not pay. |
| "Feedback" | First-attempt pass rate per (kind, cell), kept apart from escalated attempts; it moves the start cell once there are 10 verdicts or more; a human can override it with `router feedback` | Escalated attempts only see the tasks the cheap cell already failed. Mixing them inflates the failure rate of the expensive cell. |
| "Simulated workload · real prices" | The same, but the simulation states its assumptions, includes the hard tail and the judge's false passes, and adds the honest cell the reel leaves out: Fable at low | A router that is never compared with "the best cheap model" proves nothing. |

What it does not do: it decides nothing from real data until you have real verdicts.

![router run on a critical writing task: the answer, one attempt on opus/medium judged by a tribunal of two, status solved](docs/showcase/router-run.gif)

```mermaid
flowchart LR
  T[task: kind + risk] --> P[plan: start cell, ladder, verification, budget]
  P --> A[attempt on the cell]
  A --> C{checker}
  C -- fails --> N[next cell up the ladder]
  C -- passes or none --> J{judge or tribunal}
  J -- pass --> L[(ledger)]
  J -- fail --> N
  N --> A
  N -- ladder or budget exhausted --> U[unsolved]
```

## Install

Requirements: Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and Claude Code if you want
the hook or the backend that needs no API key.

### As a Claude Code plugin (hook + skill)

Inside Claude Code, one line (2.1.275 or later):

```
/plugin install model-router --marketplace dcamunas/model-router
```

Or from a terminal:

```bash
claude plugin marketplace add dcamunas/model-router
claude plugin install model-router@dcamunas
```

The repository is both the marketplace (`dcamunas`) and the plugin (`model-router`). The first
session takes a few seconds longer: the hook builds its own Python environment with `uv` in the
plugin's data directory (`plugins/data/model-router-dcamunas/` inside the profile) and reuses it
afterwards; with every new plugin version it rebuilds it on its own. `uv` has to be on the machine
(`MODEL_ROUTER_UV=/path/to/uv` when it is not on the PATH). The only change to `settings.json` is
the plugin being enabled.

If your plan or your account lacks a model (no Fable, say), or you want `router run`, install the
CLI too and tell it once:

```bash
uv tool install git+https://github.com/dcamunas/model-router@v0.6.0
router config --models haiku,sonnet,opus    # touches neither hooks nor skill
router doctor                               # claude, profile, plugin or hooks, available models, state
```

### With the CLI, where plugins are not an option

Profiles where a policy blocks marketplaces, or one specific repository: `router install` writes
the same hooks and the same skill into the profile's `settings.json`.

```bash
uv tool install git+https://github.com/dcamunas/model-router@v0.6.0
router install                              # hooks + skill in the active profile ($CLAUDE_CONFIG_DIR or ~/.claude)
router install --models haiku,sonnet,opus   # without Fable
router doctor
```

The tables do not change: when they ask for a model you do not have, the router drops to the most
capable available model at the same effort and the ladder goes on from there (`router plan` says so
in its explanation). The variable `MODEL_ROUTER_MODELS=haiku,sonnet,opus` does the same without
touching the configuration.

Another profile, one repository, or removing it:

```bash
router install --settings ~/.claude-other/settings.json --profile-dir ~/.claude-other
router install --settings .claude/settings.json --no-skill
router uninstall
```

Upgrade: `uv tool upgrade model-router` (or install another tag) and `router install` again. What
changes in each version is in `CHANGELOG.md`.

To work on the repository:

```bash
git clone https://github.com/dcamunas/model-router && cd model-router
uv sync
uv run router install --link    # the skill is linked to the repository instead of copied
uv run pytest -q
claude --plugin-dir .           # or try the plugin as it is in the repository, for that session only
```

Real calls from the command line need a credential: `ANTHROPIC_API_KEY` in the environment or,
without it, your Claude Code account (see "Without an API key"). `plan`, `bench --simulate`,
`cells`, `stats` and `doctor` (without `--live`) do not call the API.

## Usage

```bash
# What the router would do with a task, at no cost
router plan "Review this SQL migration script before it goes to production"
#   classification: review / high  (rule 'review'; risk: high-risk keyword)
#   start: opus/medium  -> Claude Code {'model': 'opus', 'effort': 'medium'}
#     - table review x high -> opus/medium
#     - ladder: opus/medium -> fable/low -> fable/xhigh
#     - verification light (risk high); budget $2.00; max 4 attempts
#   verifier (light): opus/medium

# Only model/effort, for the frontmatter of a Claude Code agent
router plan "classify these tickets" --claude-code
#   model: haiku
#   effort: low

# Run it for real, with a programmatic checker
router run "Return a JSON object with the keys name, tax_id and date from: ..." \
    --kind extract --risk high --check-json name,tax_id,date

# Compare strategies (offline, no API)
router bench --simulate --tasks 200 --seed 1

# Compare strategies on real tasks (spends money or quota; asks first)
router bench tasks.yaml --strategies "router,fable/low,opus/high,sonnet/medium>opus/high"

# What has happened so far and what it has learned
router stats

# Correct a verdict by hand (it feeds the feedback store)
router feedback <task_id> --ko --note "the judge accepted a badly parsed date"
```

Format of `tasks.yaml` for the real bench (full example in `tasks.example.yaml`):

```yaml
tasks:
  - id: tax-id-01
    prompt: "Extract name and tax id from the following text as JSON: ..."
    kind: extract          # optional, inferred by rules
    risk: high             # optional
    check_json: [name, tax_id]
  - id: sql-01
    prompt: "Write the BigQuery query that ..."
    check_regex: "SELECT[\\s\\S]*FROM"
```

## Without an API key: with your Claude account

`router run` and `router bench` do not need `ANTHROPIC_API_KEY`. Without it they use the `cli`
backend: every call is one `claude -p` process (Claude Code's headless mode) with the cell's model
and effort, no tools, no MCP and a minimal system prompt, about 900 tokens per call (without
`--system-prompt`, Claude Code would send its full prompt, about 100K). Structured outputs go
through `--json-schema`. It spends your subscription's quota, not dollars: the amounts in the ledger
are the list-price equivalent, a proxy for quota use.

The Claude profile it uses, in order: `MODEL_ROUTER_CLAUDE_CONFIG_DIR`, `claude_config_dir` in
`~/.model-router/config.json` (written by `router install` or `router config`), `CLAUDE_CONFIG_DIR`.
With none of them it refuses to start, so it never uses another profile by accident. `router doctor`
says which one it is using.

Differences from the `api` backend: no `max_tokens` and no server-side refusal fallbacks, and every
call takes one or two seconds more because of the process start-up. `--backend api|cli` forces one
or the other.

## Inside Claude Code

Two pieces, one source of truth (the `model_router` package), delivered the same way by the plugin
and by `router install`:

- Hook `router-hook`: `PreToolUse` on the Agent tool sets `model` and `effort` for every
  general-purpose subagent Claude launches without choosing a model, from the kind and the risk it
  infers from the prompt. `SessionStart` tells Claude the hook exists so it does not fight it. It
  calls no API, takes a few tenths of a second and never blocks: on any error it exits quietly.
- Skill `router`: what a hook cannot do. Verify with a second subagent, escalate, and send text-only
  subtasks (classify, extract, summarise, draft) through `router run` instead of a subagent, which
  drags the whole session context along.

Nobody changes the model of the main session: `settings.json` or `/model` set it. What the hook does
do for the orchestrator: a review whose risk asks for `full` verification (today only `critical`)
never runs on the model serving the chat; the hook reads that model from the session transcript and
jumps to the verifier cell on another model (chat on Opus, critical review on fable/low). It is the
only verification the orchestrator itself gets. If the transcript cannot be read, or no other model
is available, the rule does nothing.

What it does not do: it leaves the agents defined in `.claude/agents/*.md` alone (their frontmatter
rules), and the forks too, and it neither verifies nor escalates by itself.

Environment variables: `MODEL_ROUTER_HOOK=0` turns it off without uninstalling;
`MODEL_ROUTER_HOOK_QUIET=1` removes the one-line notice in the UI; `MODEL_ROUTER_HOOK_LOG=0` stops
logging decisions; `MODEL_ROUTER_UV` tells the plugin where `uv` is. Decisions are kept in
`~/.model-router/hook.jsonl` and `router stats` summarises them.

## As a library

```python
from model_router import Router, Task, json_checker
from model_router.client import LiveClient

router = Router(LiveClient())
result = router.run(Task(
    prompt="Extract name, tax id and date from the text as JSON: ...",
    kind="extract", risk="high", checker=json_checker(("name", "tax_id", "date")),
))
print(result.status, result.final_cell, f"${result.total_cost_usd:.4f}")
print(result.answer)
```

`Task` takes `output_schema` (a pydantic model for structured output), `system`, `budget_usd`,
`max_attempts` and `verify` to force the mode. `Router` accepts any object with
`complete(cell, prompt=..., ...)`, so it can be tested without a network
(`model_router.testing.FakeClient`).

## How it decides

1. Kind and risk. You pass them or rules infer them (the rules know English and Spanish prompts);
   `--classify-llm` uses Haiku.
2. Start cell. The `START` table in `policy.py` (kind × risk). With 10 or more verdicts for that
   kind, the feedback store moves it up when its first-attempt pass rate falls below the break-even
   `c1 / c2` (the cost of an attempt on that cell, verification included, over the cost of the next
   step): below that, starting cheap costs more. The risk floors (`high` ≥ sonnet/low, `critical`
   ≥ opus/medium) cannot be lowered.
3. Ladder. Each step is the cheapest cell that is clearly more capable (+0.08 capability) among the
   available models. With the current assumptions: `sonnet/medium -> opus/low -> fable/low -> fable/xhigh`.
4. Verification. `low` and `medium` none, `high` light, `critical` full (full verification was the
   biggest cost sink; raise `high` to `full` with `--verify full`, or in `policy.py` once your bench
   justifies it). When the task has a checker, the checker goes first and is final when it fails.
   `light` is the worker's own cell in a fresh context; `full` is one fixed judge, `opus/xhigh` unless
   `router config --judge` or `MODEL_ROUTER_JUDGE` say otherwise, on another model than the worker
   (see below). At `critical`, an `analysis`, `write` or `review` task with no checker gets a
   `tribunal` of both instead of the full judge alone; `--verify full` forces one judge, `--verify
   panel` or `--verify tribunal` force two on any task.
5. Stopping. A passing verdict, the ladder exhausted, the budget by risk (0.10 / 0.50 / 2 / 10 USD),
   two refusals in the same task, or the API down. An answer cut by `max_tokens` is retried once on
   the same cell with twice the room.
6. Ledger. `~/.model-router/ledger.jsonl` (`MODEL_ROUTER_HOME` moves it): every attempt with its
   real tokens, cost, the model that served it (when a fallback ran), verdict and reason.

## Tribunal: two judges and an arbiter, not a council

![The tribunal, step by step: the worker answers, two judges read it blind to each other, they split, the arbiter reads both reasoned verdicts and decides, the ledger keeps every verdict](docs/showcase/tribunal.gif)

```mermaid
flowchart LR
  W[worker's answer] --> A[judge A: the worker's cell, fresh context]
  W --> B[judge B: the fixed judge, another model]
  A & B --> D{agree?}
  D -- yes --> V[verdict]
  D -- split --> R[arbiter: task, answer, both reasons]
  R --> V
  V --> S[(ledger and scoreboard)]
```

Karpathy's `llm-council` sends one question to several models, has each one rank the others'
answers and lets a chairman write the final one. With four members that is nine calls per
question, and the value comes from mixing providers. Here every model is Claude and the number that
matters is the cost per solved task, so the router keeps the one part that pays: a second,
independent opinion.

Who judges is a fixed choice, not a climb up the lattice. `full` uses one judge cell, `opus/xhigh`
unless `router config --judge` or `MODEL_ROUTER_JUDGE` say otherwise, in a fresh context. When the
worker is already on that model the judge moves to the next model up at low effort (Fable at low
for an Opus worker), or to the model below when there is nothing above, so the check stays a
cross-check between models. No judge thinks above `xhigh`; a judge at `high` writes about 60 %
less thinking than one at `xhigh`, and `router config --judge opus/high` is the cheaper setting
when verification is most of the bill. Verifying is easier than producing, so
the judge does not have to outrank the worker; it has to be independent and good enough. The
earlier rule, the cheapest cell clearly more capable than the worker, put Fable at xhigh and max in
the judge's seat for Fable workers, and one verdict cost $0.42 in the first real bench. `light` is
the worker's own cell in a fresh context, capped the same way.

The `tribunal` mode asks those two judges, always, each blind to the other. If they agree, that is
the verdict. If they split, an arbiter cell reads the task, the answer and both reasoned verdicts
(as judge A and judge B, no names) and decides for itself; one extra call, only on a split, instead
of a new attempt and a new pair of judges. The arbiter is the next model up from the full judge at
low effort (Fable at low for an Opus judge), or the judge's model one effort up when nothing is
above; `router config --arbiter` or `MODEL_ROUTER_ARBITER` fix it. When no cell other than the
judges is available, a split fails. The policy uses the tribunal on its own at critical risk for
analysis, writing and review tasks without a checker; `Policy(panel_kinds=...)` changes that list,
`Policy(critical_mode="panel")` picks the cheaper variant, and a task can force `verify="tribunal"`
or `verify="panel"` anywhere.

The `panel` mode is the same pair with the second judge skipped when the first fails and no
arbiter: a split is a fail and the task goes up the ladder. One judge call cheaper on a fail, no
scoreboard worth the name.

Every judge's own verdict goes to the ledger, so `router stats` keeps a scoreboard per judge cell:
verdicts and pass rate, how often it agreed with the other judge, how often the arbiter backed it
on a split, and how many of its verdicts you contradicted with `router feedback`. Those are the
numbers to pick a judge with, instead of the error rates the simulation assumes.

`router bench --simulate` compares `router` (tribunal) with `router (panel)` and `router (one
judge)`. The simulation assumes the
judges of one answer share their random draw half the time (`JUDGE_CORRELATION`): models of one
family err together, which is the honest reason a panel of Claude cells buys less than a council of
different providers would. Measure it on your own tasks before trusting either number.

## Judges from other providers

The judges and the arbiter can come from OpenAI, DeepSeek or xAI, which is where a tribunal earns its
keep: two providers err in different places. Declare the model with its prices, point the judge at
it, and export the key:

```bash
router config --external gpt=openai/gpt-5.5@5/30       # NAME=PROVIDER/MODEL_ID@$ per M in/$ per M out
router config --judge gpt/high --arbiter deepseek/high  # after declaring deepseek the same way
export OPENAI_API_KEY=...                                # DEEPSEEK_API_KEY, XAI_API_KEY
router doctor                                            # says which keys are set
```

The ids and the prices are yours to type, on purpose: they change, and a wrong number in the ledger
is worse than none. External models judge and arbitrate; they never do the work, the hook never sends
a subagent to them, and the ladder stays on Anthropic cells. Their cached tokens are priced at the
input rate. One client speaks to the three providers (the same chat-completions API; a structured
verdict is asked for as a JSON object and validated here), so adding a fourth is one line in
`providers.py`. Not covered yet: subscription access through a provider's CLI, as `claude -p` does
for Anthropic.

API details that are deliberate: `output_config.effort` carries the effort; `thinking` is not sent
(adaptive by default, and `disabled` returns 400 on the 5.5 family); the models that accept it get
`fallbacks="default"` so a classifier refusal is retried on the model Anthropic recommends;
structured output goes through `parse`; above 16K tokens the call streams.

## What leaves your machine

- The hook calls no API: it classifies by rules, locally. It saves every decision in
  `~/.model-router/hook.jsonl` with the first 60 characters of the subagent's prompt.
- `router run` and `router bench` send the prompt to Anthropic, through your API key or your Claude
  Code account. To nobody else.
- The ledger keeps tokens, cost, cell, verdict and reason for every attempt; it keeps neither the
  prompt nor the answer.
- `router plan`, `router stats`, `router cells` and `router doctor` without `--live` call nothing.

## Assumptions (read them before you believe a number)

- Prices in `catalog.py`, Anthropic API as of 2026-10-06. The ledger uses the real `usage`.
- `EFFORT_OUTPUT_MULT`: output tokens per effort relative to `high` (0.5 / 0.75 / 1 / 1.6 / 2.5).
- `CAPABILITY_TIER` + `CAPABILITY_EFFORT`: capability 0-1 per cell. Haiku 0.40, Sonnet 0.60,
  Opus 0.75, Fable 0.90; effort adds between −0.06 and +0.05. It is an opinion, not a measurement.
- The simulation adds: difficulty per kind, a 15 % hard tail, a `light` judge with 10 % false
  passes and a `full` one with 3 %, and judges that share their draw half the time.
- Prompt caches are per model: every model change in the ladder loses the cache. For a long
  conversation, change cells between conversations, not inside one.

## What is missing before this is a real router

An evaluation set of your own: 20 to 30 real tasks, frozen, with a checker or a reference answer,
run through `router bench tasks.yaml` with several strategies. Until then, the only reliable number
this gives you is the real cost per solved task in the ledger. `evals/v1.yaml` is a starter set of
fifteen made-up tasks (ten with a checker, two critical ones without so the panel runs): copy it and
replace the tasks with yours.

## Development

```bash
uv run pytest -q
uv run ruff check src tests   # style, types on signatures, docstrings on the public API, complexity
uv run pyright                # types
uv run --with pillow docs/showcase/build.py   # the README's GIFs, from docs/showcase/router-run.txt (a real run)
```

GitHub Actions runs the same on every push, checks that the skill ships inside the wheel and
validates the plugin with the Claude Code CLI. Everything in the repository is in English: code,
documentation and what the command line prints.

## Licence

MIT. See `LICENSE`.
