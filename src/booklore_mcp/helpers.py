"""Shared helpers used across the tool modules.

Pure data-shaping helpers plus the few async helpers that read/write BookLore through
the shared `runtime.client`. Tool-specific helpers (export field maps, provider status,
duplicate presets) live with their single consuming tool module, not here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any, Literal

from . import runtime
from .client import BookLoreError
from .models import BookSummary, NameSuggestion


def _author_names(meta: dict) -> list[str]:
    """Author display names from a metadata object (authors may be dicts or strings)."""
    names = []
    for a in meta.get("authors") or []:
        name = a.get("name") if isinstance(a, dict) else a
        if name:
            names.append(str(name))
    return names


def _summarize_book(book: dict[str, Any]) -> BookSummary:
    """Trim a full Book record down to the fields most useful in a list view."""
    meta = book.get("metadata") or {}
    raw_authors = meta.get("authors")
    authors: list[str] | None = _author_names(meta) if isinstance(raw_authors, list) else None
    shelves = [
        s["name"] for s in (book.get("shelves") or []) if isinstance(s, dict) and s.get("name")
    ]
    return BookSummary(
        id=book.get("id"),
        title=book.get("title") or meta.get("title"),
        authors=authors,
        series=meta.get("seriesName"),
        readStatus=book.get("readStatus"),
        personalRating=book.get("personalRating"),
        shelves=shelves,
        kind=_book_kind(book) if book.get("primaryFile") else None,
    )


# Predicates for `missing` filtering — True means the field is empty/absent. `isbn`
# and `description` are sparsely populated upstream; `cover` uses coverUpdatedOn as a
# presence proxy (BookLore doesn't expose a cover URL on the book record).
_MISSING_CHECKS = {
    "tags": lambda m: not m.get("tags"),
    "categories": lambda m: not m.get("categories"),
    "authors": lambda m: not _author_names(m),
    "description": lambda m: not m.get("description"),
    "isbn": lambda m: not (m.get("isbn10") or m.get("isbn13")),
    "isbn10": lambda m: not m.get("isbn10"),
    "isbn13": lambda m: not m.get("isbn13"),
    "series": lambda m: not m.get("seriesName"),
    "cover": lambda m: not m.get("coverUpdatedOn"),
    "asin": lambda m: not m.get("asin"),
    "goodreadsId": lambda m: not m.get("goodreadsId"),
    "googleId": lambda m: not m.get("googleId"),
    "hardcoverId": lambda m: not m.get("hardcoverId"),
    "amazonRating": lambda m: not m.get("amazonRating"),
    "goodreadsRating": lambda m: not m.get("goodreadsRating"),
    "publisher": lambda m: not m.get("publisher"),
    "language": lambda m: not m.get("language"),
    "pageCount": lambda m: not m.get("pageCount"),
}
# Of the missing-checks, those whose field BookLore strips from the plain list response.
_MISSING_FULL_ONLY = {"asin", "goodreadsId", "googleId", "hardcoverId", "description"}


def _matches_query(book: dict, q: str) -> bool:
    """Case-insensitive substring match over title/series/isbn/author names — only fields
    the light (no-description) list carries, so the result is the same whichever list
    was fetched."""
    meta = book.get("metadata") or {}
    parts = [
        book.get("title") or meta.get("title"),
        meta.get("seriesName"),
        meta.get("isbn10"),
        meta.get("isbn13"),
        *_author_names(meta),
    ]
    return q in " ".join(str(x) for x in parts if x).lower()


def _normalize_text(text: str) -> str:
    """Casefold, strip diacritics and turn punctuation into spaces, for fuzzy matching.
    "Ender's Pokémon" -> "ender s pokemon"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", stripped).split())


def _fuzzy_score(query: str, text: str) -> float:
    """Best similarity (0..1) of a normalized `query` to any run of words in `text`.

    A substring hit scores 1.0. Otherwise the query is compared against word windows of
    roughly its own length (a "partial ratio"), so a short query can match inside a long
    title — a whole-string ratio would score "dune" vs "Dune Messiah" far too low.
    """
    hay = _normalize_text(text)
    if not query or not hay:
        return 0.0
    if query in hay:
        return 1.0
    words = hay.split()
    n = len(query.split())
    best = 0.0
    for size in range(min(max(1, n - 1), len(words)), min(len(words), n + 1) + 1):
        for i in range(len(words) - size + 1):
            m = SequenceMatcher(None, query, " ".join(words[i : i + size]))
            # quick_ratio() is a cheap upper bound — skip windows that can't win.
            if m.real_quick_ratio() > best and m.quick_ratio() > best:
                best = max(best, m.ratio())
    return best


def _token_sort(text: str) -> str:
    """Normalized words in sorted order, so "Sanderson, Brandon" == "Brandon Sanderson"."""
    return " ".join(sorted(_normalize_text(text).split()))


def _name_similarity(query: str, name: str) -> float:
    """Similarity of a typed name to a stored one, tolerant of typos and word order."""
    word_order = SequenceMatcher(None, _token_sort(query), _token_sort(name)).ratio()
    return max(_fuzzy_score(_normalize_text(query), name), word_order)


def _name_counts(names_per_book: Iterable[Iterable[str]]) -> dict[str, int]:
    """Count books per name, merged case-insensitively (the first spelling seen wins) and
    counting each book once per name."""
    display: dict[str, str] = {}
    counts: dict[str, int] = {}
    for names in names_per_book:
        seen: set[str] = set()
        for name in names:
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            display.setdefault(key, name)
            counts[key] = counts.get(key, 0) + 1
    return {display[k]: counts[k] for k in display}


# A "did you mean" hint only needs to be plausible, so this is far looser than the
# search_titles match threshold ("Sci-Fi" vs "Science Fiction" scores ~0.57).
_SUGGESTION_MIN_SCORE = 0.5


def _suggest_names(query: str, counts: dict[str, int], limit: int = 5) -> list[NameSuggestion]:
    """The stored names closest to `query`, best first — the "did you mean" list an
    exact-name lookup returns when it finds nothing."""
    scored = [(_name_similarity(query, name), name) for name in counts]
    scored = [(sc, n) for sc, n in scored if sc >= _SUGGESTION_MIN_SCORE]
    scored.sort(key=lambda sn: (-sn[0], sn[1].lower()))
    return [
        NameSuggestion(name=n, book_count=counts[n], score=round(sc, 3)) for sc, n in scored[:limit]
    ]


# Every key search_books' `filters` understands. Unknown keys are rejected rather than
# ignored, so a misspelled filter can't silently return the whole library.
_FILTER_KEYS = {
    "tags",
    "tags_mode",
    "categories",
    "authors",
    "shelf_ids",
    "library_ids",
    "kind",
    "read_status",
    "missing",
    "missing_mode",
}
_FILTER_CHOICES = {
    "tags_mode": {"any", "all"},
    "missing_mode": {"any", "all"},
    "kind": {"book", "comic", "audiobook"},
}


def _validate_filters(filters: dict) -> None:
    """Raise a BookLoreError naming the valid options for an unknown filter key, an
    unknown `missing` field, or an out-of-range mode/kind value."""
    if bad := sorted(set(filters) - _FILTER_KEYS):
        raise BookLoreError(
            f"Unknown filter key(s): {', '.join(bad)}. Valid: {', '.join(sorted(_FILTER_KEYS))}."
        )
    for key, choices in _FILTER_CHOICES.items():
        if key in filters and filters[key] not in choices:
            raise BookLoreError(
                f"Invalid {key} {filters[key]!r}. Valid: {', '.join(sorted(choices))}."
            )
    if bad := sorted(set(filters.get("missing") or []) - set(_MISSING_CHECKS)):
        raise BookLoreError(
            f"Unknown missing field(s): {', '.join(bad)}. "
            f"Valid: {', '.join(sorted(_MISSING_CHECKS))}."
        )


def _matches_filters(book: dict, filters: dict) -> bool:
    """Apply a search_books `filters` object to a raw book record."""
    meta = book.get("metadata") or {}

    if tags := filters.get("tags"):
        have = {t.lower() for t in (meta.get("tags") or [])}
        want = {t.lower() for t in tags}
        if filters.get("tags_mode") == "all":
            if not want.issubset(have):
                return False
        elif not (want & have):
            return False

    if cats := filters.get("categories"):
        have = {c.lower() for c in (meta.get("categories") or [])}
        if not ({c.lower() for c in cats} & have):
            return False

    if authors := filters.get("authors"):
        names = [n.lower() for n in _author_names(meta)]
        if not any(q.lower() in n for q in authors for n in names):
            return False

    if shelf_ids := filters.get("shelf_ids"):
        have_ids = {s.get("id") for s in (book.get("shelves") or []) if isinstance(s, dict)}
        if not (set(shelf_ids) & have_ids):
            return False

    if (rs := filters.get("read_status")) and (book.get("readStatus") or "UNSET") != rs:
        return False

    if (libs := filters.get("library_ids")) and book.get("libraryId") not in set(libs):
        return False

    if (kind := filters.get("kind")) and not _matches_kind(book, kind):
        return False

    # `missing`: filter on empty fields. mode "all" (default) keeps a book only when every
    # listed field is empty; mode "any" keeps it when at least one listed field is empty.
    if missing := filters.get("missing"):
        flags = [_MISSING_CHECKS[f](meta) for f in missing if f in _MISSING_CHECKS]
        if flags:
            if filters.get("missing_mode") == "any":
                if not any(flags):
                    return False
            elif not all(flags):
                return False

    return True


def _merge_list(current: list | None, add: list | None = None, remove: list | None = None) -> list:
    """Case-insensitive add/remove on a string list, preserving order and casing."""
    remove_lower = {str(r).lower() for r in (remove or [])}
    result = [x for x in (current or []) if str(x).lower() not in remove_lower]
    have = {str(x).lower() for x in result}
    for a in add or []:
        if str(a).lower() not in have:
            result.append(a)
            have.add(str(a).lower())
    return result


# Fields BookLore's clearFlags map accepts on PUT .../metadata. Setting a flag true nulls
# that field regardless of replaceMode — the only way to clear ONE field without
# REPLACE_ALL wiping the whole record. (From the backend's MetadataClearFlags class.)
_CLEARABLE_FIELDS = {
    "title",
    "subtitle",
    "publisher",
    "publishedDate",
    "description",
    "seriesName",
    "seriesNumber",
    "seriesTotal",
    "isbn13",
    "isbn10",
    "asin",
    "goodreadsId",
    "comicvineId",
    "hardcoverId",
    "hardcoverBookId",
    "googleId",
    "pageCount",
    "language",
    "amazonRating",
    "amazonReviewCount",
    "goodreadsRating",
    "goodreadsReviewCount",
    "hardcoverRating",
    "hardcoverReviewCount",
    "lubimyczytacId",
    "lubimyczytacRating",
    "ranobedbId",
    "ranobedbRating",
    "audibleId",
    "audibleRating",
    "audibleReviewCount",
    "authors",
    "categories",
    "moods",
    "tags",
    "cover",
    "reviews",
    "narrator",
    "abridged",
    "ageRating",
    "contentRating",
}

# Goodreads ids arrive both bare ("171712768") and slugged
# ("23463279-designing-data-intensive-applications"); BookLore stores either verbatim.
_GOODREADS_ID_RE = re.compile(r"^\s*(\d+)(?:-.*)?\s*$")


def _normalize_goodreads_id(value: Any) -> Any:
    """Reduce a Goodreads id to its bare numeric form, leaving non-numeric values as-is."""
    if not isinstance(value, str):
        return value
    m = _GOODREADS_ID_RE.match(value)
    return m.group(1) if m else value.strip()


def _normalize_metadata(meta: dict) -> dict:
    """Copy of a metadata patch with identifier fields normalized on write."""
    out = dict(meta)
    if out.get("goodreadsId") is not None:
        out["goodreadsId"] = _normalize_goodreads_id(out["goodreadsId"])
    return out


def _meta(book: dict) -> dict:
    return book.get("metadata") or {}


# primaryFile.bookType -> the coarse kind the discovery tools filter on. CBX covers
# cbz/cbr/cb7 comic archives; everything else readable (EPUB/PDF/MOBI) is a "book".
_KIND_BY_FILE_TYPE = {"CBX": "comic", "AUDIOBOOK": "audiobook"}
BookKind = Literal["all", "book", "comic", "audiobook"]


def _book_kind(book: dict) -> str:
    """'comic', 'audiobook' or 'book', from the primary file's bookType."""
    file_type = (book.get("primaryFile") or {}).get("bookType")
    return _KIND_BY_FILE_TYPE.get(str(file_type).upper(), "book")


def _matches_kind(book: dict, kind: str) -> bool:
    return kind == "all" or _book_kind(book) == kind


def _series_number(book: dict) -> float | None:
    """A book's position in its series: metadata.seriesNumber, falling back for comics to
    comicMetadata.issueNumber when that is numeric ("12", "1.5" — not "Annual 1")."""
    meta = _meta(book)
    number = meta.get("seriesNumber")
    if isinstance(number, (int, float)) and not isinstance(number, bool):
        return float(number)
    issue = (meta.get("comicMetadata") or {}).get("issueNumber")
    if isinstance(issue, str):
        try:
            return float(issue.strip())
        except ValueError:
            return None
    return None


def _categories(book: dict) -> list[str]:
    """A book's categories (BookLore's genre field), as a list of strings."""
    return [str(c) for c in (_meta(book).get("categories") or []) if c]


def _shelf_names(book: dict) -> list[str]:
    return [s["name"] for s in (book.get("shelves") or []) if isinstance(s, dict) and s.get("name")]


async def _all_books(with_description: bool = False) -> list[dict]:
    return await runtime.client.get_books(with_description)


async def _put_metadata(
    book_id: int, meta_patch: dict, clear_fields: list[str] | None = None
) -> Any:
    """PUT a partial metadata object, touching ONLY the supplied fields.

    Uses REPLACE_WHEN_PROVIDED: BookLore writes just the keys present in the
    payload and leaves every other field untouched. (REPLACE_ALL would null out
    every field we omit — title, authors, description, ISBNs — so it must never
    be used with a partial patch.) `clearFlags` must be present or the API 500s.

    `clear_fields` are field names to null out: BookLore honours a true clearFlag
    regardless of replace mode, so it's the one way to clear a single field without
    REPLACE_ALL wiping the rest.
    """
    clear_flags = {f: True for f in (clear_fields or [])}
    return await runtime.client.put(
        f"/api/v1/books/{book_id}/metadata",
        params={"mergeCategories": "false", "replaceMode": "REPLACE_WHEN_PROVIDED"},
        json={"metadata": meta_patch, "clearFlags": clear_flags},
    )


def _validate_clear_fields(clear_fields: list[str] | None) -> None:
    """Reject clearFlag names BookLore won't accept, with a helpful message."""
    bad = sorted(set(clear_fields or []) - _CLEARABLE_FIELDS)
    if bad:
        raise BookLoreError(
            f"Cannot clear unknown field(s): {', '.join(bad)}. "
            f"Clearable fields: {', '.join(sorted(_CLEARABLE_FIELDS))}."
        )


async def _apply_patch(book_id: int, patch: dict) -> dict:
    """Apply a per-book patch to one book; return its fresh record.

    `patch` keys:
      - tags / categories: {"add": [...], "remove": [...]} — additive merges.
      - metadata: {field: value, ...} — arbitrary BookMetadata fields written
        with REPLACE_WHEN_PROVIDED (only the keys you pass; identifiers normalized).
      - clear_fields: [field, ...] — fields to null out (see _put_metadata).
    """
    clear_fields = patch.get("clear_fields") or []
    _validate_clear_fields(clear_fields)
    book = await runtime.client.get(f"/api/v1/books/{book_id}", params={"withDescription": "false"})
    meta = book.get("metadata") or {}
    meta_patch: dict[str, Any] = {}
    for field in ("tags", "categories"):
        if field in patch:
            ops = patch[field] or {}
            meta_patch[field] = _merge_list(meta.get(field), ops.get("add"), ops.get("remove"))
    if extra := patch.get("metadata"):
        meta_patch.update(_normalize_metadata(extra))
    if not meta_patch and not clear_fields:
        return book
    await _put_metadata(book_id, meta_patch, clear_fields)
    return await runtime.client.get(f"/api/v1/books/{book_id}", params={"withDescription": "false"})
