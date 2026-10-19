#!/bin/sh
# Run the router hook from a virtualenv kept in the plugin's data directory.
#
# Claude Code installs a plugin by copying the repository; it installs no Python. So the first
# SessionStart (and every one after the plugin version changes) builds the venv with uv from the
# plugin root and records the version. PreToolUse never builds: with no venv it exits quietly, so a
# subagent launch is never delayed. Any failure exits 0 with a note on stderr: a hook must never
# block Claude. The event JSON on stdin is passed through to the real hook untouched.
set -u
PATH="${PATH:-}:/usr/bin:/bin"  # sed, cat and mkdir, whatever environment Claude Code was started from

root="${CLAUDE_PLUGIN_ROOT:-}"
data="${CLAUDE_PLUGIN_DATA:-}"
if [ -z "$root" ] || [ -z "$data" ]; then
    echo "router-hook: CLAUDE_PLUGIN_ROOT or CLAUDE_PLUGIN_DATA is missing; this script only runs as a plugin" >&2
    exit 0
fi

venv="$data/venv"
hook="$venv/bin/router-hook"
stamp="$data/installed-version"
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml" | head -n 1)
installed=$(cat "$stamp" 2>/dev/null || true)

if [ ! -x "$hook" ] || [ "$installed" != "$version" ]; then
    if [ "${1:-}" != "--bootstrap" ]; then
        echo "router-hook: the plugin environment is not ready yet; it is built when a session starts" >&2
        exit 0
    fi
    # MODEL_ROUTER_UV names the uv binary when it is somewhere unusual; otherwise PATH and the usual places.
    uv="${MODEL_ROUTER_UV:-}"
    if [ -z "$uv" ]; then
        uv=$(command -v uv 2>/dev/null || true)
        for candidate in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
            if [ -z "$uv" ] && [ -x "$candidate" ]; then
                uv="$candidate"
            fi
        done
    fi
    if [ -z "$uv" ] || [ ! -x "$uv" ]; then
        echo "router-hook: cannot find uv; install it (https://docs.astral.sh/uv/) and start a new session" >&2
        exit 0
    fi
    mkdir -p "$data"
    if [ ! -x "$venv/bin/python" ] && ! "$uv" venv -q "$venv" </dev/null >&2; then
        echo "router-hook: could not create the environment in $venv; see the uv error above" >&2
        exit 0
    fi
    # --reinstall-package: the version may be the same while the files changed (a local marketplace).
    if "$uv" pip install -q --python "$venv/bin/python" --reinstall-package model-router "$root" </dev/null >&2; then
        printf '%s' "$version" >"$stamp"
    else
        echo "router-hook: could not install the package in $venv; see the uv error above" >&2
        exit 0
    fi
fi

exec "$hook"
