"""Plain-text tables and money, with no extra dependency."""

from __future__ import annotations

from collections.abc import Sequence


def money(value: float | None) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}"
    if value < 1:
        return f"${value:.3f}"
    return f"${value:,.2f}"


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(row: list[str]) -> str:
        parts = []
        for i, c in enumerate(row):
            numeric = c.replace(",", "").replace("$", "").replace("%", "").replace(".", "").lstrip("-").isdigit()
            parts.append(c.rjust(widths[i]) if numeric and i > 0 else c.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    lines = [fmt(cells[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in cells[1:]]
    return "\n".join(lines)
