---
name: router
description: Verify and escalate subagent work by risk with the model router (`router` on the command line). The hook already picks model and effort on its own; use this skill when the user says "use the router", "verify this", "make it cheap but verified", or when a subtask is high or critical risk and a second subagent has to check it.
---

# The model router inside Claude Code

What already happens on its own: a `PreToolUse` hook on the Agent tool sets `model` and `effort`
on every general-purpose subagent you launch without `model`, from the kind and the risk it infers
from the prompt. What this skill adds is the rest of the loop: verify by risk and escalate on failure.

## Plain text: `router run`, not a subagent

A Claude Code subagent drags the whole session prompt along (tools, MCP, skills: about 100K tokens).
For a subtask that needs no tools (classify, extract, summarise, draft, review a text you already
have) use the router directly: about 900 tokens of context, model and effort by kind and risk, an
optional checker, a judge by risk (at critical risk for analysis, writing and review, a tribunal: two
judges blind to each other and an arbiter if they split), and an entry in the ledger.

```bash
router run "<task>" --kind <kind> --risk <risk>              # the answer goes to stdout
router run --file brief.md --check-json name,date,amount     # programmatic checker
router run "..." --json                                      # answer + attempts as JSON
```

It uses the Claude account of the profile recorded in `~/.model-router/config.json` (written by
`router install` or `router config`), with no API key. If `router` is not on the PATH or anything
fails, `router doctor` says so.

## When to step in yourself

- Do not set `model` on the Agent unless the user asks for it or the task is high risk. If you set
  it, the hook keeps it.
- Agents defined in `.claude/agents/*.md` do not go through the hook: their frontmatter rules.
- High or critical risk (production, money, customers, deletions, legal): ask for the plan and follow
  its verification. The hook does not do this part.

## Steps for a subtask with risk

1. Plan, at no cost:

   ```bash
   router plan "<the subtask in one sentence>" --kind <kind> --risk <risk>
   ```

   Kinds: classify, extract, summarize, search, chat, write, code, review, analysis, agentic.
   Risks: low, medium, high, critical. It prints the start cell, the ladder, the verification mode,
   the verifier (two cells for a panel or a tribunal) and the arbiter of a tribunal.

2. Launch the subagent with the `model` and `effort` of the start cell and the full brief.

3. Verify as the plan says:
   - `none`: accept.
   - `light`: check the result against the brief yourself, or with a checker (tests, valid JSON).
   - `full`: launch **another** subagent on the verifier's cell, fresh context, with the brief and
     the result, and ask it for `passed / reason / issues`. Never the subagent that did the work.
   - `panel`: the same, twice, on the two cells the plan prints, cheaper one first. Each judge gets
     the brief and the result in a fresh context and never sees the other's verdict. If the first
     fails, do not launch the second. The result passes only when both pass.
   - `tribunal`: both judges, always, each in a fresh context and blind to the other. If they agree,
     that is the verdict. If they split, launch a third subagent on the arbiter cell the plan prints
     with the brief, the result and both verdicts (as judge A and judge B, no model names) and ask it
     to decide for itself; its verdict is final. Say in the final message who split and who was
     backed.
   - A judge from another provider (declared with `router config --external`) is not a Claude Code
     model, so it cannot be a subagent: for that seat use the Anthropic cell the plan prints for the
     other judge, and know that the cross-provider check only happens in `router run`.

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
- The hook's decisions are kept in `~/.model-router/hook.jsonl`; `router stats` summarises them.
