# Install

[← README](../README.md)

The router is a Claude Code plugin. It needs Claude Code (2.1.275 or later) and
[uv](https://docs.astral.sh/uv/); the plugin brings its own Python environment.

## The plugin

Inside Claude Code:

```
/plugin install model-router --marketplace dcamunas/claude-plugins
```

Or from a terminal:

```bash
claude plugin marketplace add dcamunas/claude-plugins
claude plugin install model-router@dcamunas
```

The marketplace `dcamunas` lives at [dcamunas/claude-plugins](https://github.com/dcamunas/claude-plugins);
this repository is the plugin. The first session takes a few seconds longer: the hook builds its Python
environment with `uv` in the plugin's data directory (`plugins/data/model-router-dcamunas/` inside the
profile), and builds it again with every new plugin version. `uv` has to be on the machine
(`MODEL_ROUTER_UV=/path/to/uv` when it is not on the PATH).

In that first session the router also writes three keys into the profile's `settings.json`, each one only if
the file does not have it yet, and says so in one line:

- `autoCompactWindow: 400000`, so long conversations compact at 400k
  ([why 400k](claude-code.md#context-and-the-usage-windows));
- `statusLine`, which runs `router statusline` from the plugin's environment;
- `env.CLAUDE_CODE_SUBAGENT_MODEL: "sonnet"`, the model of a subagent nobody gave one. The router's choice and an
  agent's own model come first.

It writes each key once per profile and never changes a value you set: a key you edit or delete stays as you
left it, and the router does not put it back. `router doctor` says who set each one. `router config --natives
off` stops it before the first session, or for keys it has not written yet.

The plugin puts the `router` command on the PATH of the shell Claude Code runs commands in, so in a session
`router doctor`, `router config`, `router agents` or `router limits` work with nothing else installed. In a
terminal, the same command is in the plugin's environment:
`~/.claude/plugins/data/model-router-dcamunas/venv/bin/router` (alias it, or put that folder on your PATH).

Each Claude profile has its own plugin, its own `router` and its own state, in `model-router/` inside the
profile ($CLAUDE_CONFIG_DIR, or `~/.claude`): two profiles on one machine share nothing.

If your plan or your account lacks a model (no Fable, say), tell it once:

```bash
router config --models haiku,sonnet,opus
router doctor                               # claude, profile, plugin, available models, settings, state
```

The tables do not change: when they ask for a model you do not have, the router drops to the most capable
available model at the same effort and the steps up go on from there (`router plan` says so in its
explanation).

## In a team repository

To give everyone who works in a repository the same router, turn the plugin on in its shared settings and
commit them:

```bash
router project --team    # writes the two keys below into .claude/settings.json, keeping the rest
```

```json
{
  "extraKnownMarketplaces": {"dcamunas": {"source": {"source": "github", "repo": "dcamunas/claude-plugins"}}},
  "enabledPlugins": {"model-router@dcamunas": true}
}
```

Claude Code reads them once a teammate trusts the folder, but it does not install a plugin hosted elsewhere
on its own: each person runs, once, in that repository,
`claude plugin install model-router@dcamunas --scope project`, and starts a new session. They need `uv`.
`router doctor`, run in a session in the repository, says whether the plugin is installed in that profile.
Then, if you want them: [risk floors](claude-code.md#risk-floors-per-repository) in
`.claude/model-router.json` and [shared counts](claude-code.md#shared-with-your-team-optional) with
`router project --share`.

## Upgrade and removal

`claude plugin update model-router@dcamunas` takes the version the marketplace pins; the next session builds
its environment. `claude plugin uninstall model-router@dcamunas` removes it; the state folder
`model-router/` in the profile stays until you delete it. What changes in each version is in the
[CHANGELOG](../CHANGELOG.md).

## Work on the repository

```bash
git clone https://github.com/dcamunas/model-router && cd model-router
uv sync
uv run pytest -q
claude --plugin-dir .           # the plugin as it is in the repository, for that session only
```

## Credentials

The commands that call Claude (`router doctor --live`, `router limits --check`) use your Claude Code account
through `claude -p`, on the profile in use. The other commands do not call the API.
