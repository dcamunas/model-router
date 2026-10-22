# What leaves your machine

[← README](../README.md)

What each part of the router writes on your disk and what it sends, and to whom.

- The hooks call no API: they classify by rules, locally. Every decision goes to
  `~/.model-router/hook.jsonl` with the first 60 characters of the subagent's prompt and a hash of
  it; every measurement goes to `~/.model-router/subagents.jsonl` with numbers, that hash, a
  signature of sixteen numbers (no text) and the project's path, read from the transcript Claude Code
  already keeps on your disk. A project's tuning goes to `~/.model-router/projects/`.
- In a repository that shares counts, the hook adds weekly files to `.claude/model-router/` with the
  counts above and nothing else. They leave your machine only when you commit and push them.
- `router run`, `router judge` and `router bench` send the prompt to Anthropic, through your API key
  or your Claude Code account, and to nobody else.
- The ledger and its SQLite index keep tokens, cost, cell, verdict and the judges' reasons for every
  attempt; they keep neither the prompt nor the answer, only a hash of the task. A reason may quote a
  few words of either.
- `router plan`, `router stats`, `router cells` and `router doctor` without `--live` call nothing.
