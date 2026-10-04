"""Two-stage Reddit sweep — the pipeline that ties client + scorer + store.

A *sweep* is the engine's data-collection loop. It runs in two deliberately
cheap-then-expensive stages so a single run can cover many subreddits without
hammering Reddit:

  * **Stage 1 (broad / cheap).** For every subreddit in the profile, pull the
    ``new`` listing *and* run a search for each of the profile's search terms.
    Every raw post is normalized into an :class:`~prospector.models.Item`,
    deduplicated by id, scored against the compiled pain lexicon, and upserted
    into the store.

  * **Stage 2 (targeted / deep).** Only the posts that actually look painful
    (``pain_score >= profile.pain_threshold``) or that drew a big discussion
    (high ``num_comments``) earn a comment-tree fetch. Those are ranked by
    pain and capped at ``max_threads``; their comments are scored and stored too.

The loop is intentionally *resilient*: a single subreddit or thread that errors
out is logged and skipped, never aborting the whole sweep. Everything that can
be injected (``run_id``, ``now``, the various budget overrides, even the ``log``
sink) is a parameter so the sweep is fully deterministic and unit-testable.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Callable, Optional

from prospector.errors import CommentsUnavailable
from prospector.models import Item, Profile, SweepResult
from prospector.scorer import compile_lexicon, score_item

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids hard import coupling
    from prospector.reddit_client import RedditClient
    from prospector.store import Store

__all__ = ["sweep", "combined_query", "HIGH_NUM_COMMENTS", "TOP_PATTERNS_LIMIT"]

#: A post with at least this many comments is deep-fetched even if its lexicon
#: pain score is below the profile threshold — a busy thread is itself a signal.
HIGH_NUM_COMMENTS: int = 50

#: How many of the most-fired lexicon patterns to keep in the run summary.
TOP_PATTERNS_LIMIT: int = 15


def combined_query(terms: list[str]) -> str:
    """Join search terms into one Reddit query: ``"a b" OR "c"``.

    Reddit search accepts ``OR`` between quoted phrases. One combined query per
    subreddit costs one request in place of one request per term, which matters
    on the RSS transport (about one request per minute).
    """
    phrases = []
    for term in terms:
        clean = " ".join(str(term).replace('"', " ").split())
        if clean:
            phrases.append(f'"{clean}"')
    return " OR ".join(phrases)


def sweep(
    profile: Profile,
    client: "RedditClient",
    store: "Store",
    run_id: Optional[str] = None,
    now: Optional[int] = None,
    time_window: Optional[str] = None,
    listing_limit: Optional[int] = None,
    max_threads: Optional[int] = None,
    log: Callable[[str], object] = print,
    combine_terms: bool = False,
    rss_comment_threads: Optional[int] = None,
) -> SweepResult:
    """Run a full two-stage sweep for ``profile`` and return a :class:`SweepResult`.

    Args:
        profile: The loaded topic profile driving the sweep (subreddits, search
            terms, lexicon, thresholds and comment budget).
        client: A :class:`~prospector.reddit_client.RedditClient` (or any object
            implementing ``listing``/``search``/``comments`` with the same
            signatures) used to fetch raw Reddit ``data`` dicts.
        store: A :class:`~prospector.store.Store` (or any object implementing
            ``upsert_items`` / ``record_sweep``) that persists scored items.
        run_id: Stable identifier for this run; defaults to ``uuid4().hex``.
        now: Wall-clock seconds stamped onto fetched items and used as the run
            start time; defaults to ``int(time.time())``.
        time_window: Override the profile's ``time_window`` (Reddit time filter).
        listing_limit: Override the profile's stage-1 ``listing_limit``.
        max_threads: Override the profile's stage-2 ``max_threads`` budget.
        log: Where human-readable progress / error lines go (defaults to
            :func:`print`); swap for a no-op or a logger in tests.
        combine_terms: When ``True``, stage 1 sends ONE search per subreddit
            with all search terms joined by ``OR`` (see :func:`combined_query`)
            in place of one search per term.
        rss_comment_threads: Override the profile's ``rss_comment_threads``.
            On the RSS transport the feeds carry no comment counts, so the
            "busy thread" rule cannot fire and few posts clear the pain
            threshold. Stage 2 then reads at least this many threads: the
            threshold candidates first, then the next posts by pain score,
            newest first on a tie. The ``.json`` path ignores this setting.

    Returns:
        A :class:`SweepResult` with post/comment counts, per-subreddit tallies
        and the most-fired lexicon patterns. The result is also persisted via
        ``store.record_sweep`` before being returned.
    """
    if run_id is None:
        run_id = uuid.uuid4().hex
    if now is None:
        now = int(time.time())
    started_at = int(now)

    tw = time_window or profile.time_window
    lim = listing_limit if listing_limit else profile.listing_limit
    thread_budget = max_threads if max_threads else profile.max_threads

    compiled = compile_lexicon(profile.pain_lexicon)

    stats_fn = getattr(client, "request_stats", None)
    stats_before = stats_fn() if callable(stats_fn) else None

    # ----------------------------------------------------------------- #
    # Shared accumulators                                               #
    # ----------------------------------------------------------------- #
    posts: dict[str, Item] = {}  # id -> scored post Item (deduped)
    comments: dict[str, Item] = {}  # id -> scored comment Item (deduped)
    sub_tally: Counter[str] = Counter()
    pattern_tally: Counter[str] = Counter()

    def _record_patterns(item: Item) -> None:
        for m in item.matches:
            pattern_tally[m.pattern] += 1

    def _ingest_post(data: object, default_sub: str) -> None:
        """Normalize, score and stash one raw post ``data`` dict (dedup by id)."""
        if not isinstance(data, dict):
            return
        try:
            item = Item.from_reddit(
                data, kind="post", profile=profile.name, fetched_at=now
            )
        except Exception as exc:  # noqa: BLE001 - one bad record can't kill the run
            log(f"[sweep] skipping malformed post in r/{default_sub}: {exc!r}")
            return
        if not item.id or item.id in posts:
            return
        score_item(item, compiled)
        posts[item.id] = item
        sub_tally[item.subreddit or default_sub] += 1
        _record_patterns(item)

    # ----------------------------------------------------------------- #
    # Stage 1 — broad / cheap: listing + search per subreddit           #
    # ----------------------------------------------------------------- #
    for sub in profile.subreddits:
        try:
            raw_listing = client.listing(
                subreddit=sub, sort="new", limit=lim, time_filter=tw, pages=1
            )
        except Exception as exc:  # noqa: BLE001 - skip this listing, keep going
            log(f"[sweep] listing r/{sub} failed: {exc!r}")
            raw_listing = []
        for data in raw_listing or []:
            _ingest_post(data, sub)

        terms = list(profile.search_terms)
        if combine_terms and len(terms) > 1:
            terms = [combined_query(terms)]
        for term in terms:
            try:
                raw_search = client.search(
                    term,
                    subreddit=sub,
                    sort="relevance",
                    time_filter=tw,
                    limit=lim,
                    restrict_sr=True,
                )
            except Exception as exc:  # noqa: BLE001 - skip this query, keep going
                log(f"[sweep] search {term!r} in r/{sub} failed: {exc!r}")
                continue
            for data in raw_search or []:
                _ingest_post(data, sub)

    if posts:
        try:
            store.upsert_items(list(posts.values()))
        except Exception as exc:  # noqa: BLE001 - persistence hiccup is non-fatal
            log(f"[sweep] upsert of {len(posts)} posts failed: {exc!r}")

    # ----------------------------------------------------------------- #
    # Stage 2 — targeted / deep: comment trees of the painful posts     #
    # ----------------------------------------------------------------- #
    threshold = profile.pain_threshold
    candidates = [
        it
        for it in posts.values()
        if it.pain_score >= threshold or it.num_comments >= HIGH_NUM_COMMENTS
    ]
    # Highest pain first; busy-but-low-pain threads fall to the back.
    candidates.sort(key=lambda it: (it.pain_score, it.num_comments), reverse=True)
    candidates = candidates[: thread_budget if thread_budget and thread_budget > 0 else 0]

    if getattr(client, "using_rss", False):
        # RSS has no comment counts: rank by pain, then by recency, and make
        # sure at least ``floor`` threads are read.
        floor = (
            rss_comment_threads
            if rss_comment_threads is not None
            else profile.rss_comment_threads
        )
        chosen = {it.id for it in candidates}
        ranked = sorted(
            posts.values(),
            key=lambda it: (it.pain_score, it.created_utc),
            reverse=True,
        )
        candidates.sort(key=lambda it: (it.pain_score, it.created_utc), reverse=True)
        for it in ranked:
            if len(candidates) >= max(0, int(floor)):
                break
            if it.id not in chosen:
                candidates.append(it)
                chosen.add(it.id)

    threads_deep_fetched = 0
    cc = profile.comments
    for post in candidates:
        try:
            raw_comments = client.comments(
                post.id,
                limit=cc.max_per_thread,
                depth=cc.depth,
                min_score=cc.min_score,
            )
        except CommentsUnavailable:
            log(f"[sweep] comments not available for {post.id} (HTTP 403); skipped")
            continue
        except Exception as exc:  # noqa: BLE001 - skip this thread, keep going
            log(f"[sweep] comments for {post.id} failed: {exc!r}")
            continue
        threads_deep_fetched += 1
        for data in raw_comments or []:
            if not isinstance(data, dict):
                continue
            try:
                citem = Item.from_reddit(
                    data, kind="comment", profile=profile.name, fetched_at=now
                )
            except Exception as exc:  # noqa: BLE001 - one bad comment is harmless
                log(f"[sweep] skipping malformed comment under {post.id}: {exc!r}")
                continue
            if not citem.id or citem.id in comments:
                continue
            score_item(citem, compiled)
            comments[citem.id] = citem
            sub_tally[citem.subreddit or post.subreddit] += 1
            _record_patterns(citem)

    if comments:
        try:
            store.upsert_items(list(comments.values()))
        except Exception as exc:  # noqa: BLE001 - persistence hiccup is non-fatal
            log(f"[sweep] upsert of {len(comments)} comments failed: {exc!r}")

    # ----------------------------------------------------------------- #
    # Build, persist and return the run summary                         #
    # ----------------------------------------------------------------- #
    result = SweepResult(
        run_id=run_id,
        profile=profile.name,
        posts_collected=len(posts),
        comments_collected=len(comments),
        threads_deep_fetched=threads_deep_fetched,
        subreddits=dict(sub_tally),
        top_patterns=pattern_tally.most_common(TOP_PATTERNS_LIMIT),
        started_at=started_at,
        finished_at=int(time.time()),
    )
    if stats_before is not None:
        stats_after = stats_fn()
        result.requests = {
            key: int(stats_after.get(key, 0)) - int(stats_before.get(key, 0))
            for key in stats_after
        }
    log(
        f"[sweep] {profile.name}: {result.posts_collected} posts, "
        f"{result.comments_collected} comments from "
        f"{result.threads_deep_fetched} deep threads "
        f"across {len(result.subreddits)} subs"
    )

    try:
        store.record_sweep(result)
    except Exception as exc:  # noqa: BLE001 - failing to log the run isn't fatal
        log(f"[sweep] record_sweep failed: {exc!r}")

    return result
