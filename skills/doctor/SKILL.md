---
name: doctor
description: Run the model router's checks (`router doctor`) and report them. Use when the user types /model-router:doctor; pass `--live` to also call Claude once per model.
disable-model-invocation: true
argument-hint: "[--live]"
allowed-tools: Bash("${CLAUDE_PLUGIN_ROOT}"/bin/router doctor *)
---

# Router doctor

The checks of the router in this profile, just run (one line per check, `ok` or `FAIL`; `--live` asks Claude
once per model, which spends a few tokens):

```
!`"${CLAUDE_PLUGIN_ROOT}"/bin/router doctor $ARGUMENTS 2>&1`
```

Report them as they are: the `FAIL` lines first, each with the fix its own text names, then one line saying
the `ok` checks passed. Add nothing the output does not say. If there is no output or the command could not
run, say so and give the line that explains it, usually that the plugin environment is built when a new
session starts.
