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

# Exit 0 when version $1 is newer than $2, compared number by number; an empty $2 is older than anything.
newer() {
    awk -v a="$1" -v b="$2" 'BEGIN { n = split(a, x, "."); m = split(b, y, "."); if (m > n) n = m
        for (i = 1; i <= n; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
        exit 1 }'
}

# A session opened before the plugin was updated keeps the old plugin root, while the environment already holds
# the newer version a later session installed. Going quiet would leave that session unrouted, and building would
# put the old version back under every newer session: route with the environment as it is.
stale=""
if [ -n "$version" ] && [ -x "$hook" ] && [ -n "$installed" ] && [ "$installed" != "$version" ] && ! newer "$version" "$installed"; then
    stale=1
    echo "router-hook: this session runs plugin $version, the environment has $installed; a new session uses it" >&2
fi

if [ -z "$stale" ] && { [ ! -x "$hook" ] || [ "$installed" != "$version" ]; }; then
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
