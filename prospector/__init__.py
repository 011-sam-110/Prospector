"""prospector — mine Reddit for unmet needs.

A deterministic engine (the "hands") that fetches, scores, stores, and renders
Reddit signal, exposed through two interfaces:

  * a CLI (``prospector ...``) for standalone / open-source use, and
  * an MCP server (``prospector mcp``) so Claude becomes the "brain".

The engine never invents anything — it only surfaces what it actually fetched,
and the report renderer enforces an evidence-bound contract so every claimed
"gap" is traceable to real Reddit permalinks and verbatim quotes.
"""

__version__ = "0.1.0"

from prospector.models import (
    Item,
    Match,
    LexiconRule,
    EvidenceThresholds,
    CommentConfig,
    Profile,
    EvidenceItem,
    SweepResult,
)

__all__ = [
    "__version__",
    "Item",
    "Match",
    "LexiconRule",
    "EvidenceThresholds",
    "CommentConfig",
    "Profile",
    "EvidenceItem",
    "SweepResult",
]
