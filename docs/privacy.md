# What leaves your machine

[← README](../README.md)

What each part of the router writes on your disk and what it sends, and to whom.

The paths below are those of the default Claude profile, `~/.claude`. Each profile keeps its own folder: a
session started with `CLAUDE_CONFIG_DIR=~/.claude-other` writes to `~/.claude-other/model-router`, and two
profiles on one machine share no decisions, measurements, limits or settings.

- The hooks classify by rules, locally, and call no API, with one exception: after a subagent dies on a
  usage limit, the StopFailure hook sends the word "ok" to that model through `claude -p`, on your
  account, to read the exact reset. It goes to `~/.claude/model-router/limits.json` with the window's use: times
  and numbers, no text. Every decision goes to
  `~/.claude/model-router/hook.jsonl` with a hash of the subagent's prompt, never its text; every measurement goes to `~/.claude/model-router/subagents.jsonl` with numbers, that hash, a
  signature of sixteen numbers (no text) and the project's path, read from the transcript Claude Code
  already keeps on your disk. A project's tuning goes to `~/.claude/model-router/projects/`.
- In a repository that shares counts, the hook adds weekly files to `.claude/model-router/` with counts
  and nothing else: launches and relaunches per kind, risk and cell, and runs and repeats of the agents
  committed in the repository itself. An agent from your profile, a plugin, a link or an untracked file is
  never named there, nor is one whose name has spaces or characters beyond letters, digits, `.`, `_` and `-`. They leave your machine only when you commit and push them.
- `router run` and `router judge` send the prompt to Anthropic, through your API key or your Claude Code
  account, and to nobody else.
- The ledger keeps tokens, cost, cell, verdict and the judges' reasons for every attempt; it keeps neither
  the prompt nor the answer. A reason may quote a few words of either.
- `router plan`, `router stats`, `router cells` and `router doctor` without `--live` call nothing.
