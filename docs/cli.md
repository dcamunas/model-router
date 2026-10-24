# Command line and library

[← README](../README.md)

The `router` command and the Python modules other packages import. Jump to [Commands](#commands) or
[As a library](#as-a-library).

## Commands

```bash
# Where the router would start a task, at no cost
router plan "Review this SQL migration script before it goes to production"
#   classification: review / high  (rule 'review'; risk: high-risk keyword)
#   start: opus/medium  -> Claude Code {'model': 'opus', 'effort': 'medium'}
#     - table review x high -> opus/medium

# Only model/effort, for the frontmatter of a Claude Code agent
router plan "classify these tickets" --claude-code
#   model: haiku
#   effort: low

# The options of a workflow agent() call; no text needed when kind and risk are given
router plan --kind search --risk low --workflow
#   {model: 'haiku', effort: 'medium'}

# Advice for the agents whose model and effort are fixed by hand, in this repository
router agents

# Models over their usage limit and the exact moment each resets; --check asks now (a refused question is free)
router limits --check fable

# The hook's decisions and what each measured subagent spent
router stats

# The seven-day window day by day and, once the status line has seen 20 rises of it, which weighting of tokens
# the window follows
router usage --days 7

# The status line command: reads the JSON Claude Code sends on stdin, prints one line, keeps a sample
router statusline

# The models it may use, the judge for critical reviews on the chat's model, the read-only agent
router config --models haiku,sonnet,opus --judge opus/xhigh --lean on

# The other profiles signed in to the same account, which `router usage` reads too
router config --usage-profiles ~/.claude-other

# Is everything in place: claude, the plugin, the settings, limits, floors and state
router doctor
```

Exit codes: 0 when the command did its job, 1 when a check of `router doctor` failed, 2 when the command could
not run, with one line on stderr saying why.

`router doctor --live` and `router limits --check` are the only commands that call Claude (the status line runs
the second one in the background for Fable's weekly window). They do it through
`claude -p` on the Claude profile in use (`CLAUDE_CONFIG_DIR`, else `~/.claude`), so a work profile never spends
a personal account's quota. `router doctor` names the profile.

## As a library

The router has no Python API of its own to drive. Its modules are shared with
[model-tribunal](https://github.com/dcamunas/model-tribunal), which builds its judges on them and imports these
names:

| Module | Names |
|---|---|
| `model_router.task` | `Task`, `KINDS`, `RISKS`, `json_payload` |
| `model_router.client` | `ClaudeClient`, `ClientUnavailableError`, `LiveClient`, `Completion`, `NON_STREAMING_MAX_TOKENS` |
| `model_router.cli_client` | `CliClient`, `DEFAULT_SYSTEM` |
| `model_router.verify` | `ask`, `judge_prompt`, `JUDGE_SYSTEM`, `JudgeVerdict`, `VerifyResult`, `Verdict` |
| `model_router.catalog` | `MODELS`, `ModelSpec`, `register`, `unregister`, `Cell`, `Usage`, `parse_cell`, `available_models`, `nearest_available`, `nearest_effort`, `UnknownModelError` |
| `model_router.classify` | `head`, `classify_rules` |
| `model_router.hook` | `main_model` |
| `model_router.paths` | `claude_profile`, `read_json`, `merge_json`, `read_jsonl`, `parse_jsonl`, `append_jsonl`, `replace_file`, `locked`, `is_child` |
| `model_router.fmt` | `money`, `table` |
| `model_router.clock` | the module |

`ask` puts one judge cell to one answer and returns its verdict. A client is any object with
`complete(cell, prompt=..., ...)`, so a caller can be tested without a network
(`model_router.testing.FakeClient`).
