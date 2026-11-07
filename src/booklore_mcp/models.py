"""Pydantic response models returned by the MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class BookSummary(BaseModel):
    """Trimmed view of a Book, returned by the list-style read tools."""

    id: int | None = None
    title: str | None = None
    authors: list[str] | None = None
    series: str | None = None
    readStatus: str | None = None
    personalRating: float | None = None
    shelves: list[str] = []
    # "book" | "comic" | "audiobook", from the primary file; None when the record
    # carries no file info.
    kind: str | None = None


class NameSuggestion(BaseModel):
    """A stored name close to one that matched nothing ("did you mean")."""

    name: str
    book_count: int
    score: float


class SearchResult(BaseModel):
    """Paginated result envelope for search_books / books_by_author / books_by_genre.
    `suggestions` is filled only when an exact-name lookup found nothing."""

    total: int
    limit: int
    offset: int
    books: list[BookSummary]
    suggestions: list[NameSuggestion] = []


class TitleMatch(BookSummary):
    """A search_titles hit: which field matched and how closely (1.0 = exact text)."""

    matched_on: Literal["title", "subtitle", "series", "tag"]
    score: float


class TitleSearchResult(BaseModel):
    """Paginated result envelope for search_titles."""

    total: int
    limit: int
    offset: int
    books: list[TitleMatch]


class MatchCandidate(BaseModel):
    """A library book considered for one match_books input row."""

    book: BookSummary
    score: float
    title_score: float
    author_score: float | None = None


class BookMatch(BaseModel):
    """match_books' verdict for one input row. `book` is set only when status is
    "matched"; `candidates` lists the closest books either way (best first)."""

    input: dict
    status: Literal["matched", "ambiguous", "unmatched"]
    book: BookSummary | None = None
    candidates: list[MatchCandidate] = []


class MatchBooksResult(BaseModel):
    """Result envelope for match_books."""

    matched: int
    ambiguous: int
    unmatched: int
    results: list[BookMatch]
