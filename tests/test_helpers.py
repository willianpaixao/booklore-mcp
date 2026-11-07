"""Unit tests for the _summarize_book helper (returns a BookSummary model)."""

from __future__ import annotations

from booklore_mcp.helpers import _summarize_book
from booklore_mcp.models import BookSummary


def test_summarize_trims_and_normalizes_authors():
    book = {
        "id": 7,
        "title": "The Hobbit",
        "readStatus": "READ",
        "personalRating": 8,
        "metadata": {
            "authors": [{"name": "J.R.R. Tolkien"}, "Christopher Tolkien"],
            "seriesName": "Middle-earth",
            "title": "ignored when top-level title present",
        },
        "shelves": [{"name": "Fantasy"}, {"name": "Favorites"}],
        "primaryFile": {"bookType": "EPUB"},
        "extraField": "should be dropped",
    }

    summary = _summarize_book(book)
    assert isinstance(summary, BookSummary)
    assert summary.model_dump() == {
        "id": 7,
        "title": "The Hobbit",
        "authors": ["J.R.R. Tolkien", "Christopher Tolkien"],
        "series": "Middle-earth",
        "readStatus": "READ",
        "personalRating": 8,
        "shelves": ["Fantasy", "Favorites"],
        "kind": "book",
    }


def test_summarize_falls_back_to_metadata_title():
    summary = _summarize_book({"id": 1, "metadata": {"title": "From metadata"}})
    assert summary.title == "From metadata"
    assert summary.authors is None
    assert summary.shelves == []
    assert summary.kind is None  # no file info -> unknown, not guessed as "book"
