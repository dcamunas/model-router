# Inside Claude Code

[← README](../README.md)

What the plugin does to the subagents Claude launches: the model and effort it gives them, the read-only
agent, what it measures, what it learns per repository, and what a repository can set for itself.

Paths under `~/.claude/model-router` are those of the default Claude profile; a profile elsewhere (`CLAUDE_CONFIG_DIR`) keeps
its own `model-router/` folder.

A subagent's bill is mostly its context, read again on every turn: the tools, the MCP servers, the
skills and the conversation so far. So the plugin works on the model, the agent and the measurement of
every subagent, and learns from what it measures. All of it on Claude models.

## Model and effort

`PreToolUse` on the Agent tool sets `model` and `effort` for every
general-purpose subagent launched without a model, from the kind and the risk declared on the first
line of the prompt (`kind: review, risk: high`), else inferred from the text. A critical review never runs
on the model serving the chat. The router never picks `max` effort
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
turns, tokens (cache reads and writes included), peak context and duration, next to the
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
- **Reset.** The tuning lives in `~/.claude/model-router/projects/`. `--reset` or `--apply` restart the count,
  so what was already weighed does not move it again.

Alone, the numbers grow slowly: six launches of one kind on one start cell in one repository before
anything moves. That is on purpose; a tuning built on two runs would be noise.

## Usage limits

A Claude subscription caps usage in windows: five hours, seven days, and a separate weekly allowance for
some models, Fable among them. When one is spent, every call to that model is refused until the window
resets. The router finds out from the failure and then knows the exact moment:

- **Detected.** A subagent that dies on a limit fires `StopFailure` with `error: rate_limit` (it never
  reaches `SubagentStop`), and `router run` gets the same 429 back.
- **Exact reset.** The router then asks that model for one word through `claude -p --output-format
  stream-json`, whose `rate_limit_event` carries the window and its `resetsAt`. A refused question
  reports a cost of 0. What it learns goes to `~/.claude/model-router/limits.json`.
- **Routed around.** Until that moment the model is left out of the available ones: the hook, plans,
  judges and `router run` pick the next model, and each launch's notice says until when. At the reset it
  is back with no other step. A launch that names the limited model with `model` is refused, with the
  time, so it does not fail later.
- **Told.** The session context names the limited models, `router doctor` lists them, and `router limits`
  shows each one's reset with the account's windows; `router limits --check` asks now.
- **Account-wide.** When the five-hour or seven-day window is spent, every model is refused: there is
  nowhere to route to, and the router says so.

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

In a repository that [shares counts](#shared-with-your-team-optional), the teammates' runs of the same agent
on the same cell add to yours before the thresholds apply, so the advice covers the whole team: the `team`
column says how many runs came from others. Turns, spend and savings come from your machine only. A cell
the agent's file no longer names gets no advice, so a change you made does not keep showing up.

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
{"v":1,"c":{"review|high|opus/medium":[12,1]},"a":{"migration-curator|opus/xhigh":[9,0]}}
```

- **What is in it.** `c`: launches and fails per kind, risk and start cell, for the subagents the router
  routed. `a`: runs and repeats per agent fixed by hand and its cell, for `router agents`, and only for
  agents committed in the repository's own `.claude/agents/` (a plain file in git, never a link or an
  untracked file), whose names are already there. Which agents count is read from the checkout when the
  week's file is written. No prompts, signatures, paths, people, costs or times. The file name is a salted
  hash plus the week.
- **No double counting.** Each router counts the others' files with its own log, never its own files.
  One installation weighs at most 30 launches per cell, at its own fail rate. For routing, weeks before a
  reset do not count; `router agents` takes the whole 13-week window, since it only advises.
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
router limits                # models over their usage limit, until the exact reset; --check asks now
router config --lean off     # keep every subagent on the agent Claude chose (`--lean on` undoes it)
```

## What else the plugin does

`SessionStart` tells Claude the hook exists and what to declare. The skill `router` covers what a
hook cannot do: verify a subagent's result, escalate, and send text-only subtasks through `router
run`, which carries no session context at all. A launch takes the hook about 50 milliseconds, and the
hooks call no API, with one exception: after a usage-limit failure, the StopFailure hook asks the limited model for one
word (a `claude -p` call of up to 30 seconds) to learn its reset. They block only a launch that names a
model over its limit; on any error they exit quietly. Agents defined in `.claude/agents/*.md` and forks are
left alone; they are still measured. A session opened before a plugin update keeps routing with the
environment the update installed, and never puts the older version back.

## Environment variables

| Variable | Effect |
|---|---|
| `MODEL_ROUTER_HOOK=0` | Turns the hooks off without uninstalling. |
| `MODEL_ROUTER_SHARE=0` | Keeps this installation out of a repository's shared counts. |
| `MODEL_ROUTER_HOOK_QUIET=1` | Removes the one-line notice. |
| `MODEL_ROUTER_HOOK_LOG=0` | Stops logging decisions and measurements, and with them all learning. |
| `MODEL_ROUTER_UV` | Tells the plugin where `uv` is. |

Decisions go to `~/.claude/model-router/hook.jsonl` and measurements to `~/.claude/model-router/subagents.jsonl`.
