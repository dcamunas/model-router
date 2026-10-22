# Command line

[← Tribunal README](../README.md)

Reviewing a branch, or a text written elsewhere, from a terminal.

```bash
tribunal qa --brief "what the change was for"            # uncommitted work in this repository
tribunal qa --base main --brief-file brief.md            # everything since main
tribunal qa --dry-run                                    # who would judge, and which files
tribunal judge "write the incident notice" --kind write --risk critical --answer notice.md
```

`tribunal judge` is for a text written elsewhere: critical prose, an analysis, a review. `--cell`
names the model that wrote it, so it is not one of its judges.

Both exit 0 on a pass (or when there is nothing to judge), 1 on a fail and 2 when they could not
judge. Every verdict goes to the ledger: `router stats` shows the judges' scoreboard and a line with
the tribunal's reviews and spend.
