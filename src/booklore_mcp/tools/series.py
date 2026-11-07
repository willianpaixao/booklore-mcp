"""Series tools: find missing volumes / issues in book and comic series."""

from __future__ import annotations

import math

from ..app import mcp
from ..helpers import (
    BookKind,
    _all_books,
    _book_kind,
    _matches_kind,
    _meta,
    _name_counts,
    _series_number,
    _suggest_names,
)


def _fmt(number: float) -> int | float:
    """1.0 -> 1, keep 1.5 as-is, so whole-numbered volumes read naturally."""
    return int(number) if number.is_integer() else number


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Find series gaps"})
async def find_series_gaps(
    series: str | None = None,
    kind: BookKind = "all",
    include_complete: bool = False,
) -> dict:
    """Find missing parts of book and comic series you own — read-only.

    Books are grouped by series name (case-insensitive). A book's position is its
    seriesNumber; for comics without one, a numeric comicMetadata.issueNumber is used.
    `missing` lists the whole numbers from 1 up to the series total (when BookLore knows
    it, `total_known`=true) or else up to the highest number you own — so with an unknown
    total, gaps *inside* the run are found but missing trailing volumes cannot be. Half
    numbers like 1.5 count as owned extras, never as gaps. `unnumbered` lists books in the
    series with no position at all (fix their seriesNumber to include them).

    `series` narrows to one series (case-insensitive exact name); `kind` to "book",
    "comic" (CBX) or "audiobook". If no series has that name, `suggestions` lists the
    closest series names with their book counts — retry with one of those.
    Complete series are omitted unless include_complete=True.
    Returns {series_count, incomplete_count, series:[{series, kind, series_total,
    total_known, owned, missing, unnumbered, complete, books:[{id, title, number}]}]},
    plus `suggestions` when a named series was not found.
    """
    # seriesTotal is stripped from the plain list response, so fetch full records.
    groups: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    all_names: list[str] = []  # every series name of the right kind, for suggestions
    for b in await _all_books(with_description=True):
        name = (_meta(b).get("seriesName") or "").strip()
        if not name or not _matches_kind(b, kind):
            continue
        all_names.append(name)
        key = name.lower()
        if series is not None and key != series.strip().lower():
            continue
        names.setdefault(key, name)
        groups.setdefault(key, []).append(b)

    report: list[dict] = []
    for key in sorted(groups, key=lambda k: names[k].lower()):
        books = groups[key]
        numbered = [(b, n) for b in books if (n := _series_number(b)) is not None]
        owned = sorted({n for _, n in numbered})
        totals = [t for b in books if isinstance(t := _meta(b).get("seriesTotal"), int) and t > 0]
        series_total = max(totals) if totals else None
        upper = max(series_total or 0, math.floor(max(owned)) if owned else 0)
        whole_owned = {int(n) for n in owned if n.is_integer()}
        missing = [i for i in range(1, upper + 1) if i not in whole_owned]
        unnumbered = [b.get("id") for b in books if _series_number(b) is None]
        complete = not missing and not unnumbered and upper > 0
        if complete and not include_complete:
            continue
        kinds = {_book_kind(b) for b in books}
        report.append(
            {
                "series": names[key],
                "kind": kinds.pop() if len(kinds) == 1 else "mixed",
                "series_total": series_total,
                "total_known": series_total is not None,
                "owned": [_fmt(n) for n in owned],
                "missing": missing,
                "unnumbered": unnumbered,
                "complete": complete,
                "books": [
                    {
                        "id": b.get("id"),
                        "title": b.get("title") or _meta(b).get("title"),
                        "number": _fmt(n) if (n := _series_number(b)) is not None else None,
                    }
                    for b in sorted(
                        books, key=lambda b: (_series_number(b) is None, _series_number(b) or 0)
                    )
                ],
            }
        )
    result: dict = {
        "series_count": len(report),
        "incomplete_count": sum(1 for s in report if not s["complete"]),
        "series": report,
    }
    if series is not None and not groups:
        counts = _name_counts([n] for n in all_names)
        result["suggestions"] = [s.model_dump() for s in _suggest_names(series, counts)]
    return result
