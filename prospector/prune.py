"""Prune: keep the store inside its retention window and remove deleted content.

Reddit's Data API Terms say that content deleted on Reddit must leave your store.
The prune step enforces that and an age limit in one pass:

1. **Age.** Delete every item posted more than ``max_age_days`` ago (default 7,
   by ``created_utc``). Because an item is always fetched after it was posted,
   this also limits how long any stored copy can exist.
2. **Deleted in the store.** Delete every item whose stored text or author now
   reads ``[deleted]`` or ``[removed]``. A later sweep that re-reads an item
   writes that state into the store.
3. **Deleted on Reddit (recheck).** Ask Reddit for the current state of the
   remaining items, 100 per request (``/api/info``). Delete an item when Reddit
   returns it as deleted or removed, or leaves it out of a non-empty answer
   (Reddit leaves out ids that no longer exist). Items fetched in the last
   ``skip_fresh_hours`` are not rechecked, because the run that fetched them
   already saw their current state.

Every deleted item also loses its lexicon matches, its embedding bookkeeping
row and its vector. :func:`purge_cache` removes old response-cache files, which
hold raw Reddit content too.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_SKIP_FRESH_HOURS",
    "DEFAULT_CACHE_MAX_AGE_HOURS",
    "PruneResult",
    "is_gone",
    "is_gone_data",
    "delete_items",
    "prune",
    "purge_cache",
]

DEFAULT_MAX_AGE_DAYS = 7.0
DEFAULT_SKIP_FRESH_HOURS = 12.0
DEFAULT_CACHE_MAX_AGE_HOURS = 24.0

#: Stop the recheck after this many failed requests in a row.
_MAX_FAILED_BATCHES = 3
_RECHECK_BATCH = 100
_DELETE_CHUNK = 500

# Text Reddit shows in place of content that was deleted or removed.
_GONE_TEXT = frozenset(
    {
        "[deleted]",
        "[removed]",
        "[deleted by user]",
        "[removed by reddit]",
        "[removed by moderator]",
    }
)
_BRACKET_SPACES = re.compile(r"\[\s*(.*?)\s*\]")


def _norm(text: Optional[str]) -> str:
    collapsed = " ".join((text or "").split()).lower()
    return _BRACKET_SPACES.sub(r"[\1]", collapsed)


def is_gone(title: Optional[str], body: Optional[str], author: Optional[str]) -> bool:
    """``True`` when an item reads as deleted or removed on Reddit.

    The rule is deliberately broad: a ``[deleted]`` author (a deleted account)
    also counts, so the store never keeps text that Reddit has unlinked from
    its writer.
    """
    if _norm(author) == "[deleted]":
        return True
    return _norm(body) in _GONE_TEXT or _norm(title) in _GONE_TEXT


def is_gone_data(data: dict) -> bool:
    """:func:`is_gone` for a raw ``.json`` / RSS ``data`` dict."""
    if data.get("removed_by_category"):
        return True
    body = data.get("selftext") if "selftext" in data else data.get("body")
    return is_gone(data.get("title"), body, data.get("author", "[deleted]"))


@dataclass
class PruneResult:
    """Counts from one prune run."""

    expired: int = 0
    gone_in_store: int = 0
    gone_on_reddit: int = 0
    missing_on_reddit: int = 0
    rechecked: int = 0
    recheck_requests: int = 0
    recheck_inconclusive: int = 0
    vectors_deleted: int = 0
    cache_files_deleted: int = 0
    remaining: int = 0

    @property
    def deleted(self) -> int:
        """Total items deleted by this run."""
        return (
            self.expired
            + self.gone_in_store
            + self.gone_on_reddit
            + self.missing_on_reddit
        )

    def as_dict(self) -> dict:
        out = asdict(self)
        out["deleted"] = self.deleted
        return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
        (name,),
    ).fetchone()
    return row is not None


def _chunks(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def delete_items(conn: sqlite3.Connection, ids: Iterable[str]) -> tuple[int, int]:
    """Delete items and everything derived from them, in one transaction.

    Removes the ``items`` rows, their ``matches``, their ``item_embeddings``
    rows and their vectors. Returns ``(items_deleted, vectors_deleted)``.

    Raises :class:`prospector.semantic.SemanticUnavailable` when the store has a
    vector table but ``sqlite-vec`` is not installed: the prune must not leave
    vectors of deleted content behind.
    """
    unique = [i for i in dict.fromkeys(ids) if i]
    if not unique:
        return 0, 0
    has_meta = _table_exists(conn, "item_embeddings")
    has_vec = _table_exists(conn, "item_vectors")
    if has_vec:
        from prospector import semantic  # local import: optional dependency

        semantic.load_extension(conn)

    items_deleted = 0
    vectors_deleted = 0
    with conn:
        for chunk in _chunks(unique, _DELETE_CHUNK):
            marks = ", ".join("?" for _ in chunk)
            if has_meta:
                vectors_deleted += conn.execute(
                    f"SELECT COUNT(*) FROM item_embeddings WHERE item_id IN ({marks})",
                    chunk,
                ).fetchone()[0]
                conn.execute(
                    f"DELETE FROM item_embeddings WHERE item_id IN ({marks})", chunk
                )
            if has_vec:
                conn.executemany(
                    "DELETE FROM item_vectors WHERE item_id = ?", [(i,) for i in chunk]
                )
            conn.execute(f"DELETE FROM matches WHERE item_id IN ({marks})", chunk)
            cur = conn.execute(f"DELETE FROM items WHERE id IN ({marks})", chunk)
            items_deleted += max(cur.rowcount, 0)
    return items_deleted, vectors_deleted


def _recheck(
    conn: sqlite3.Connection,
    client,
    fresh_after: int,
    result: PruneResult,
    log: Callable[[str], object],
) -> tuple[list[str], list[str]]:
    """Ask Reddit for the state of stored items. Returns (gone_ids, missing_ids)."""
    rows = conn.execute(
        "SELECT id FROM items WHERE fetched_at < ? ORDER BY created_utc DESC",
        (int(fresh_after),),
    ).fetchall()
    candidates = [r[0] for r in rows if str(r[0]).startswith(("t1_", "t3_"))]
    gone: list[str] = []
    missing: list[str] = []
    failed_in_a_row = 0
    for batch in _chunks(candidates, _RECHECK_BATCH):
        if failed_in_a_row >= _MAX_FAILED_BATCHES:
            result.recheck_inconclusive += 1
            continue
        result.recheck_requests += 1
        try:
            entries = client.info(batch)
        except Exception as exc:  # noqa: BLE001 - one failed batch must not stop the prune
            failed_in_a_row += 1
            result.recheck_inconclusive += 1
            log(f"[prune] recheck of {len(batch)} items failed: {exc}")
            if failed_in_a_row >= _MAX_FAILED_BATCHES:
                log(
                    f"[prune] {_MAX_FAILED_BATCHES} recheck requests failed in a row; "
                    "the next run rechecks the rest"
                )
            continue
        if not entries:
            # An empty answer can be a soft rate limit. Never read it as
            # "everything was deleted".
            result.recheck_inconclusive += 1
            log(f"[prune] Reddit returned nothing for {len(batch)} ids; kept them")
            continue
        failed_in_a_row = 0
        result.rechecked += len(batch)
        by_name = {
            str(e.get("name") or ""): e for e in entries if isinstance(e, dict)
        }
        for item_id in batch:
            data = by_name.get(item_id)
            if data is None:
                missing.append(item_id)
            elif is_gone_data(data):
                gone.append(item_id)
    return gone, missing


def prune(
    conn: sqlite3.Connection,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: Optional[int] = None,
    client=None,
    skip_fresh_hours: float = DEFAULT_SKIP_FRESH_HOURS,
    log: Callable[[str], object] = print,
) -> PruneResult:
    """Prune the store on ``conn`` and return the counts.

    Args:
        conn: An open connection to a prospector store (``Store.conn``).
        max_age_days: Delete items posted longer ago than this.
        now: Unix time to measure ages from (default: the current time).
        client: Optional object with ``info(fullnames) -> list[dict]`` (a
            :class:`~prospector.reddit_client.RedditClient`). When it is given,
            the remaining items are rechecked on Reddit.
        skip_fresh_hours: Items fetched more recently than this are not
            rechecked.
        log: Where progress lines go.
    """
    if now is None:
        now = int(time.time())
    result = PruneResult()
    cutoff = int(now - float(max_age_days) * 86400)

    expired = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE created_utc < ?", (cutoff,)
        ).fetchall()
    ]
    expired_set = set(expired)
    gone_in_store = [
        r[0]
        for r in conn.execute("SELECT id, title, body, author FROM items").fetchall()
        if r[0] not in expired_set and is_gone(r[1], r[2], r[3])
    ]
    deleted, vectors = delete_items(conn, expired + gone_in_store)
    result.vectors_deleted += vectors
    result.expired = len(expired)
    result.gone_in_store = len(gone_in_store)
    if deleted != len(expired) + len(gone_in_store):  # pragma: no cover - defensive
        log(f"[prune] expected to delete {len(expired) + len(gone_in_store)}, deleted {deleted}")

    if client is not None:
        fresh_after = int(now - float(skip_fresh_hours) * 3600)
        gone, missing = _recheck(conn, client, fresh_after, result, log)
        _, vectors = delete_items(conn, gone + missing)
        result.vectors_deleted += vectors
        result.gone_on_reddit = len(gone)
        result.missing_on_reddit = len(missing)

    result.remaining = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return result


def purge_cache(
    cache_dir: Path | str,
    max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS,
    now: Optional[float] = None,
) -> int:
    """Delete response-cache files older than ``max_age_hours``. Returns the count.

    The cache holds raw Reddit responses, so it must not outlive the store's
    retention. Only ``*.json`` and ``*.json.tmp`` files directly inside
    ``cache_dir`` are touched.
    """
    folder = Path(cache_dir)
    if not folder.is_dir():
        return 0
    if now is None:
        now = time.time()
    cutoff = now - float(max_age_hours) * 3600
    removed = 0
    for path in list(folder.glob("*.json")) + list(folder.glob("*.json.tmp")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
