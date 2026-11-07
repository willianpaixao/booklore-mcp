"""MCP prompts: reusable, user-invokable workflows built on the tools.

Registered on the shared `mcp` by import side-effect, like the tools package.
"""

from __future__ import annotations

from .app import mcp


@mcp.prompt(title="Organize library into shelves")
def organize_library(context: str) -> str:
    """Have the assistant design shelves for a goal and file books/comics into them,
    with your approval before anything is written."""
    return f"""Organize my BookLore library into shelves for this goal: {context}

Follow these steps in order:
1. Read the library compactly: call export_library with format="csv" and
   fields=["id","title","authors","series","categories","kind"] (page with offset/limit
   if it is large). Use list_shelves to see the shelves that already exist, and
   list_genres / search_books if you need more detail.
2. Design a small, sensible set of shelves for the goal (reuse existing shelves where
   they fit) and decide which book ids belong on each. Books and comics ("kind") may go
   on the same or separate shelves — whatever suits the goal.
3. Call organize_shelves with that plan and dry_run=true. Nothing is written yet.
4. Show me the preview: each shelf, whether it is new or existing, how many books and
   comics it gets, a few example titles, and any unknown ids. Ask me to approve or
   adjust it.
5. Only after I explicitly approve, call organize_shelves again with the same (or my
   adjusted) plan and dry_run=false, then summarize what was created and assigned.
Never remove books from shelves, and never apply without my approval."""
