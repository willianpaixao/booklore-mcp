"""Search & discovery tools: full-library search, title and genre search, author
indexes, duplicate finding."""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Literal

from .. import runtime
from ..app import mcp
from ..client import BookLoreError
from ..helpers import (
    _MISSING_FULL_ONLY,
    BookKind,
    _all_books,
    _author_names,
    _categories,
    _fuzzy_score,
    _matches_filters,
    _matches_kind,
    _matches_query,
    _meta,
    _name_counts,
    _normalize_text,
    _suggest_names,
    _summarize_book,
    _token_sort,
    _validate_filters,
)
from ..models import (
    BookMatch,
    MatchBooksResult,
    MatchCandidate,
    SearchResult,
    TitleMatch,
    TitleSearchResult,
)

_SORT_KEYS = {
    "title": lambda b: (b.get("title") or (b.get("metadata") or {}).get("title") or "").lower(),
    "addedOn": lambda b: b.get("addedOn") or "",
    "rating": lambda b: b["personalRating"] if b.get("personalRating") is not None else -1,
    "readStatus": lambda b: b.get("readStatus") or "",
}

# Minimum word-level similarity for a search_titles fuzzy hit. 0.80 lets neighbouring
# words collide (witcher/watcher 0.86, batman/batmen 0.83); 0.88 still tolerates
# dropped/doubled letters ("foundaton" 0.95, "diskworld" 0.89).
_TITLE_MATCH_THRESHOLD = 0.88
_TitleField = Literal["title", "subtitle", "series", "tag"]

# Duplicate-detection signal presets (BookLore exposes no presets server-side).
_DUP_SIGNALS = (
    "matchByIsbn",
    "matchByExternalId",
    "matchByTitleAuthor",
    "matchByDirectory",
    "matchByFilename",
)
_DUP_PRESETS = {
    "strict": {"matchByIsbn", "matchByExternalId"},
    "balanced": {"matchByIsbn", "matchByExternalId", "matchByTitleAuthor"},
    "aggressive": set(_DUP_SIGNALS),
}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Search books"})
async def search_books(
    query: str = "",
    filters: dict | None = None,
    sort: Literal["title", "addedOn", "rating", "readStatus"] = "title",
    order: Literal["asc", "desc"] = "asc",
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Search and filter the library server-side-style, returning a paginated page.

    `query` is a case-insensitive substring over title/series/isbn/author names. It does
    not search subtitles (they need the slower full-record fetch) — for subtitles and
    misspelled or half-remembered titles use search_titles instead.
    `filters` is an optional object:
      - tags: [str], tags_mode: "any"|"all" (default "any")
      - categories: [str] (any), authors: [str] (any, substring)
      - shelf_ids: [int], library_ids: [int]
      - kind: "book"|"comic"|"audiobook" — by primary file type (comics are CBX)
      - read_status: "UNSET"|"READING"|"READ"|"UNREAD"|… (sparse upstream)
      - missing: subset of ["tags","categories","authors","description","isbn",
        "isbn10","isbn13","series","cover","asin","goodreadsId","googleId",
        "hardcoverId","amazonRating","goodreadsRating","publisher","language",
        "pageCount"] — fields to treat as empty.
      - missing_mode: "all" (default) keeps books where ALL listed fields are
        empty; "any" keeps books missing AT LEAST ONE — better for enrichment
        sweeps ("anything still lacking an id/rating").
    Unknown filter keys, `missing` fields or mode/kind values are rejected with the
    list of valid options — they are never silently ignored.
    Returns {total, limit, offset, books:[summaries]}. `limit` is capped at 200.
    """
    filters = filters or {}
    _validate_filters(filters)
    missing = filters.get("missing") or []
    # Identifier fields (asin/goodreadsId/…) and description are stripped from the plain
    # list response — fetch full metadata only when a `missing` filter depends on one.
    need_desc = any(f in _MISSING_FULL_ONLY for f in missing)
    books = await _all_books(with_description=need_desc)

    if query:
        ql = query.lower()
        books = [b for b in books if _matches_query(b, ql)]
    if filters:
        books = [b for b in books if _matches_filters(b, filters)]

    books.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["title"]), reverse=(order == "desc"))

    total = len(books)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    page = books[offset : offset + limit]
    return SearchResult(
        total=total, limit=limit, offset=offset, books=[_summarize_book(b) for b in page]
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List authors"})
async def list_authors(query: str = "", limit: int = 100, offset: int = 0) -> dict:
    """List authors (from BookLore's authors index) with per-author book counts.
    Optionally filter by a case-insensitive substring `query`."""
    authors = await runtime.client.get("/api/v1/authors") or []
    if query:
        ql = query.lower()
        authors = [a for a in authors if ql in (a.get("name") or "").lower()]
    authors.sort(key=lambda a: (a.get("name") or "").lower())
    page = authors[max(0, offset) : max(0, offset) + max(1, limit)]
    return {
        "total": len(authors),
        "authors": [
            {"id": a.get("id"), "name": a.get("name"), "book_count": a.get("bookCount")}
            for a in page
        ],
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Books by author"})
async def books_by_author(author: str, limit: int = 50, offset: int = 0) -> SearchResult:
    """List books by an author (case-insensitive exact match on the author name).

    When nothing matches exactly, `suggestions` lists the closest author names in the
    library (typo- and word-order-tolerant, e.g. "Sanderson, Brandon") with their book
    counts — retry with one of those names. Returns {total, limit, offset, books,
    suggestions}. `limit` is capped at 200.
    """
    al = author.strip().lower()
    books = await _all_books()
    matched = [b for b in books if any(al == n.lower() for n in _author_names(_meta(b)))]
    result = _page(matched, limit, offset)
    if not matched:
        counts = _name_counts(_author_names(_meta(b)) for b in books)
        result.suggestions = _suggest_names(author, counts)
    return result


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Search titles"})
async def search_titles(
    query: str,
    kind: BookKind = "all",
    library_id: int | None = None,
    shelf_id: int | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TitleSearchResult:
    """Approximate (typo-tolerant) search over book names: title, subtitle, series name
    and tags.

    Use it for misspelled or half-remembered names ("foundaton", "hary potter"). Authors
    and ISBNs are ignored. Exact substring hits always match; otherwise a word-level
    similarity of at least 88% is required. Results are ranked best match first, then
    by title. Diacritics and punctuation are ignored.

    Searches the WHOLE library by default. Leave every scope parameter unset unless the
    user explicitly asks to narrow the search:
      - kind: only when the user names a format — "book", "comic" (CBX) or "audiobook".
      - library_id: only when the user names a library (ids from list_libraries).
      - shelf_id: only when the user names a shelf (ids from list_shelves).
      - category: only when the user names a genre (names from list_genres;
        case-insensitive exact match).
    For exact matching, author search or multi-value filters use search_books.
    Each hit carries `matched_on` ("title" | "subtitle" | "series" | "tag") and `score`
    (1.0 = the query text appears verbatim; lower = a fuzzy, typo-tolerant hit).
    `limit` is capped at 200. Returns {total, limit, offset, books:[summaries]}.
    """
    q = _normalize_text(query)
    if not q:
        raise BookLoreError("Provide a non-empty `query`.")

    scope: dict = {}
    if kind != "all":
        scope["kind"] = kind
    if library_id is not None:
        scope["library_ids"] = [library_id]
    if shelf_id is not None:
        scope["shelf_ids"] = [shelf_id]
    if category:
        scope["categories"] = [category]

    def best_field(b: dict) -> tuple[float, _TitleField]:
        meta = _meta(b)
        fields: list[tuple[_TitleField, object]] = [
            ("title", b.get("title") or meta.get("title")),
            ("subtitle", meta.get("subtitle")),
            ("series", meta.get("seriesName")),
            *(("tag", t) for t in meta.get("tags") or []),
        ]
        scores = [(_fuzzy_score(q, str(value)), label) for label, value in fields if value]
        # max() keeps the first of equal scores, so title beats subtitle beats series/tag.
        return max(scores, key=lambda sl: sl[0], default=(0.0, "title"))

    # Subtitles are stripped from the plain list response, so fetch full records.
    hits: list[tuple[float, _TitleField, dict]] = []
    for b in await _all_books(with_description=True):
        if scope and not _matches_filters(b, scope):
            continue
        score, label = best_field(b)
        if score >= _TITLE_MATCH_THRESHOLD:
            hits.append((score, label, b))
    hits.sort(key=lambda h: (-h[0], _SORT_KEYS["title"](h[2])))

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return TitleSearchResult(
        total=len(hits),
        limit=limit,
        offset=offset,
        books=[
            TitleMatch(**_summarize_book(b).model_dump(), matched_on=label, score=round(sc, 3))
            for sc, label, b in hits[offset : offset + limit]
        ],
    )


# match_books: a row matches when its title is at least this similar to a book's (whole
# title vs whole title, unlike search_titles' substring-friendly scoring) and, if the row
# names an author, that author is at least _MATCH_AUTHOR_MIN similar to one of the book's.
_MATCH_TITLE_MIN = _TITLE_MATCH_THRESHOLD
_MATCH_AUTHOR_MIN = 0.6
# The best candidate must beat the runner-up by this much, else the row is "ambiguous"
# (typically duplicates or editions — let the user pick).
_MATCH_MARGIN = 0.05
_MATCH_MAX_ITEMS = 200
_MATCH_CANDIDATES = 3
_Scored = tuple[float, float, float | None, dict]  # (score, title, author, book)


def _title_variants(title: str) -> set[str]:
    """Normalized forms of an input title worth comparing: as given minus parentheticals
    (Goodreads appends "(Series, #3)"), and the part before a ":" subtitle."""
    base = re.sub(r"\([^)]*\)", " ", title)
    return {v for v in (_normalize_text(base), _normalize_text(base.split(":")[0])) if v}


# Below this title similarity a book isn't even shown as a near miss. It doubles as the
# pruning floor in _best_ratios: 200 rows x 5,000 books takes ~3 s at 0.7 vs ~18 s at 0.5,
# because the cheap upper bounds rarely rule a pair out at a low floor.
_MATCH_CANDIDATE_FLOOR = 0.7


def _best_ratios(queries: set[str], targets_per_book: list[list[str]]) -> list[float]:
    """For each book, the best whole-string similarity between any query and any of its
    targets. Scores that cannot reach _MATCH_CANDIDATE_FLOOR are pruned with the cheap
    upper bounds and left below it (not computed exactly)."""
    best = [0.0] * len(targets_per_book)
    for q in queries:
        m = SequenceMatcher(None, "", q)  # seq2 is analysed once per query, not per book
        for i, targets in enumerate(targets_per_book):
            for t in targets:
                m.set_seq1(t)
                bar = max(best[i], _MATCH_CANDIDATE_FLOOR)
                if m.real_quick_ratio() >= bar and m.quick_ratio() >= bar:
                    best[i] = max(best[i], m.ratio())
    return best


def _match_ok(s: _Scored) -> bool:
    return s[1] >= _MATCH_TITLE_MIN and (s[2] is None or s[2] >= _MATCH_AUTHOR_MIN)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Match books"})
async def match_books(items: list[dict]) -> MatchBooksResult:
    """Resolve a list of {title, author?} rows (e.g. a Goodreads CSV export) to library
    books in one call — read-only. Use it to get book ids before bulk_set_read_status,
    add_to_shelves or set_personal_rating.

    `items` is [{"title": str, "author": str (optional)}], at most 200 rows per call; any
    other keys are echoed back untouched in `input`. Titles are compared whole and
    typo-tolerantly against each book's title (and title + subtitle); parentheticals
    like "(Series, #3)" and a ": subtitle" are tolerated. A given author is compared
    word-order-insensitively ("Sanderson, Brandon") and must also be close.

    Each row gets a status:
      - "matched": one clear best book — use `book.id`.
      - "ambiguous": several books score about equally (often duplicates or editions) —
        ask the user which of `candidates` is meant.
      - "unmatched": nothing close enough; `candidates` shows the nearest misses, if any.
    Returns {matched, ambiguous, unmatched, results:[{input, status, book, candidates:
    [{book, score, title_score, author_score}]}]}, in input order.
    """
    if not items:
        raise BookLoreError('Provide at least one item: [{"title": ..., "author": ...}].')
    if len(items) > _MATCH_MAX_ITEMS:
        raise BookLoreError(
            f"Too many items ({len(items)}); send at most {_MATCH_MAX_ITEMS} per call."
        )
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            raise BookLoreError(f'items[{i}] needs a non-empty "title".')

    # Subtitles are stripped from the plain list response, so fetch full records.
    library: list[tuple[dict, list[str], list[str]]] = []
    for b in await _all_books(with_description=True):
        title = b.get("title") or _meta(b).get("title") or ""
        subtitle = _meta(b).get("subtitle")
        titles = [_normalize_text(title)]
        if subtitle:
            titles.append(_normalize_text(f"{title} {subtitle}"))
        authors = [_token_sort(a) for a in _author_names(_meta(b))]
        library.append((b, [t for t in titles if t], [a for a in authors if a]))

    library_titles = [titles for _, titles, _ in library]
    results: list[BookMatch] = []
    for item in items:
        title_scores = _best_ratios(_title_variants(str(item["title"])), library_titles)
        author = str(item.get("author") or "").strip()
        author_query = _token_sort(author) if author else ""

        scored: list[_Scored] = []
        for (b, _, authors), title_score in zip(library, title_scores, strict=True):
            if title_score < _MATCH_CANDIDATE_FLOOR:
                continue
            author_score = (
                max(SequenceMatcher(None, author_query, a).ratio() for a in authors)
                if author_query and authors
                else None
            )
            score = (
                title_score if author_score is None else 0.75 * title_score + 0.25 * author_score
            )
            scored.append((score, title_score, author_score, b))
        scored.sort(key=lambda sc: -sc[0])

        good = [sc for sc in scored if _match_ok(sc)]
        status: Literal["matched", "ambiguous", "unmatched"]
        if not good:
            status = "unmatched"
        elif len(good) == 1 or good[0][0] - good[1][0] > _MATCH_MARGIN:
            status = "matched"
        else:
            status = "ambiguous"
        candidates = [
            MatchCandidate(
                book=_summarize_book(b),
                score=round(sc, 3),
                title_score=round(ts, 3),
                author_score=None if au is None else round(au, 3),
            )
            for sc, ts, au, b in (good or scored)[:_MATCH_CANDIDATES]
        ]
        results.append(
            BookMatch(
                input=item,
                status=status,
                book=candidates[0].book if status == "matched" else None,
                candidates=candidates,
            )
        )

    return MatchBooksResult(
        matched=sum(r.status == "matched" for r in results),
        ambiguous=sum(r.status == "ambiguous" for r in results),
        unmatched=sum(r.status == "unmatched" for r in results),
        results=results,
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List genres"})
async def list_genres(query: str = "", limit: int = 100, offset: int = 0) -> dict:
    """List the library's genres (BookLore categories) with per-genre book counts.

    Genres are collected from every book's categories, merged case-insensitively (the
    first spelling seen is kept). Optionally filter by a case-insensitive substring
    `query`. Use books_by_genre to list the books in one. Returns {total, genres:[{name,
    book_count}]}.
    """
    names: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for b in await _all_books():
        # De-duplicate per book first so a book tagged "SciFi" and "scifi" counts once.
        for key, name in {c.lower(): c for c in _categories(b)}.items():
            names.setdefault(key, name)
            counts[key] += 1
    keys = sorted(names, key=lambda k: names[k].lower())
    if query:
        ql = query.lower()
        keys = [k for k in keys if ql in k]
    page = keys[max(0, offset) : max(0, offset) + max(1, limit)]
    return {
        "total": len(keys),
        "genres": [{"name": names[k], "book_count": counts[k]} for k in page],
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Books by genre"})
async def books_by_genre(
    genre: str, kind: BookKind = "all", limit: int = 50, offset: int = 0
) -> SearchResult:
    """List books in a genre (case-insensitive exact match on a BookLore category). Get
    genre names from list_genres. `kind` narrows to "book", "comic" or "audiobook".
    When nothing matches exactly, `suggestions` lists the closest genre names (among
    books of that kind) with their book counts — retry with one of those names.
    Sorted by title. Returns {total, limit, offset, books, suggestions}."""
    gl = genre.strip().lower()
    books = [b for b in await _all_books() if _matches_kind(b, kind)]
    matched = [b for b in books if any(gl == c.lower() for c in _categories(b))]
    matched.sort(key=_SORT_KEYS["title"])
    result = _page(matched, limit, offset)
    if not matched:
        result.suggestions = _suggest_names(genre, _name_counts(_categories(b) for b in books))
    return result


def _page(books: list[dict], limit: int, offset: int) -> SearchResult:
    """Paginate sorted raw records into a SearchResult (limit capped at 200)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return SearchResult(
        total=len(books),
        limit=limit,
        offset=offset,
        books=[_summarize_book(b) for b in books[offset : offset + limit]],
    )


@mcp.tool(annotations={"readOnlyHint": True, "title": "Find duplicates"})
async def find_duplicates(
    library_id: int | None = None,
    preset: Literal["strict", "balanced", "aggressive"] = "balanced",
    signals: dict | None = None,
) -> dict:
    """Find duplicate books using BookLore's native detection — read-only, merges nothing.

    Detection is per-library; omit `library_id` to scan every library. `preset` picks
    the signal set: strict (ISBN + external id), balanced (+ title/author), aggressive
    (+ same-directory + filename). `signals` overrides individual flags, e.g.
    {"matchByFilename": true}. Each group reports the suggested merge target, the match
    reason, the books, and `distinct_isbns` (true ⇒ the books carry different ISBNs, so
    likely separate editions rather than true duplicates).
    Returns {libraries_scanned, group_count, groups}.
    """
    flags = {s: (s in _DUP_PRESETS[preset]) for s in _DUP_SIGNALS}
    if signals:
        bad = sorted(set(signals) - set(_DUP_SIGNALS))
        if bad:
            raise BookLoreError(
                f"Unknown duplicate signal(s): {', '.join(bad)}. Valid: {', '.join(_DUP_SIGNALS)}."
            )
        flags.update({k: bool(v) for k, v in signals.items()})

    if library_id is not None:
        library_ids: list[int] = [library_id]
    else:
        libs = await runtime.client.get("/api/v1/libraries") or []
        library_ids = [
            lib["id"] for lib in libs if isinstance(lib, dict) and lib.get("id") is not None
        ]

    groups: list[dict] = []
    for lib_id in library_ids:
        result = (
            await runtime.client.post(
                "/api/v1/books/duplicates", json={"libraryId": lib_id, **flags}
            )
            or []
        )
        for grp in result:
            books = grp.get("books") or []
            isbns = {(_meta(b).get("isbn13") or _meta(b).get("isbn10")) for b in books}
            isbns.discard(None)
            groups.append(
                {
                    "library_id": lib_id,
                    "match_reason": grp.get("matchReason"),
                    "suggested_target_book_id": grp.get("suggestedTargetBookId"),
                    "distinct_isbns": len(isbns) > 1,
                    "books": [
                        {
                            "id": b.get("id"),
                            "title": b.get("title") or _meta(b).get("title"),
                            "authors": _author_names(_meta(b)),
                            "isbn13": _meta(b).get("isbn13"),
                            "isbn10": _meta(b).get("isbn10"),
                        }
                        for b in books
                    ],
                }
            )
    return {"libraries_scanned": len(library_ids), "group_count": len(groups), "groups": groups}
