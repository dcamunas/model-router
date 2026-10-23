# Command line and library

[← README](../README.md)

The `router` command and the Python API: every command with an example, the task file for benches,
running with your Claude account instead of an API key, and using it as a library. Jump to
[Commands](#commands), [Without an API key](#without-an-api-key-with-your-claude-account) or
[As a library](#as-a-library).

## Commands

```bash
# What the router would do with a task, at no cost
router plan "Review this SQL migration script before it goes to production"
#   classification: review / high  (rule 'review'; risk: high-risk keyword)
#   start: opus/medium  -> Claude Code {'model': 'opus', 'effort': 'medium'}
#     - table review x high -> opus/medium
#     - ladder: opus/medium -> fable/low -> fable/xhigh
#     - verification light (risk high); budget $2.00; max 4 attempts
#   verifier (light): fable/low

# Only model/effort, for the frontmatter of a Claude Code agent
router plan "classify these tickets" --claude-code
#   model: haiku
#   effort: low

# The options of a workflow agent() call; no text needed when kind and risk are given
router plan --kind search --risk low --workflow
#   {model: 'haiku', effort: 'medium'}

# Run it for real, with a programmatic checker
router run "Return a JSON object with the keys name, tax_id and date from: ..." \
    --kind extract --risk high --check-json name,tax_id,date

# Judge an answer written elsewhere (a subagent, a person) with the task's judges; exit 1 on a fail
router judge "Write the incident notice for ..." --kind write --risk critical --answer notice.md

# Compare strategies (offline, no API)
router bench --simulate --tasks 200 --seed 1

# Compare strategies on real tasks (spends money or quota; asks first)
router bench tasks.yaml --strategies "router,fable/low,opus/high,sonnet/medium>opus/high"

# The same three times per task, fixed cells verified as the router would, and a dry run first
router bench tasks.yaml --strategies "router,fable/low" --repeat 3 --verify auto --dry-run

# Every cell on its own (or --cells), verified as the router would: what `router calibrate` measures from
router bench tasks.yaml --matrix --cells haiku/low,sonnet/low,opus/low --repeat 3 --max-usd 20

# Measure the start table from the benches and the ledger; --apply makes it the one that decides
router calibrate
router calibrate --apply
router bench tasks.yaml --strategies "router@builtin,router"   # before and after, on the same eval

# How often the rule classifier gets kind and risk right on your eval (and Haiku, with --llm)
router calibrate --classifier tasks.yaml

# Advice for the agents whose model and effort are fixed by hand, in this repository
router agents

# Models over their usage limit and the exact moment each resets; --check asks now (a refused question is free)
router limits --check fable

# What has happened so far and what it has learned
router stats

# The SQLite index of the ledger: its state, or rebuild it from the JSON-lines files
router db
router db rebuild

# Correct a verdict by hand (it feeds the feedback store)
router feedback <task_id> --ko --note "the judge accepted a badly parsed date"
```

Format of `tasks.yaml` for the real bench (full example in [`tasks.example.yaml`](../tasks.example.yaml)):

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
`config.json` in the router's state folder (written by `router install` or `router config`), `CLAUDE_CONFIG_DIR`,
and last `~/.claude`, the profile that holds the router's state. With `MODEL_ROUTER_HOME` set and none of
the others, it refuses to start, so it never uses another profile by accident. `router doctor` says which
one it is using.

Differences from the `api` backend: no `max_tokens` and no server-side refusal fallbacks, and every
call takes one or two seconds more because of the process start-up. `--backend api|cli` forces one
or the other.

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
