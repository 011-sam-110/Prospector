"""FastMCP server — exposes the prospector engine to an MCP client (Claude).

This module wraps the engine modules (profiles, store, reddit client, scrape) as
a small set of named tools. The tool *names* are a contract Claude depends on, so
do not rename them. Each tool returns plain JSON-friendly values (dataclasses are
serialized to dicts) and carries a concise docstring — Claude reads those
docstrings to decide when to call a tool.

Run it over stdio with ``python -m prospector.mcp_server`` (or
``prospector mcp`` via the CLI). A shared :class:`~prospector.store.Store` and
:class:`~prospector.reddit_client.RedditClient` are created lazily on first use;
the database path comes from the ``PROSPECTOR_DB`` env var (default
``prospector.db``).
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from fastmcp import FastMCP

from prospector.models import Item
from prospector.profiles import list_profiles, load_profile
from prospector.reddit_client import RedditClient
from prospector.scrape import sweep
from prospector.store import Store

# --------------------------------------------------------------------------- #
# Server instance + lazy shared engine handles                                #
# --------------------------------------------------------------------------- #
mcp = FastMCP("prospector")

_DEFAULT_DB = "prospector.db"

_store: Optional[Store] = None
_client: Optional[RedditClient] = None


def _db_path() -> str:
    """Database path for the shared store (``PROSPECTOR_DB`` env or default)."""
    return os.environ.get("PROSPECTOR_DB", _DEFAULT_DB)


def get_store() -> Store:
    """Return the process-wide :class:`Store`, opening it on first use."""
    global _store
    if _store is None:
        _store = Store(_db_path())
    return _store


def get_client() -> RedditClient:
    """Return the process-wide :class:`RedditClient` (OAuth auto from env)."""
    global _client
    if _client is None:
        _client = RedditClient()
    return _client


# --------------------------------------------------------------------------- #
# Serialization helpers                                                        #
# --------------------------------------------------------------------------- #
def _now() -> int:
    """Current unix time as an int (kept out of tool scopes that shadow ``time``)."""
    return int(time.time())


def _to_dict(obj: Any) -> Any:
    """Serialize a dataclass instance (recursively) to a plain dict.

    Non-dataclass values pass through unchanged; ``None`` stays ``None``. This is
    what every tool funnels its return value through so the wire payload is plain
    JSON (dicts, lists, strings, numbers).
    """
    if obj is None:
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def _serialize_list(objs: Any) -> list[dict]:
    """Serialize an iterable of dataclasses to a list of dicts."""
    return [_to_dict(o) for o in (objs or [])]


def _fullname(post_id: str, kind: str = "t3_") -> str:
    """Normalize a bare id or fullname to a Reddit fullname (default ``t3_``)."""
    pid = (post_id or "").strip()
    if pid.startswith(("t1_", "t3_")):
        return pid
    return f"{kind}{pid}"


# --------------------------------------------------------------------------- #
# Tools — profiles                                                            #
# --------------------------------------------------------------------------- #
@mcp.tool
def reddit_profiles() -> list[str]:
    """List the available topic profiles by name (e.g. ``hospital-tech``).

    A profile defines which subreddits to mine, the search terms, and the pain
    lexicon. Use this first to discover what can be swept or queried.
    """
    return list(list_profiles())


@mcp.tool
def reddit_profile_get(name: str) -> dict:
    """Return one profile's full configuration as a dict.

    Includes its subreddits, search_terms, pain_lexicon (weighted regex rules),
    pain_threshold and the evidence thresholds that gate reported gaps.
    """
    return _to_dict(load_profile(name))


# --------------------------------------------------------------------------- #
# Tools — sweep (live; writes to the store)                                   #
# --------------------------------------------------------------------------- #
@mcp.tool
def reddit_sweep(
    profile: str,
    time: str = "",
    limit: int = 0,
    max_threads: int = 0,
) -> dict:
    """Run a full two-stage sweep for a profile and persist the results.

    Stage 1 pulls + scores posts across the profile's subreddits and search
    terms; stage 2 deep-fetches comments on the highest-pain threads. This makes
    live Reddit calls and may take a while. ``time`` (hour|day|week|month|year|all),
    ``limit`` (posts per sub) and ``max_threads`` override the profile defaults
    when non-empty/non-zero. Returns a SweepResult summary (counts, per-subreddit
    tallies, top matched patterns).
    """
    prof = load_profile(profile)
    result = sweep(
        prof,
        get_client(),
        get_store(),
        time_window=time or None,
        listing_limit=limit or None,
        max_threads=max_threads or None,
    )
    return result.as_dict()


# --------------------------------------------------------------------------- #
# Tools — live drilling (search / thread); writes to the store                #
# --------------------------------------------------------------------------- #
@mcp.tool
def reddit_search(
    query: str,
    subreddits: list[str] = [],
    sort: str = "relevance",
    time: str = "year",
    limit: int = 50,
) -> list[dict]:
    """Search Reddit live for a query and store the matching posts.

    If ``subreddits`` is given the search is restricted to each of them (results
    merged + de-duplicated); otherwise it is a site-wide search. Posts are stored
    so you can drill into them later with ``reddit_query`` /
    ``reddit_get_evidence`` / ``reddit_fetch_thread``. Returns the stored posts as
    dicts (id, subreddit, author, score, permalink, title, body, ...).
    """
    client = get_client()
    store = get_store()
    now = _now()

    raw: list[dict] = []
    subs = [s for s in (subreddits or []) if s]
    if subs:
        for sub in subs:
            try:
                raw.extend(
                    client.search(
                        query,
                        subreddit=sub,
                        sort=sort,
                        time_filter=time,
                        limit=limit,
                        restrict_sr=True,
                    )
                )
            except Exception:  # one bad sub must not kill the whole search
                continue
    else:
        raw = client.search(
            query,
            subreddit=None,
            sort=sort,
            time_filter=time,
            limit=limit,
            restrict_sr=False,
        )

    items: list[Item] = []
    seen: set[str] = set()
    for data in raw:
        item = Item.from_reddit(data, kind="post", profile=None, fetched_at=now)
        if item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)

    if items:
        try:
            store.upsert_items(items)
        except Exception:
            pass
    return _serialize_list(items)


@mcp.tool
def reddit_fetch_thread(
    post_id: str,
    max_comments: int = 40,
    min_score: int = 0,
) -> dict:
    """Fetch a post's comment tree live and store the comments.

    ``post_id`` may be a bare id (``abc123``) or a fullname (``t3_abc123``).
    Returns ``{"post": <post dict or None>, "comments": [<comment dicts>],
    "comments_collected": N}``. The post object is included when it is already in
    the store (e.g. from a prior sweep/search). Comments are stored so you can
    cite them as evidence afterwards.
    """
    client = get_client()
    store = get_store()
    now = _now()

    raw_comments = client.comments(
        post_id,
        limit=max_comments,
        depth=2,
        min_score=min_score,
    )
    comments: list[Item] = []
    seen: set[str] = set()
    for data in raw_comments or []:
        item = Item.from_reddit(data, kind="comment", profile=None, fetched_at=now)
        if item.id in seen:
            continue
        seen.add(item.id)
        comments.append(item)

    if comments:
        try:
            store.upsert_items(comments)
        except Exception:
            pass

    post = store.get_item(_fullname(post_id))
    return {
        "post": _to_dict(post),
        "comments": _serialize_list(comments),
        "comments_collected": len(comments),
    }


# --------------------------------------------------------------------------- #
# Tools — read the store                                                       #
# --------------------------------------------------------------------------- #
@mcp.tool
def reddit_query(
    profile: str = "",
    subreddit: str = "",
    min_pain: float = 0.0,
    contains: str = "",
    sort: str = "pain",
    limit: int = 50,
) -> list[dict]:
    """Query already-stored items (no network).

    Filters: ``profile`` (which sweep collected it), ``subreddit``, ``min_pain``
    (lower bound on the deterministic pain score), ``contains`` (case-insensitive
    substring over title+body). ``sort`` is one of pain|score|new|comments.
    Returns hydrated item dicts including their matched lexicon patterns.
    """
    items = get_store().query(
        profile=profile or None,
        subreddit=subreddit or None,
        min_pain=min_pain,
        contains=contains or None,
        sort=sort,
        limit=limit,
    )
    return _serialize_list(items)


@mcp.tool
def reddit_get_evidence(ids: list[str]) -> list[dict]:
    """Resolve stored items into citable evidence for the given ids.

    Returns one EvidenceItem dict per known id (in the order requested) with the
    permalink and a trimmed verbatim quote — use this to ground any claim in a
    real Reddit link instead of paraphrasing. Unknown ids are skipped.
    """
    return _serialize_list(get_store().get_evidence(list(ids)))


@mcp.tool
def reddit_stats(profile: str = "") -> dict:
    """Summary stats for the store (optionally scoped to one ``profile``).

    Returns totals, the post/comment split, a per-subreddit breakdown, the most
    frequent matched patterns, and the (min, max) created-utc date range.
    """
    return get_store().stats(profile=profile or None)


@mcp.tool
def reddit_export(
    format: str = "json",
    profile: str = "",
    min_pain: float = 0.0,
    limit: int = 500,
) -> str:
    """Export stored items as a serialized string in ``json``, ``csv`` or ``md``.

    Applies the same ``profile`` / ``min_pain`` filters as ``reddit_query`` and
    returns the rendered payload as text (highest pain first). Useful for handing
    a dataset back to the user.
    """
    items = get_store().query(
        profile=profile or None,
        min_pain=min_pain,
        sort="pain",
        limit=limit,
    )
    rows = _serialize_list(items)
    fmt = (format or "json").lower()

    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False)

    columns = [
        "id",
        "kind",
        "subreddit",
        "author",
        "created_utc",
        "score",
        "num_comments",
        "pain_score",
        "permalink",
        "title",
    ]

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
        return buf.getvalue()

    if fmt in ("md", "markdown"):
        lines = [
            "| pain | subreddit | author | score | permalink |",
            "| ---: | --- | --- | ---: | --- |",
        ]
        for row in rows:
            title = (row.get("title") or row.get("body") or "").splitlines()
            label = title[0][:80] if title else ""
            link = row.get("permalink") or ""
            cell = f"[{label or 'link'}]({link})" if link else label
            lines.append(
                f"| {row.get('pain_score', 0)} | {row.get('subreddit', '')} | "
                f"{row.get('author', '')} | {row.get('score', 0)} | {cell} |"
            )
        return "\n".join(lines)

    raise ValueError(f"unknown export format: {format!r} (use json|csv|md)")


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def main() -> None:
    """Run the MCP server over stdio (the default FastMCP transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
