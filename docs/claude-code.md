# Inside Claude Code

[← README](../README.md)

What the plugin does to the subagents Claude launches: the model and effort it gives them, the read-only
agent, what it measures, what it learns per repository, and what a repository can set for itself.

A subagent's bill is mostly its context, read again on every turn: the tools, the MCP servers, the
skills and the conversation so far. A review subagent on Opus measured in this repository took 36
turns and read 3.6M cached tokens: 1.43 USD at list price for its context alone. So the plugin
works on the model, the agent and the measurement of every subagent, and learns from what it measures.
All of it on Claude models.

## Model and effort

`PreToolUse` on the Agent tool sets `model` and `effort` for every
general-purpose subagent launched without a model, from the kind and the risk declared on the first
line of the prompt (`kind: review, risk: high`), else inferred from the text, and from the active
calibration. A critical review never runs on the model serving the chat. The router never picks `max` effort
on its own: `xhigh` is the top it chooses, and `max` runs only when someone names it.

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

## Risk floors per repository

The risk is what Claude declares, and Claude can forget that in one repository a migration or anything
that reaches production is never low. The repository says it once, in `.claude/model-router.json`,
committed with the code:

```json
{"v": 1, "floors": [
  {"match": "producci[oó]n|production", "risk": "critical", "why": "reaches production"},
  {"match": "\\.sql\\b|migration", "risk": "high"}
]}
```

- **Where it looks.** Each `match` is a regular expression, case-insensitive, searched in the
  subagent's description and the first four non-empty lines of its prompt, where the task is said. Material
  pasted below never sets a floor.
- **Only up.** The risk is the highest of the declared one and every rule that matches. A floor can
  make work dearer, never cheaper.
- **Visible.** Each decision records the rule that raised it; `router project` lists the rules with how
  often each fired, and `router doctor` flags a rule it cannot use (a bad pattern, an unknown risk).
- **Also outside the hook.** `router plan` and `router run` in that repository follow the same floors.
- **Safe to commit.** The file is read as a regular file, never through a link, with limits on its
  size, the number of rules and the length of a pattern. Each search has 0.05 seconds: a pattern that
  takes longer (some regular expressions take exponential time) counts as not matching, and the
  decision names it. It sits next to `.claude/model-router/`, not
  inside it, so setting floors does not turn sharing on.

## Agents fixed by hand

Agents defined in `.claude/agents/*.md` or by a plugin keep their own `model:` and `effort:`; the hook
leaves them alone. They are measured all the same, and `router agents` turns those measurements into
advice per agent and cell in the repository:

| Advice | When |
|---|---|
| measuring | fewer than 6 runs |
| raise one step | 2 runs or more repeated, and at least 30 % of them |
| lower | 10 runs or more and none repeated: the most capable cell that costs at least 20 % less and gives up little capability |
| keep | anything else |

A run counts as repeated when, in the same session and within 30 minutes of its end, another subagent
starts on an alike prompt on a more capable cell: whoever launched it was not satisfied. The same agent
again on the same cell is not a repeat, since a loop over files with one template brief looks the same.
Agents whose frontmatter says `model: inherit`, or names no model, and Claude Code's own agents get
no advice: their model is the session's. When the advice changes the model, it
prices the measured tokens at both models; a change of effort also changes the tokens, which is not
estimated. It never edits an agent: the change is yours.

## Workflows

The hook does not see the agents a workflow script spawns: they run on the session's model and effort
unless the script names them. They are measured. `router plan --kind search --risk low --workflow`
prints the options for that `agent()` call, `{model: 'haiku', effort: 'medium'}`, and the skill tells
Claude to use it.

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
router project --team        # turn the plugin on for everyone in the repo (Install, "In a team repository")
router agents                # advice for the agents fixed by hand in this repo
router config --lean off     # keep every subagent on the agent Claude chose
```

## What else the plugin does

`SessionStart` tells Claude the hook exists and what to declare. The skill `router` covers what a
hook cannot do: verify a subagent's result, escalate, and send text-only subtasks through `router
run`, which carries no session context at all. The hooks call no API, take a few tenths of a second
and never block: on any error they exit quietly. Agents defined in `.claude/agents/*.md` and forks are
left alone; they are still measured. A session opened before a plugin update keeps routing with the
environment the update installed, and never puts the older version back.

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
