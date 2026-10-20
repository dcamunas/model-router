"""The installed package version, recorded in every ledger entry so the record says which router decided."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def router_version() -> str:
    """Return the installed version of model-router, or `unknown` when it runs from a checkout that is not installed."""
    try:
        return version("model-router")
    except PackageNotFoundError:
        return "unknown"
