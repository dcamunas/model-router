# Inside Claude Code

[← README](../README.md)

What the plugin does to the subagents Claude launches: the model and effort it gives them, the read-only
agent, what it measures, and what it learns per repository.

A subagent's bill is mostly its context, read again on every turn: the tools, the MCP servers, the
skills and the conversation so far. A review subagent on Opus measured in this repository took 36
turns and read 3.6M cached tokens: 1.43 USD at list price for its context alone. So the plugin
works on the model, the agent and the measurement of every subagent, and learns from what it measures.
All of it on Claude models.

## Model and effort

`PreToolUse` on the Agent tool sets `model` and `effort` for every
general-purpose subagent launched without a model, from the kind and the risk declared on the first
line of the prompt (`kind: review, risk: high`), else inferred from the text, and from the active
calibration. A critical review never runs on the model serving the chat.

## The read-only agent

Reading work goes to `model-router:reader`, a read-only agent with Read, Grep, Glob, WebFetch and
WebSearch: no edits, no shell, no MCP servers.

- **Why.** The same review of one file, on the same model and effort, cost 0.0029 USD of context on the
  general agent and 0.0009 USD on the read-only one: 70 % less, with a quarter of the context written
  to the cache.
- **How a subagent gets there.** `access: read` on the first line of the prompt, on a line of
  declarations only (`kind: review, risk: low, access: read`). Anything else keeps the general agent.
- **Why it does not guess.** A test of 71 realistic write requests found half of them phrased like
  reading work ("find where the timeout is set and set it to 30"). Whoever launches the subagent knows
  whether it writes, and the session context asks Claude to say so.
- **When it cannot do the task**, the reader says so and asks to be relaunched with `access: write`.

## Measurement

`SubagentStop` reads each finished subagent's transcript and records its model,
turns, tokens (cache reads and writes included), peak context, tool calls and duration, next to the
routing decision. `router stats` shows it per agent and model. Context tokens are exact; output
tokens are a lower bound (a transcript keeps the usage taken when each message began), so the cost
shown is a floor, and almost all of it is context anyway.

## Learning per repository

The router learns, per git repository, where each kind of task should start.

- **The signal is a relaunch.** When Claude launches the same task again (same kind and risk, an alike
  prompt, same session, within 30 minutes) on a bigger model or with more effort, the first launch
  counts as a fail. Moving from the read-only agent to the general one does not: the model was not
  the problem.
- **No text is kept.** Each measurement carries its repository and a signature of its prompt: sixteen
  salted numbers.
- **What counts.** Only launches where the router chose both model and effort, from the last 13 whole
  weeks.
- **Up on its own.** If the cell a kind and risk starts on fails in 2 of 6 launches or more (30 % at
  least), it starts one step up in that repository from the next session, and Claude Code says so.
- **Down only by hand.** After 10 launches without a fail, `router project` proposes the cell it was
  raised from, or one at least 20 % cheaper, and `--apply` applies it. The risk floors always hold.
- **Reset.** The tuning lives in `~/.model-router/projects/`. `--reset` or `--apply` restart the count,
  so what was already weighed does not move it again.

Alone, the numbers grow slowly: six launches of one kind on one start cell in one repository before
anything moves. That is on purpose; a tuning built on two runs would be noise.

## Shared with your team (optional)

One person's numbers grow slowly; a team can pool them. `router project --share` creates
`.claude/model-router/` in the repository. Once that directory is committed, every teammate's router
adds one small file there per closed week, never changed afterwards:

```json
{"v":1,"c":{"review|high|opus/medium":[12,1]}}
```

- **What is in it.** Launches and fails per kind, risk and start cell. No prompts, signatures, paths,
  names, costs or times. The file name is a salted hash plus the week.
- **No double counting.** Each router counts the others' files with its own log, never its own files.
  The same rules apply: weeks before a reset do not count, and one installation weighs at most 30
  launches per cell, at its own fail rate.
- **No raise on others' numbers alone.** A raise happens on its own only with at least one fail on
  your machine too; otherwise `router project` proposes it.
- **Git-friendly.** Files never change, so branches and merges do not fight over them. Any router
  deletes a file a week after it leaves the 13-week window. Links in that directory are never followed.
- **Opting out.** `MODEL_ROUTER_SHARE=0` keeps one installation out. Git still records who committed
  each file, so anyone who looks can see weekly counts per person.

## Commands

```bash
router stats                 # ... then "Claude Code subagents, measured from their transcripts"
router project               # this repo: routed subagents, relaunches, tuning in force, cheaper starts proposed
router project --apply       # apply the proposed changes; --reset removes the project's tuning
router project --share       # pool counts with the team through .claude/model-router/ in the repo
router config --lean off     # keep every subagent on the agent Claude chose
```

## What else the plugin does

`SessionStart` tells Claude the hook exists and what to declare. The skill `router` covers what a
hook cannot do: verify a subagent's result, escalate, and send text-only subtasks through `router
run`, which carries no session context at all. The hooks call no API, take a few tenths of a second
and never block: on any error they exit quietly. Agents defined in `.claude/agents/*.md` and forks are
left alone; they are still measured.

## Environment variables

| Variable | Effect |
|---|---|
| `MODEL_ROUTER_HOOK=0` | Turns the hooks off without uninstalling. |
| `MODEL_ROUTER_LEAN=0` | Keeps subagents off the read-only agent. |
| `MODEL_ROUTER_SHARE=0` | Keeps this installation out of a repository's shared counts. |
| `MODEL_ROUTER_HOOK_QUIET=1` | Removes the one-line notice. |
| `MODEL_ROUTER_HOOK_LOG=0` | Stops logging decisions and measurements, and with them all learning. |
| `MODEL_ROUTER_UV` | Tells the plugin where `uv` is. |

Decisions go to `~/.model-router/hook.jsonl` and measurements to `~/.model-router/subagents.jsonl`.
