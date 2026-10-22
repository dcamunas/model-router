# Background: where the idea comes from

[← README](../README.md)

The reel the idea comes from, what this router does differently and why, and how it compares with
routers that are trained.

A reel by @donimas ("router picks model + effort · verify by risk"). Four things are different
here, and each one has its reason in the table.

| Reel | Here | Why |
|---|---|---|
| A ladder of models: Sonnet ×1, then Opus ×2, then Fable ×5 | A lattice of (model, effort) cells sorted by cost, with an estimated capability per cell | Anthropic's cost guidance says Fable at `low` often costs less per solved task than Opus at `high`. A ladder of models never tries that cell; the lattice does. |
| Verify "by risk" | An order of trust: a programmatic checker first (free, and final when it fails), then one judge on another model than the worker, with a scoreboard of who was right | A code checker beats any judge, and a judge on another model is a blind cross-check. |
| "Feedback" | First-attempt pass rate per (kind, cell), kept apart from escalated attempts; it moves the start cell once there are 10 verdicts or more; a human can override it with `router feedback` | Escalated attempts only see the tasks the cheap cell already failed. Mixing them inflates the failure rate of the expensive cell. |
| "Simulated workload · real prices" | The same, but the simulation states its assumptions, includes the hard tail and the judge's false passes, and adds the honest cell the reel leaves out: Fable at low | A router that is never compared with "the best cheap model" proves nothing. |

What it does not do: it decides nothing from real data until you have real verdicts.

Compared with learned routers: [RouteLLM](https://lmsys.org/blog/2024-07-01-routellm/) trains a router on
preference data to send each query to a strong or a weak model, [LLMRouter](https://huggingface.co/papers/2608.06867)
collects more than a dozen routers of that kind, and [RouterArena](https://openreview.net/pdf/03e5d4326116c387dbd69c38e194211db0736cda.pdf)
ranks them. This is not one of those. Nothing is trained: the start cell comes from a table by kind and risk,
and a cell is model and effort inside one provider. The difference is what happens after the answer: a checker
or a judge verifies it, a failure climbs the ladder, and every verdict feeds the table. If you want a learned
router across providers, take one of those; this one picks the effort inside Claude Code and verifies by risk.
