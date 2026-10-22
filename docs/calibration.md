# Calibration and assumptions

[← README](../README.md)

The numbers the router starts from are assumptions. This page says how to replace them with
measurements on your own tasks, and which ones they are.

## Calibrate on your tasks

You need an evaluation set of your own: 20 to 30 real tasks, frozen, with a checker or a reference
answer. [`evals/v1.yaml`](../evals/v1.yaml) is a starter set of fifteen made-up tasks (ten with a
checker, two critical ones without, so a judge decides): copy it and replace the tasks with yours.
The task file format is in [Command line and library](cli.md#commands).

```bash
router bench tasks.yaml --matrix --repeat 3 --max-usd 20   # every cell on its own, verified as the router would
router calibrate                                           # the start table the measurements support
router calibrate --apply                                   # make it the table that decides
router bench tasks.yaml --strategies "router@builtin,router"   # before and after, on the same eval
```

Until then, the only reliable number the router gives you is the real cost per solved task in the
ledger.

## What `router calibrate` does

- **Measures**, from your benches and your ledger: the first-attempt pass rate per kind and cell with
  its 95 % interval, the mean cost of an attempt and the expected cost per solved task.
- **Proposes a table, on evidence only.** A change needs a cheaper cell that was measured and passes,
  or the current cell measured and beaten; six first attempts at least; and the risk floors hold.
- **Counts only independent verdicts.** A checker, or a judge on a model other than the worker's that
  backed the final verdict. A verdict the worker's own model alone gave is reported and left out,
  because a cheap cell passes itself.
- **`--apply` makes it the table that decides.** `router plan` and `router doctor` name it, and every
  record carries its id.
- **Moves where a task starts, not the ladder.** Escalation still climbs by the capability below.
- **Ships empty.** No calibration comes with the package; the only measured numbers are yours.

## Assumptions (read them before you believe a number)

- **Prices** in `catalog.py`, Anthropic API as of 2026-10-06. The ledger uses the real `usage`.
- **`EFFORT_OUTPUT_MULT`**: output tokens per effort relative to `high` (0.5 / 0.75 / 1 / 1.6 / 2.5).
- **`CAPABILITY_TIER` + `CAPABILITY_EFFORT`**: capability 0-1 per cell. Haiku 0.40, Sonnet 0.60,
  Opus 0.75, Fable 0.90; effort adds between −0.06 and +0.05. It is an opinion, not a measurement;
  `router calibrate` prints the measured rates next to it.
- **The simulation** adds difficulty per kind, a 15 % hard tail, a `light` judge with 10 % false
  passes and a `full` one with 3 %, and judges that share their draw half the time.
- **Prompt caches are per model**: every model change in the ladder loses the cache. For a long
  conversation, change cells between conversations, not inside one.
