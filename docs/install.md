# Install

[← README](../README.md)

Every way to install the router: as a plugin, with the CLI where plugins are blocked, in another
profile or one repository, and for working on the code. Requirements: Python 3.12 or later,
[uv](https://docs.astral.sh/uv/), and Claude Code if you want the hook or the backend that needs no
API key.

## As a Claude Code plugin (hooks, skill, read-only agent)

Inside Claude Code, one line (2.1.275 or later):

```
/plugin install model-router --marketplace dcamunas/claude-plugins
```

Or from a terminal:

```bash
claude plugin marketplace add dcamunas/claude-plugins
claude plugin install model-router@dcamunas
```

The marketplace `dcamunas` lives at [dcamunas/claude-plugins](https://github.com/dcamunas/claude-plugins);
this repository is the plugin. The first session takes a few seconds longer: the hook builds its own Python environment with `uv` in the
plugin's data directory (`plugins/data/model-router-dcamunas/` inside the profile) and reuses it
afterwards; with every new plugin version it rebuilds it on its own. `uv` has to be on the machine
(`MODEL_ROUTER_UV=/path/to/uv` when it is not on the PATH). The only change to `settings.json` is
the plugin being enabled.

If your plan or your account lacks a model (no Fable, say), or you want `router run`, install the
CLI too and tell it once:

```bash
uv tool install git+https://github.com/dcamunas/model-router@v0.18.0
router config --models haiku,sonnet,opus    # touches neither hooks nor skill
router doctor                               # claude, profile, plugin or hooks, available models, state
```

## In a team repository

To give everyone who works in a repository the same router, turn the plugin on in its shared settings
and commit them:

```bash
router project --team    # writes the two keys below into .claude/settings.json, keeping the rest
```

```json
{
  "extraKnownMarketplaces": {"dcamunas": {"source": {"source": "github", "repo": "dcamunas/claude-plugins"}}},
  "enabledPlugins": {"model-router@dcamunas": true}
}
```

Claude Code reads them once a teammate trusts the folder, but it does not install a plugin hosted
elsewhere on its own: each person runs, once, in that repository,
`claude plugin install model-router@dcamunas --scope project`, and starts a new session. They need
`uv`. The plugin alone does not put the `router` command on the PATH; whoever wants `router doctor`,
`router agents` or `router limits` installs the CLI as well
(`uv tool install git+https://github.com/dcamunas/model-router@v0.18.0`). `router doctor` run in the
repository says whether the plugin is installed in that profile. Then, if you
want them: [risk floors](claude-code.md#risk-floors-per-repository) in `.claude/model-router.json` and
[shared counts](claude-code.md#shared-with-your-team-optional) with `router project --share`.

## With the CLI, where plugins are not an option

Profiles where a policy blocks marketplaces, or one specific repository: `router install` writes
the same hooks into the profile's `settings.json` and copies the skill and the read-only agent
(as `router-reader`) into the profile.

```bash
uv tool install git+https://github.com/dcamunas/model-router@v0.18.0
router install                              # hooks + skill in the active profile ($CLAUDE_CONFIG_DIR or ~/.claude)
router install --models haiku,sonnet,opus   # without Fable
router doctor
```

The tables do not change: when they ask for a model you do not have, the router drops to the most
capable available model at the same effort and the ladder goes on from there (`router plan` says so
in its explanation). The variable `MODEL_ROUTER_MODELS=haiku,sonnet,opus` does the same without
touching the configuration.

## Another profile, one repository, or removing it

```bash
router install --settings ~/.claude-other/settings.json --profile-dir ~/.claude-other
router install --settings .claude/settings.json --no-skill
router uninstall
```

## Upgrade

`uv tool upgrade model-router` (or install another tag) and `router install` again. What changes in
each version is in the [CHANGELOG](../CHANGELOG.md).

## Work on the repository

```bash
git clone https://github.com/dcamunas/model-router && cd model-router
uv sync
uv run router install --link    # the skill is linked to the repository instead of copied
uv run pytest -q
claude --plugin-dir .           # or try the plugin as it is in the repository, for that session only
```

## Credentials

Real calls from the command line need a credential: `ANTHROPIC_API_KEY` in the environment or,
without it, your Claude Code account (see [Without an API key](cli.md#without-an-api-key-with-your-claude-account)). `plan`, `bench --simulate`,
`cells`, `stats` and `doctor` (without `--live`) do not call the API.
