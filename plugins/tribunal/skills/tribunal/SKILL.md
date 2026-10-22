---
name: tribunal
description: A Claude judge and a Codex judge, blind to each other, with an arbiter on a split. The plugin's hook reviews the code each turn changes on its own; use this skill when the user asks to "pass it through the tribunal", "pass it through Codex", "review it with Codex", "QA this change", to review a critical text written elsewhere, to run it by hand on a branch, or to change how it behaves in a repository.
---

# Tribunal: QA of code changes

What already happens on its own: when a turn changes code in a git repository, the plugin's `Stop` hook
judges that change against the user's request before the turn ends. Two judges read the brief, the diff and
the changed files, each in its own session, blind to the other: a Claude model other than the one serving
this chat, which may also read the repository, and a Codex model (ChatGPT), which reads nothing but that. If they disagree, an arbiter reads both verdicts and
decides. A pass shows one line. A fail comes back to you as the reason to keep working.

## When the tribunal fails your change

- Read every finding against the code. Fix the ones that are right.
- Say in a sentence what you changed, and which findings you think are wrong and why. Do not fix what is
  not broken to please a judge.
- Finish the turn. The hook judges the new state; a turn gets two reviews by default, and a fail in the
  last one is reported to the user and the turn ends.

## Asking for it

- By hand, on uncommitted work or against a branch:

  ```bash
  tribunal qa --brief "what the change was meant to do"          # uncommitted work in this repository
  tribunal qa --base main --brief-file brief.md                  # everything since main
  tribunal qa --dry-run                                          # who would judge, and which files
  tribunal judge "the task" --kind write --risk critical --answer text.md   # a text written elsewhere
  ```

  Exit code 0 on a pass or when there is nothing to judge, 1 on a fail, 2 when it could not run.
  Every verdict goes to the ledger, so `router stats` shows the judges' scoreboard.

- Per request: a line `qa: off`, `qa: final` or `qa: parallel` in the first two lines of the request.
  `parallel` (Claude and Codex both building, then comparing) is not built yet and runs as `final`.

## Settings

Later wins: the machine, the repository, the request.

```bash
tribunal config --codex terra/high --rounds 2              # this machine; `default` goes back
tribunal config --codex-models                             # which Codex models this account can run
```

`.claude/tribunal.json` in a repository: `{"mode": "off"}` to leave it out, or any of `mode`, `claude`,
`codex`, `arbiter`, `rounds`.

Not judged: a turn that changed nothing, or only documentation (`.md`, `.rst`). The settings are
taken when the request arrives: nothing written during the turn changes how it is judged. If another
session edited the same repository during the turn, a fail is reported, not sent back. A judge that
cannot be reached (a quota, the network) is reported and never blocks. `MODEL_ROUTER_TRIBUNAL=0` turns
the hook off for a session.
