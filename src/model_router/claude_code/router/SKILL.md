---
name: router
description: Verify and escalate subagent work by risk with the model router (`router` on the command line). The hook already picks model and effort on its own; use this skill when the user says "use the router", "verify this", "make it cheap but verified", or when a subtask is high or critical risk and a second subagent has to check it.
---

# The model router inside Claude Code

What already happens on its own: a `PreToolUse` hook on the Agent tool sets `model` and `effort`
on every general-purpose subagent you launch without `model`, from the kind and the risk of the
task. Declare both in the subagent's description or on the first line of its prompt, as `kind: review,
risk: high` (a value ends the line or is followed by a comma or a full stop; only the first two lines
count, so material pasted below never sets the risk); a declared value beats what the hook infers from the text, and `critical` is never
inferred, only declared. The risk is the damage a wrong result does (low: you read it and can redo
it; medium: used without a second read; high: money, customers, production, legal, deletions,
publishing; critical: high and hard to detect), and only you know what the result is for.

Reading work goes to a read-only agent with Read, Grep, Glob, WebFetch and WebSearch only: no edits,
no shell, no MCP, and a fraction of the context per turn. Say it on the first line of every subagent's
prompt: `access: read` for work that only reads, `access: write` for anything that edits, saves, runs
commands or uses MCP tools. Only `access: read` on the first line, a line of declarations only, sends a
subagent to the read-only agent; the hook never guesses it from the wording, which is not safe.
If the read-only agent answers that it cannot do the task, relaunch with `access: write`. What every
subagent spent is measured when it ends: `router stats`. When a subagent's result is not good enough,
relaunch the same task higher (set `model`, or more effort) rather than rephrasing it: the router reads
that as a fail of its first choice and, after a few in the same repository, starts that kind of task
higher there (`router project` shows it). Keep the declaration line the same when you relaunch, apart
from `access`: a relaunch counts only for the same kind and risk.
What this skill adds is the rest of the loop: verify by risk and escalate on failure.

## When not to launch a subagent

A subagent costs two steps before it does anything: writing the brief and reading its answer, and it
starts with none of this conversation. Do the work yourself when:

- you would do it in two or three steps;
- it depends on what was said here and does not fit in a brief of a few lines.

Delegate what makes you read a lot (sweeps, logs, long files) and returns a conclusion, and work that
runs while you do something else.

## Workflows

The hook does not see the agents a workflow script spawns: they run on the session's model and effort
unless the script names them. Measured, they show in `router stats` all the same. Give every `agent()`
call its own options from the router, by the kind and risk of that stage:

```bash
router plan --kind search --risk low --workflow     # {model: 'haiku', effort: 'medium'}
```

## Plain text: `router run`, not a subagent

A Claude Code subagent drags the whole session prompt along (tools, MCP, skills: about 100K tokens).
For a subtask that needs no tools (classify, extract, summarise, draft, review a text you already
have) use the router directly: about 900 tokens of context, model and effort by kind and risk, an
optional checker, one judge by risk on another model than the worker, and an entry in the ledger.

```bash
router run "<task>" --kind <kind> --risk <risk>              # the answer goes to stdout
router run --file brief.md --check-json name,date,amount     # programmatic checker
router run "..." --json                                      # answer + attempts as JSON
```

It uses the Claude account of this session's profile (or the one `router config` recorded), with no API
key. The plugin brings the `router` command; until the first session has built its environment it says
"not ready yet", and after that `router doctor` explains anything that fails.

## When to step in yourself

- Do not set `model` on the Agent unless the user asks for it or the task is high risk. If you set
  it, the hook keeps it, unless that model is over its usage limit: then the launch is refused with
  the time it resets, and a launch without `model` goes to an available one.
- Agents defined in `.claude/agents/*.md` do not go through the hook: their frontmatter rules.
  `router agents` says, from what they spent and how often they were repeated, which look too high or too
  low; the change is the user's.
- High or critical risk (production, money, customers, deletions, legal): ask for the plan and follow
  its verification. The hook does not do this part.

## Steps for a subtask with risk

1. Plan, at no cost:

   ```bash
   router plan "<the subtask in one sentence>" --kind <kind> --risk <risk>
   ```

   Kinds: classify, extract, summarize, search, chat, write, code, review, analysis, agentic.
   Risks: low, medium, high, critical. It prints the start cell, the ladder, the verification mode,
   and the verifier.

2. Launch the subagent with the `model` and `effort` of the start cell and the full brief.

3. Verify as the plan says. The default is the router itself: save the subagent's result to a file
   and run

   ```bash
   router judge "<the same subtask>" --kind <kind> --risk <risk> --cell <start cell> --answer result.md
   ```

   It runs the plan's judge (light or full), records every verdict in the ledger for the scoreboard, prints
   the reasons and exits 1 on a fail. About 900 tokens of context per judge instead of a subagent.

   Launch judge subagents instead only when the verdict needs tools (run the tests, read the repo):
   - `none`: accept.
   - `light`: check the result against the brief yourself, or with a checker (tests, valid JSON).
   - `full`: launch **another** subagent on the verifier's cell, fresh context, with the brief and
     the result, and ask it for `passed / reason / issues`. Never the subagent that did the work.

4. On failure, relaunch on the next cell of the ladder with the reason for the failure in the brief.
   Stop when it passes, when the ladder runs out, or when the cost is not worth it.

5. In the final message say which cell solved it and how many attempts it took.

## Verifying your own design

You, the main chat, have no automatic verifier or escalation: no hook can change your model. When
the design is yours and it touches production, money, customers or deletions:

1. Launch a general-purpose subagent **without `model`** whose prompt starts with "Review" and says
   it is critical, with the brief, the full design and the request for `passed / reason / issues`.
2. The hook classifies it as review/critical and sends it to a model other than yours (chat on Opus,
   review on fable/low). You will see "cross-checked against the chat" in the notice.
3. If it fails, fix the design and repeat once; if it fails again, tell the user before running
   anything.

## Rules

- A programmatic checker beats any judge. If there are tests, the verdict is "they pass or they
  don't".
- Do not change cells inside one subagent conversation: the cache is per model.
- The hook's decisions are kept in `<profile>/model-router/hook.jsonl`; `router stats` summarises them.
