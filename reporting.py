"""Plain-text table rendering for reports."""

from __future__ import annotations

from collections.abc import Sequence


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render rows as an aligned text table."""
    cells = [[str(value) for value in row] for row in rows]
    widths = [
        max([len(header)] + [len(row[i]) for row in cells])
        for i, header in enumerate(headers)
    ]

    def line(values: Sequence[str]) -> str:
        return " | ".join(v.ljust(w) for v, w in zip(values, widths))

    separator = "-+-".join("-" * w for w in widths)
    return "\n".join([line(headers), separator, *(line(row) for row in cells)])
