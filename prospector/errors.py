"""Shared exception types.

:class:`RedditError` lives here so that both transports (``reddit_client`` for
the ``.json`` API and ``reddit_rss`` for the Atom feeds) can raise it without a
circular import. ``prospector.reddit_client.RedditError`` is the same class.
"""

from __future__ import annotations

from typing import Optional


class RedditError(Exception):
    """Raised on a hard failure: exhausted retries, a blocked or non-JSON/non-feed
    response, an OAuth failure, or an error body Reddit returns inline.

    ``status`` is the HTTP status when the failure came from one (for example
    ``403``). ``blocked`` is ``True`` when Reddit served a block or login page
    in place of data. The client uses both to decide on the RSS fallback.
    """

    def __init__(
        self, message: str, status: Optional[int] = None, blocked: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.blocked = blocked


class CommentsUnavailable(RedditError):
    """A thread's comment feed was refused (HTTP 403). The sweep skips the
    thread and does not retry it."""
