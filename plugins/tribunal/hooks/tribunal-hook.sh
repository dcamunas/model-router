#!/bin/sh
# Run the tribunal hook from the environment the model-router plugin builds (this plugin installs no Python of
# its own): the sibling data directory of the model-router plugin, or MODEL_ROUTER_TRIBUNAL_HOOK, or the PATH.
# Without any of them it exits quietly with a note: a hook must never block Claude. The event JSON on stdin is
# passed through to the real hook untouched.
set -u
PATH="${PATH:-}:/usr/bin:/bin"

if [ "${MODEL_ROUTER_TRIBUNAL:-}" = "0" ]; then
    exit 0
fi
hook="${MODEL_ROUTER_TRIBUNAL_HOOK:-}"
if [ -z "$hook" ] && [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    for candidate in "$(dirname "$CLAUDE_PLUGIN_DATA")"/model-router-*/venv/bin/router-tribunal-hook; do
        if [ -x "$candidate" ]; then
            hook="$candidate"
            break
        fi
    done
fi
if [ -z "$hook" ]; then
    hook=$(command -v router-tribunal-hook 2>/dev/null || true)
fi
if [ -z "$hook" ] || [ ! -x "$hook" ]; then
    echo "tribunal: the model-router plugin's environment (0.13.0 or later) is not ready; install or update model-router and start a new session" >&2
    exit 0
fi
exec "$hook"
