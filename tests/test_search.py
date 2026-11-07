"""Tests for the search/discovery helpers and the new search/bulk/additive tools."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from booklore_mcp import helpers, runtime
from booklore_mcp.app import mcp
from booklore_mcp.client import BookLoreClient, BookLoreError

BASE = "http://booklore.test"


# --- pure helper unit tests (no I/O) ----------------------------------------


def test_author_names_normalizes_dicts_and_strings():
    meta = {"authors": [{"name": "Ann"}, "Bob", {"name": None}, ""]}
    assert helpers._author_names(meta) == ["Ann", "Bob"]


def test_merge_list_add_remove_case_insensitive_preserves_order():
    merged = helpers._merge_list(
        ["Python", "narrative"], add=["python", "trading"], remove=["NARRATIVE"]
    )
    assert merged == ["Python", "trading"]


def test_matches_filters_tags_any_vs_all():
    book = {"metadata": {"tags": ["blockchain", "bitcoin"]}}
    assert helpers._matches_filters(book, {"tags": ["BITCOIN"]})  # any, case-insensitive
    assert helpers._matches_filters(book, {"tags": ["bitcoin", "blockchain"], "tags_mode": "all"})
    assert not helpers._matches_filters(book, {"tags": ["bitcoin", "ethereum"], "tags_mode": "all"})


def test_matches_filters_missing_requires_every_listed_field_empty():
    book = {"metadata": {"tags": ["x"], "categories": []}}
    assert helpers._matches_filters(book, {"missing": ["categories"]})
    assert not helpers._matches_filters(book, {"missing": ["tags"]})
    assert not helpers._matches_filters(book, {"missing": ["tags", "categories"]})


def test_matches_filters_missing_any_mode_and_identifier_fields():
    book = {"metadata": {"asin": "B01", "goodreadsId": ""}}
    # "all" (default): every field must be empty — asin is present, so excluded.
    assert not helpers._matches_filters(book, {"missing": ["asin", "goodreadsId"]})
    # "any": at least one empty — goodreadsId is empty, so included.
    assert helpers._matches_filters(
        book, {"missing": ["asin", "goodreadsId"], "missing_mode": "any"}
    )


def test_normalize_goodreads_id_strips_slug_keeps_numeric():
    assert helpers._normalize_goodreads_id("23463279-designing-data-intensive") == "23463279"
    assert helpers._normalize_goodreads_id("171712768") == "171712768"
    assert helpers._normalize_goodreads_id("  42  ") == "42"
    assert helpers._normalize_goodreads_id(None) is None


def test_matches_filters_shelf_library_status():
    book = {"libraryId": 1, "readStatus": "READ", "shelves": [{"id": 5}], "metadata": {}}
    assert helpers._matches_filters(book, {"shelf_ids": [5]})
    assert not helpers._matches_filters(book, {"shelf_ids": [9]})
    assert helpers._matches_filters(book, {"library_ids": [1]})
    assert helpers._matches_filters(book, {"read_status": "READ"})
    assert not helpers._matches_filters(book, {"read_status": "UNREAD"})


def test_matches_query_over_title_and_authors():
    book = {"title": "The Hobbit", "metadata": {"authors": ["J.R.R. Tolkien"]}}
    assert helpers._matches_query(book, "tolkien")
    assert helpers._matches_query(book, "hobbit")
    assert not helpers._matches_query(book, "dune")


# --- tool flows -------------------------------------------------------------


@pytest.fixture
def authed(respx_mock):
    respx_mock.post(f"{BASE}/api/v1/auth/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "a", "refreshToken": "r"})
    )
    runtime.client = BookLoreClient(BASE, "tester", "secret")
    return respx_mock


_BOOKS = [
    {"id": 1, "title": "Tagged", "libraryId": 1, "metadata": {"tags": ["x"], "authors": ["Ann"]}},
    {"id": 2, "title": "Untagged", "libraryId": 1, "metadata": {"tags": [], "authors": ["Bob"]}},
]


async def test_search_books_missing_filter(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_BOOKS))
    async with Client(mcp) as c:
        result = await c.call_tool("search_books", {"filters": {"missing": ["tags"]}})
    assert result.data.total == 1
    assert result.data.books[0].id == 2


async def test_add_tags_merges_existing_and_puts(authed):
    book = {"id": 7, "metadata": {"tags": ["keep"]}}
    authed.get(f"{BASE}/api/v1/books/7").mock(return_value=httpx.Response(200, json=book))
    put = authed.put(f"{BASE}/api/v1/books/7/metadata").mock(
        return_value=httpx.Response(200, json={})
    )

    async with Client(mcp) as c:
        await c.call_tool("add_tags", {"book_id": 7, "tags": ["new", "keep"]})

    body = json.loads(put.calls.last.request.content)
    assert body["metadata"]["tags"] == ["keep", "new"]  # merged, deduped, order preserved
    assert body["clearFlags"] == {}
    # Must touch ONLY tags — REPLACE_ALL would wipe title/authors/description/ISBNs.
    assert put.calls.last.request.url.params["replaceMode"] == "REPLACE_WHEN_PROVIDED"


async def test_bulk_update_reports_partial_success(authed):
    authed.get(f"{BASE}/api/v1/books/1").mock(
        return_value=httpx.Response(200, json={"id": 1, "metadata": {"tags": []}})
    )
    authed.put(f"{BASE}/api/v1/books/1/metadata").mock(return_value=httpx.Response(200, json={}))
    authed.get(f"{BASE}/api/v1/books/999").mock(return_value=httpx.Response(404, text="nope"))

    async with Client(mcp) as c:
        result = await c.call_tool(
            "bulk_update_metadata",
            {"book_ids": [1, 999], "patch": {"tags": {"add": ["t"]}}},
        )

    data = result.data
    assert data["updated"] == [1]
    assert data["failed"][0]["book_id"] == 999


async def test_ping_reports_authenticated_with_version_and_libraries(authed):
    authed.get(f"{BASE}/api/v1/healthcheck").mock(
        return_value=httpx.Response(200, json={"data": {"version": "1.2.3"}})
    )
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_BOOKS))
    authed.get(f"{BASE}/api/v1/libraries").mock(return_value=httpx.Response(200, json=[{"id": 1}]))

    async with Client(mcp) as c:
        result = await c.call_tool("ping", {})

    d = result.data
    assert d["ok"] is True and d["authenticated"] is True
    assert d["book_count"] == 2
    assert d["server_version"] == "1.2.3"
    assert d["library_count"] == 1


async def test_set_field_locks_builds_field_actions(authed):
    route = authed.put(f"{BASE}/api/v1/books/metadata/toggle-field-locks").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with Client(mcp) as c:
        await c.call_tool(
            "set_field_locks",
            {"book_ids": [5], "lock": ["tags", "authors"], "unlock": ["description"]},
        )
    body = json.loads(route.calls.last.request.content)
    assert body["bookIds"] == [5]
    assert body["fieldActions"] == {
        "tagsLocked": "LOCK",
        "authorsLocked": "LOCK",
        "descriptionLocked": "UNLOCK",
    }


async def test_list_authors_uses_authors_endpoint(authed):
    authed.get(f"{BASE}/api/v1/authors").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "Ann", "bookCount": 3},
                {"id": 2, "name": "Bob", "bookCount": 1},
            ],
        )
    )
    async with Client(mcp) as c:
        result = await c.call_tool("list_authors", {"query": "an"})

    d = result.data
    assert d["total"] == 1
    assert d["authors"][0]["name"] == "Ann"
    assert d["authors"][0]["book_count"] == 3


async def test_fetch_metadata_candidates_defaults_query_and_reports_status(authed):
    # No isbn/title/author passed -> defaulted from the book's own stored metadata.
    authed.get(f"{BASE}/api/v1/books/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7,
                "title": "Stored Title",
                "metadata": {"isbn13": "111", "authors": ["Ann"]},
            },
        )
    )
    authed.get(f"{BASE}/api/v1/settings").mock(
        return_value=httpx.Response(
            200, json={"metadataProviderSettings": {"google": {"enabled": True}}}
        )
    )
    sse = (
        'data: {"title": "Cand A", "provider": "Google"}\n\n'
        'data: {"title": "Cand B", "provider": "Google"}\n\n'
    )
    route = authed.post(f"{BASE}/api/v1/books/7/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates", {"book_id": 7, "providers": ["Google"]}
        )

    d = result.data
    assert d["count"] == 2
    assert {c["title"] for c in d["candidates"]} == {"Cand A", "Cand B"}
    # P1: search terms came from stored metadata, and were sent to the backend.
    assert d["query"] == {"isbn": "111", "title": "Stored Title", "author": "Ann"}
    body = json.loads(route.calls.last.request.content)
    assert (body["isbn"], body["title"], body["author"]) == ("111", "Stored Title", "Ann")
    # P0: per-provider status surfaces the result.
    assert d["provider_status"] == [{"provider": "Google", "status": "ok", "count": 2}]


async def test_fetch_metadata_candidates_flags_empty_and_disabled(authed):
    authed.get(f"{BASE}/api/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadataProviderSettings": {
                    "amazon": {"enabled": True, "cookie": ""},
                    "goodReads": {"enabled": False},
                }
            },
        )
    )
    # Both providers ran but the stream carried no candidates for either.
    authed.post(f"{BASE}/api/v1/books/9/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates",
            {"book_id": 9, "providers": ["Amazon", "GoodReads"], "title": "x"},
        )

    statuses = {s["provider"]: s for s in result.data["provider_status"]}
    assert statuses["GoodReads"]["status"] == "disabled"
    assert statuses["Amazon"]["status"] == "empty"
    assert "cookie" in statuses["Amazon"]["reason"]


async def test_export_library_fields_projection_fetches_full(authed):
    books = [
        {"id": 1, "title": "T", "metadata": {"asin": "B01", "isbn13": "111", "authors": ["Ann"]}}
    ]
    route = authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(mcp) as c:
        result = await c.call_tool(
            "export_library", {"format": "json", "fields": ["id", "asin", "authors"]}
        )

    d = result.data
    assert d["fields"] == ["id", "asin", "authors"]
    assert json.loads(d["content"]) == [{"id": 1, "asin": "B01", "authors": ["Ann"]}]
    # asin is stripped from the plain list response -> withDescription must be requested.
    assert route.calls.last.request.url.params["withDescription"] == "true"


async def test_bulk_set_read_status_groups_by_status(authed):
    route = authed.post(f"{BASE}/api/v1/books/status").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "bulk_set_read_status",
            {
                "items": [
                    {"book_id": 1, "status": "READ"},
                    {"book_id": 2, "status": "READ"},
                    {"book_id": 3, "status": "READING"},
                    {"book_id": 4, "status": "NONSENSE"},
                ]
            },
        )

    d = result.data
    assert sorted(d["updated"]) == [1, 2, 3]
    assert d["by_status"] == {"READ": 2, "READING": 1}
    assert d["failed"][0]["book_id"] == 4
    assert route.call_count == 2  # one call per distinct valid status, not per book


async def test_find_duplicates_wraps_native_endpoint(authed):
    authed.get(f"{BASE}/api/v1/libraries").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    groups = [
        {
            "matchReason": "TITLE_AUTHOR",
            "suggestedTargetBookId": 95,
            "books": [
                {"id": 95, "title": "CKA", "metadata": {"isbn13": "111", "authors": ["A"]}},
                {"id": 159, "title": "CKA", "metadata": {"isbn13": "222", "authors": ["A"]}},
            ],
        }
    ]
    route = authed.post(f"{BASE}/api/v1/books/duplicates").mock(
        return_value=httpx.Response(200, json=groups)
    )
    async with Client(mcp) as c:
        result = await c.call_tool("find_duplicates", {"preset": "balanced"})

    d = result.data
    assert d["libraries_scanned"] == 1 and d["group_count"] == 1
    g = d["groups"][0]
    assert g["match_reason"] == "TITLE_AUTHOR"
    assert g["suggested_target_book_id"] == 95
    assert g["distinct_isbns"] is True  # different ISBNs -> likely separate editions
    body = json.loads(route.calls.last.request.content)
    assert body["matchByIsbn"] and body["matchByExternalId"] and body["matchByTitleAuthor"]
    assert body["matchByDirectory"] is False and body["matchByFilename"] is False


async def test_fetch_metadata_candidates_flags_disabled_provider_fields(authed):
    # GoodReads runs and returns a candidate, but the Goodreads Rating field is toggled
    # off in Settings > Metadata 2 -> surfaced so the caller knows why ratings never land.
    authed.get(f"{BASE}/api/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadataProviderSettings": {"goodReads": {"enabled": True}},
                "metadataProviderSpecificFields": {"goodreadsRating": False, "goodreadsId": True},
            },
        )
    )
    sse = 'data: {"title": "DDIA", "provider": "GoodReads", "goodreadsId": "23463279"}\n\n'
    authed.post(f"{BASE}/api/v1/books/21/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates",
            {"book_id": 21, "providers": ["GoodReads"], "title": "DDIA"},
        )

    gr = result.data["provider_status"][0]
    assert gr["status"] == "ok" and gr["count"] == 1
    assert gr["disabled_fields"] == ["goodreadsRating"]


async def test_export_library_paginates_and_reports_total(authed):
    books = [{"id": i, "title": f"B{i}", "metadata": {}} for i in range(5)]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(mcp) as c:
        result = await c.call_tool(
            "export_library", {"format": "json", "fields": ["id"], "offset": 1, "limit": 2}
        )

    d = result.data
    assert (d["total"], d["offset"], d["limit"], d["returned"]) == (5, 1, 2, 2)
    assert json.loads(d["content"]) == [{"id": 1}, {"id": 2}]


async def test_normalize_goodreads_ids_dry_run_then_apply(authed):
    books = [
        {
            "id": 21,
            "title": "DDIA",
            "metadata": {"goodreadsId": "23463279-designing-data-intensive"},
        },
        {"id": 22, "title": "Clean", "metadata": {"goodreadsId": "171712768"}},
    ]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    put = authed.put(f"{BASE}/api/v1/books/21/metadata").mock(
        return_value=httpx.Response(200, json={})
    )
    async with Client(mcp) as c:
        dry = await c.call_tool("normalize_goodreads_ids", {})
        # Only the slug record is flagged; the already-numeric one is left alone.
        assert dry.data["dry_run"] is True
        assert dry.data["to_change"] == [
            {"book_id": 21, "from": "23463279-designing-data-intensive", "to": "23463279"}
        ]
        applied = await c.call_tool("normalize_goodreads_ids", {"dry_run": False})

    assert applied.data["changed"][0]["book_id"] == 21
    body = json.loads(put.calls.last.request.content)
    assert body["metadata"] == {"goodreadsId": "23463279"}  # only the one field is touched


# --- kind / series-number helpers -------------------------------------------


def test_book_kind_from_primary_file_type():
    assert helpers._book_kind({"primaryFile": {"bookType": "CBX"}}) == "comic"
    assert helpers._book_kind({"primaryFile": {"bookType": "AUDIOBOOK"}}) == "audiobook"
    assert helpers._book_kind({"primaryFile": {"bookType": "EPUB"}}) == "book"
    assert helpers._book_kind({}) == "book"  # no file info -> treat as a book


def test_series_number_prefers_series_number_then_numeric_issue():
    assert helpers._series_number({"metadata": {"seriesNumber": 3}}) == 3.0
    comic = {"metadata": {"comicMetadata": {"issueNumber": " 12 "}}}
    assert helpers._series_number(comic) == 12.0
    annual = {"metadata": {"comicMetadata": {"issueNumber": "Annual 1"}}}
    assert helpers._series_number(annual) is None
    assert helpers._series_number({"metadata": {}}) is None


# --- title / genre search ----------------------------------------------------

_TITLED = [
    {
        "id": 1,
        "title": "The Art of War",
        "libraryId": 1,
        "shelves": [{"id": 5, "name": "Favourites"}],
        "metadata": {"authors": ["Sun Tzu"], "categories": ["Strategy"]},
    },
    {
        "id": 2,
        "title": "Dune",
        "libraryId": 1,
        "metadata": {"subtitle": "A war in the desert", "authors": []},
    },
    {"id": 3, "title": "Peace", "libraryId": 1, "metadata": {"authors": ["Warren Smith"]}},
    {
        "id": 4,
        "title": "War Comics",
        "libraryId": 2,
        "primaryFile": {"bookType": "CBX"},
        "metadata": {"authors": []},
    },
    {
        "id": 5,
        "title": "Leviathan Wakes",
        "libraryId": 1,
        "metadata": {"seriesName": "The Expanse"},
    },
    {"id": 6, "title": "Sapiens", "libraryId": 1, "metadata": {"tags": ["war history"]}},
    {"id": 7, "title": "Foundation", "libraryId": 1, "metadata": {}},
]


def test_normalize_text_folds_case_diacritics_and_punctuation():
    assert helpers._normalize_text("  Ender's  POKÉMON!  ") == "ender s pokemon"


def test_fuzzy_score_substring_typo_and_neighbouring_word():
    assert helpers._fuzzy_score("dune", "Dune Messiah") == 1.0  # substring inside long title
    assert helpers._fuzzy_score("pokemon", "Pokémon Adventures") == 1.0  # diacritics ignored
    assert helpers._fuzzy_score("foundaton", "Foundation and Empire") >= 0.88  # typo
    assert helpers._fuzzy_score("hary potter", "Harry Potter and the Goblet") >= 0.88
    assert helpers._fuzzy_score("witcher", "The Watcher") < 0.88  # a different word
    assert helpers._fuzzy_score("anything", "") == 0.0
    # Text with fewer words than the query is still compared, not skipped.
    assert helpers._fuzzy_score("hary potter and the", "Harry Potter") > 0.0


def test_suggest_names_tolerates_typos_and_word_order():
    counts = {"Brandon Sanderson": 12, "Brian Sanders": 1, "Ursula K. Le Guin": 4}
    names = [s.name for s in helpers._suggest_names("Sanderson, Brandon", counts)]
    assert names[0] == "Brandon Sanderson"
    assert "Ursula K. Le Guin" not in names
    top = helpers._suggest_names("Ursla Le Guin", counts)[0]
    assert (top.name, top.book_count) == ("Ursula K. Le Guin", 4)
    assert helpers._suggest_names("zzzz", counts) == []


def test_name_counts_merges_case_and_counts_each_book_once():
    counts = helpers._name_counts([["SciFi", "scifi"], ["SCIFI"], ["Horror"]])
    assert counts == {"SciFi": 2, "Horror": 1}


def test_validate_filters_rejects_unknown_keys_values_and_missing_fields():
    helpers._validate_filters({"tags": ["x"], "kind": "comic", "missing": ["isbn"]})
    with pytest.raises(BookLoreError, match="Unknown filter key.*shelf.*Valid:.*shelf_ids"):
        helpers._validate_filters({"shelf": 5})
    with pytest.raises(BookLoreError, match="Invalid kind 'comics'"):
        helpers._validate_filters({"kind": "comics"})
    with pytest.raises(BookLoreError, match="Invalid tags_mode"):
        helpers._validate_filters({"tags_mode": "every"})
    with pytest.raises(BookLoreError, match="Unknown missing field.*isbn99"):
        helpers._validate_filters({"missing": ["isbn99"]})


async def test_search_books_rejects_unknown_filter_key(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_TITLED))
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="Unknown filter key"):
            await c.call_tool("search_books", {"filters": {"shelf": 5}})


async def test_search_titles_matches_title_subtitle_series_and_tags_not_authors(authed):
    route = authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_TITLED))
    async with Client(mcp) as c:
        war = await c.call_tool("search_titles", {"query": "war"})
        series = await c.call_tool("search_titles", {"query": "expanse"})
        typo = await c.call_tool("search_titles", {"query": "foundaton"})

    # Exact hits sorted by title: subtitle, tag, title, title (comic). No author hit (#3).
    assert [b.id for b in war.data.books] == [2, 6, 1, 4]
    assert [b.matched_on for b in war.data.books] == ["subtitle", "tag", "title", "title"]
    assert all(b.score == 1.0 for b in war.data.books)
    assert war.data.books[3].kind == "comic"
    assert [(b.id, b.matched_on) for b in series.data.books] == [(5, "series")]
    assert [b.id for b in typo.data.books] == [7]
    assert 0.88 <= typo.data.books[0].score < 1.0  # fuzzy, not verbatim
    # Subtitles are stripped from the plain list response -> full records requested.
    assert route.calls.last.request.url.params["withDescription"] == "true"


async def test_search_titles_ranks_exact_hits_before_fuzzy_ones(authed):
    books = [
        {"id": 1, "title": "A Misborn Tale", "metadata": {}},
        {"id": 2, "title": "Mistborn", "metadata": {}},
    ]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(mcp) as c:
        result = await c.call_tool("search_titles", {"query": "mistborn"})

    assert [b.id for b in result.data.books] == [2, 1]  # exact first despite title order


async def test_search_titles_scopes_only_when_asked(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_TITLED))
    async with Client(mcp) as c:
        everything = await c.call_tool("search_titles", {"query": "war"})
        comics = await c.call_tool("search_titles", {"query": "war", "kind": "comic"})
        library = await c.call_tool("search_titles", {"query": "war", "library_id": 2})
        shelf = await c.call_tool("search_titles", {"query": "war", "shelf_id": 5})
        genre = await c.call_tool("search_titles", {"query": "war", "category": "strategy"})

    assert everything.data.total == 4  # whole library, every kind, by default
    assert [b.id for b in comics.data.books] == [4]
    assert [b.id for b in library.data.books] == [4]
    assert [b.id for b in shelf.data.books] == [1]
    assert [b.id for b in genre.data.books] == [1]


async def test_search_books_query_uses_light_list_and_skips_subtitles(authed):
    route = authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_TITLED))
    async with Client(mcp) as c:
        subtitle_only = await c.call_tool("search_books", {"query": "desert"})
        title = await c.call_tool("search_books", {"query": "dune"})

    # Subtitles aren't in the light list, so they're not matched even when present.
    assert subtitle_only.data.total == 0
    assert [b.id for b in title.data.books] == [2]
    assert route.calls.last.request.url.params["withDescription"] == "false"


async def test_search_books_query_matches_series_name(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_TITLED))
    async with Client(mcp) as c:
        result = await c.call_tool("search_books", {"query": "expanse"})

    assert [b.id for b in result.data.books] == [5]


_GENRED = [
    {"id": 1, "title": "B", "metadata": {"categories": ["Science Fiction", "Classics"]}},
    {"id": 2, "title": "A", "metadata": {"categories": ["science fiction"]}},
    {
        "id": 3,
        "title": "C",
        "primaryFile": {"bookType": "CBX"},
        "metadata": {"categories": ["Science Fiction", "SCIENCE FICTION"]},
    },
    {"id": 4, "title": "D", "metadata": {}},
]


async def test_list_genres_merges_case_and_counts_books(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_GENRED))
    async with Client(mcp) as c:
        result = await c.call_tool("list_genres", {})
        filtered = await c.call_tool("list_genres", {"query": "class"})

    assert result.data == {
        "total": 2,
        "genres": [
            {"name": "Classics", "book_count": 1},
            {"name": "Science Fiction", "book_count": 3},  # duplicate casing counted once
        ],
    }
    assert [g["name"] for g in filtered.data["genres"]] == ["Classics"]


async def test_books_by_genre_case_insensitive_with_kind(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_GENRED))
    async with Client(mcp) as c:
        result = await c.call_tool("books_by_genre", {"genre": "SCIENCE fiction"})
        comics = await c.call_tool("books_by_genre", {"genre": "science fiction", "kind": "comic"})

    assert result.data.total == 3
    assert [b.id for b in result.data.books] == [2, 1, 3]  # sorted by title
    assert [b.id for b in comics.data.books] == [3]


# --- did-you-mean suggestions ------------------------------------------------

_AUTHORED = [
    {"id": 1, "title": "Mistborn", "metadata": {"authors": ["Brandon Sanderson"]}},
    {"id": 2, "title": "Elantris", "metadata": {"authors": ["Brandon Sanderson"]}},
    {"id": 3, "title": "Earthsea", "metadata": {"authors": ["Ursula K. Le Guin"]}},
]


async def test_books_by_author_exact_match_has_no_suggestions(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_AUTHORED))
    async with Client(mcp) as c:
        result = await c.call_tool("books_by_author", {"author": "brandon sanderson"})

    assert [b.id for b in result.data.books] == [1, 2]
    assert result.data.suggestions == []


async def test_books_by_author_suggests_closest_names_on_miss(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_AUTHORED))
    async with Client(mcp) as c:
        result = await c.call_tool("books_by_author", {"author": "Sanderson, Brandon"})

    assert result.data.total == 0
    top = result.data.suggestions[0]
    assert (top.name, top.book_count) == ("Brandon Sanderson", 2)


async def test_books_by_genre_suggests_closest_genres_on_miss(authed):
    books = [
        {"id": 1, "title": "A", "metadata": {"categories": ["Science Fiction"]}},
        {"id": 2, "title": "B", "metadata": {"categories": ["Science Fiction", "Horror"]}},
    ]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(mcp) as c:
        result = await c.call_tool("books_by_genre", {"genre": "Sci-Fi"})

    assert result.data.total == 0
    assert [(s.name, s.book_count) for s in result.data.suggestions] == [("Science Fiction", 2)]


# --- match_books ---------------------------------------------------------------

_MATCHABLE = [
    {
        "id": 1,
        "title": "The Way of Kings",
        "primaryFile": {"bookType": "EPUB"},
        "metadata": {"authors": ["Brandon Sanderson"], "seriesName": "Stormlight Archive"},
    },
    {"id": 2, "title": "Words of Radiance", "metadata": {"authors": ["Brandon Sanderson"]}},
    {"id": 3, "title": "Dune", "metadata": {"authors": ["Frank Herbert"]}},
    {"id": 4, "title": "Dune", "metadata": {"authors": ["Frank Herbert"]}},  # duplicate
    {
        "id": 5,
        "title": "Sapiens",
        "metadata": {"subtitle": "A Brief History of Humankind", "authors": ["Yuval Noah Harari"]},
    },
]


async def _match(authed, items):
    route = authed.get(f"{BASE}/api/v1/books").mock(
        return_value=httpx.Response(200, json=_MATCHABLE)
    )
    async with Client(mcp) as c:
        result = await c.call_tool("match_books", {"items": items})
    return result.data, route


async def test_match_books_resolves_goodreads_style_rows(authed):
    data, route = await _match(
        authed,
        [
            {
                "title": "The Way of Kings (The Stormlight Archive, #1)",
                "author": "Sanderson, Brandon",
            },
            {"title": "Words of Radience", "author": "Brandon Sanderson", "rating": 5},
            {"title": "Sapiens: A Brief History of Humankind"},
        ],
    )

    assert (data.matched, data.ambiguous, data.unmatched) == (3, 0, 0)
    assert [r.book.id for r in data.results] == [1, 2, 5]
    assert data.results[0].book.kind == "book"
    assert data.results[1].input["rating"] == 5  # extra keys echoed back
    assert route.calls.last.request.url.params["withDescription"] == "true"


async def test_match_books_flags_duplicates_ambiguous_and_wrong_author_unmatched(authed):
    data, _ = await _match(
        authed,
        [
            {"title": "Dune", "author": "Frank Herbert"},
            {"title": "The Way of Kings", "author": "Frank Herbert"},
            {"title": "A Book We Do Not Own"},
        ],
    )

    dune, wrong_author, missing = data.results
    assert dune.status == "ambiguous" and dune.book is None
    assert {c.book.id for c in dune.candidates} == {3, 4}
    assert wrong_author.status == "unmatched"
    assert wrong_author.candidates[0].book.id == 1  # nearest miss still shown
    assert wrong_author.candidates[0].author_score < 0.6
    assert missing.status == "unmatched"


async def test_match_books_validates_items(authed):
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="at least one item"):
            await c.call_tool("match_books", {"items": []})
        with pytest.raises(ToolError, match=r"items\[1\] needs a non-empty"):
            await c.call_tool("match_books", {"items": [{"title": "A"}, {"author": "B"}]})
