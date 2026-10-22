# QA of each turn in Claude Code

[← Tribunal README](../README.md)

What the plugin's hook does at the end of every turn that changes code, and how to set it per machine,
repository or request.

## How a turn is judged

When a turn changes code in a git repository, the plugin's `Stop` hook judges that change against
your request before the turn ends.

- **What the judges get.** Your request, the diff and the changed files as they are now. The Claude
  judge also reads inside the repository, read-only, so it can open callers and tests; the Codex judge
  reads nothing else (see [What reaches Codex](privacy.md)).
- **What happens next.** A pass is one line. A fail goes back to Claude with the findings, and Claude
  fixes what is right and says why it disagrees with the rest. A turn gets two reviews by default; a
  fail in the last one is reported to you and the turn ends.
- **What counts as the change.** What the working tree became since your request. The hook snapshots
  the tree when the request arrives, through a temporary git index, so your index and your files are
  not touched, and a commit made during the turn is still part of it.
- **What is not judged.** A turn that changed nothing, or only documentation (`.md`, `.rst`). A judge
  that cannot be reached, a spent quota for instance, is reported and never blocks.
- **No switching itself off.** The settings are taken when your request arrives, so nothing the work
  writes during the turn can turn its own review off.
- **Other sessions.** If another Claude Code session edits the same repository during the turn (its
  transcript shows an editing tool), its edits are in the diff too, so a fail is reported to you and
  not sent back. A session that only reads does not count.

## Settings

```bash
tribunal config                                          # what is set, and who would judge
tribunal config --codex terra/high --arbiter sonnet/high --rounds 2   # rounds: reviews per turn
tribunal config --mode off                               # this machine; `default` goes back
```

A repository can set its own in `.claude/tribunal.json`, for instance `{"mode": "off"}`, and a
request can say `qa: off` or `qa: final` in its first two lines. The later one wins: machine,
repository, request.
