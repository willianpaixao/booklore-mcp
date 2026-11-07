"""`python -m booklore_mcp` entry point."""

from __future__ import annotations

from . import tools  # noqa: F401  (register tools before the server runs)
from .app import main

if __name__ == "__main__":
    main()
