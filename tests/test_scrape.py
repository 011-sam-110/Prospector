"""Offline tests for the two-stage sweep (:mod:`prospector.scrape`).

Everything here runs without a network or a real database: the Reddit client is
a hand-rolled fake that returns canned ``data`` dicts (and can be told to raise
for a given subreddit or thread), and the store is a tiny in-memory stand-in
that implements just ``upsert_items`` / ``record_sweep``. The tests assert the
stage-1/stage-2 counts, that only painful (or very busy) threads get a deep
comment fetch, and that a single failing subreddit or thread never aborts the
run.
"""

from __future__ import annotations

import pytest

from prospector.models import (
    CommentConfig,
    EvidenceThresholds,
    Item,
    LexiconRule,
    Profile,
)
from prospector import scrape


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeClient:
    """Canned-response stand-in for :class:`prospector.reddit_client.RedditClient`.

    Records every call so tests can assert *which* subreddits/threads were
    actually hit, and raises for any subreddit/thread named in ``raise_subs`` /
    ``raise_comment_posts`` to exercise the resilience paths.
    """

    def __init__(
        self,
        listings=None,
        searches=None,
        comments=None,
        raise_subs=(),
        raise_comment_posts=(),
    ):
        self.listings = listings or {}
        self.searches = searches or {}
        self._comments = comments or {}
        self.raise_subs = set(raise_subs)
        self.raise_comment_posts = set(raise_comment_posts)
        self.listing_calls: list[str] = []
        self.search_calls: list[tuple[str, str | None]] = []
        self.comment_calls: list[str] = []

    def listing(self, subreddit, sort="new", limit=100, time_filter="year", pages=1):
        self.listing_calls.append(subreddit)
        if subreddit in self.raise_subs:
            raise RuntimeError(f"listing blew up for r/{subreddit}")
        return list(self.listings.get(subreddit, []))

    def search(
        self,
        query,
        subreddit=None,
        sort="relevance",
        time_filter="year",
        limit=100,
        restrict_sr=True,
    ):
        self.search_calls.append((query, subreddit))
        if subreddit in self.raise_subs:
            raise RuntimeError(f"search blew up for r/{subreddit}")
        return list(self.searches.get(subreddit, []))

    def comments(self, post_id, limit=100, depth=2, min_score=0):
        self.comment_calls.append(post_id)
        if post_id in self.raise_comment_posts:
            raise RuntimeError(f"comments blew up for {post_id}")
        return list(self._comments.get(post_id, []))


class FakeStore:
    """In-memory stand-in for :class:`prospector.store.Store`."""

    def __init__(self):
        self.items: dict[str, Item] = {}
        self.sweeps: list = []
        self.upsert_calls = 0

    def upsert_items(self, items):
        for it in items:
            self.items[it.id] = it
        self.upsert_calls += 1
        return len(items)

    def record_sweep(self, result):
        self.sweeps.append(result)


# --------------------------------------------------------------------------- #
# Fixture data                                                                 #
# --------------------------------------------------------------------------- #
def _post(id, sub, title, body="", num_comments=0, score=0):
    return {
        "name": f"t3_{id}",
        "id": id,
        "subreddit": sub,
        "author": f"u_{id}",
        "created_utc": 1700000000,
        "permalink": f"/r/{sub}/comments/{id}/post/",
        "title": title,
        "selftext": body,
        "score": score,
        "num_comments": num_comments,
    }


def _comment(id, sub, body, link_id, score=5):
    return {
        "name": f"t1_{id}",
        "id": id,
        "subreddit": sub,
        "author": f"u_{id}",
        "created_utc": 1700000001,
        "permalink": f"/r/{sub}/comments/x/{id}/",
        "body": body,
        "score": score,
        "link_id": link_id,
    }


def make_profile():
    """A small but real profile: two patterns, a high pain threshold."""
    return Profile(
        name="testprof",
        description="unit-test profile",
        subreddits=["nursing", "medicine", "boom"],  # 'boom' is wired to raise
        search_terms=["fax"],
        time_window="year",
        listing_limit=100,
        max_threads=60,
        pain_lexicon=[
            LexiconRule(pattern="i wish", weight=3.0),
            LexiconRule(pattern="manual", weight=1.0),
        ],
        pain_threshold=3.0,
        evidence=EvidenceThresholds(),
        comments=CommentConfig(max_per_thread=40, min_score=2, depth=2),
    )


def make_client(**overrides):
    listings = {
        "nursing": [
            _post("p1", "nursing", "i wish there was a better system"),
            _post("p2", "nursing", "normal day", "nothing here", num_comments=2),
        ],
        "medicine": [
            _post("p4", "medicine", "i wish we had X"),
            _post("p5", "medicine", "general chat", num_comments=80),  # busy, no pain
        ],
    }
    searches = {
        "nursing": [
            _post("p1", "nursing", "i wish there was a better system"),  # dup of listing
            _post("p3", "nursing", "manual entry is annoying", num_comments=1),
        ],
        "medicine": [],
    }
    comments = {
        "t3_p1": [
            _comment("c1", "nursing", "i wish", "t3_p1"),
            _comment("c2", "nursing", "manual", "t3_p1"),
        ],
        "t3_p4": [_comment("c3", "medicine", "manual", "t3_p4")],
        "t3_p5": [_comment("c4", "medicine", "nothing here", "t3_p5")],
    }
    kwargs = dict(
        listings=listings,
        searches=searches,
        comments=comments,
        raise_subs={"boom"},
    )
    kwargs.update(overrides)
    return FakeClient(**kwargs)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_two_stage_counts_and_dedup():
    profile = make_profile()
    client = make_client()
    store = FakeStore()
    logs: list[str] = []

    result = scrape.sweep(
        profile, client, store, run_id="RUN1", now=1700000000, log=logs.append
    )

    # Stage 1: p1 appears in both listing and search -> deduped. 5 unique posts.
    assert result.posts_collected == 5
    # Stage 2: c1,c2 (p1) + c3 (p4) + c4 (p5) = 4 comments.
    assert result.comments_collected == 4
    assert result.threads_deep_fetched == 3

    # Both stages were persisted, and the run was recorded.
    assert len(store.items) == 9  # 5 posts + 4 comments
    assert store.sweeps == [result]

    # Run metadata passes through untouched.
    assert result.run_id == "RUN1"
    assert result.profile == "testprof"
    assert result.started_at == 1700000000


def test_only_high_pain_or_busy_threads_are_deep_fetched():
    profile = make_profile()
    client = make_client()
    store = FakeStore()

    scrape.sweep(profile, client, store, now=1700000000, log=lambda *_: None)

    fetched = set(client.comment_calls)
    # p1 (pain 3) and p4 (pain 3) qualify on pain; p5 qualifies on num_comments=80.
    assert fetched == {"t3_p1", "t3_p4", "t3_p5"}
    # Low-pain, low-traffic posts are never deep-fetched.
    assert "t3_p2" not in fetched
    assert "t3_p3" not in fetched


def test_subreddit_and_pattern_tallies():
    profile = make_profile()
    client = make_client()
    store = FakeStore()

    result = scrape.sweep(profile, client, store, now=1700000000, log=lambda *_: None)

    # nursing: posts p1,p2,p3 (3) + comments c1,c2 (2) = 5
    # medicine: posts p4,p5 (2) + comments c3,c4 (2) = 4
    assert result.subreddits == {"nursing": 5, "medicine": 4}
    # 'i wish' fires on p1,p4,c1 (3); 'manual' fires on p3,c2,c3 (3).
    assert dict(result.top_patterns) == {"i wish": 3, "manual": 3}


def test_raising_subreddit_is_skipped_not_fatal():
    profile = make_profile()
    client = make_client()
    store = FakeStore()

    result = scrape.sweep(profile, client, store, now=1700000000, log=lambda *_: None)

    # The 'boom' sub was attempted...
    assert "boom" in client.listing_calls
    # ...but contributed nothing and did not abort the run.
    assert "boom" not in result.subreddits
    assert result.posts_collected == 5


def test_raising_thread_is_skipped_not_fatal():
    profile = make_profile()
    client = make_client(raise_comment_posts={"t3_p5"})
    store = FakeStore()

    result = scrape.sweep(profile, client, store, now=1700000000, log=lambda *_: None)

    # p5's comment fetch raised; the other two threads still completed.
    assert "t3_p5" in client.comment_calls  # it was attempted
    assert result.threads_deep_fetched == 2
    assert result.comments_collected == 3  # c1, c2, c3 (c4 lost with the failed thread)


def test_run_id_and_now_default_when_omitted():
    profile = make_profile()
    client = make_client()
    store = FakeStore()

    result = scrape.sweep(profile, client, store, log=lambda *_: None)

    # uuid4().hex is 32 hex chars; now defaults to a sane positive epoch.
    assert isinstance(result.run_id, str) and len(result.run_id) == 32
    assert result.started_at > 0
    assert result.finished_at >= result.started_at


def test_max_threads_override_caps_deep_fetch():
    profile = make_profile()
    client = make_client()
    store = FakeStore()

    result = scrape.sweep(
        profile, client, store, now=1700000000, max_threads=1, log=lambda *_: None
    )

    # Only the single highest-pain thread is deep-fetched.
    assert result.threads_deep_fetched == 1
    assert len(client.comment_calls) == 1


def test_empty_profile_returns_zeroed_result():
    profile = Profile(
        name="empty",
        description="",
        subreddits=[],
        search_terms=[],
        pain_lexicon=[],
    )
    client = make_client()
    store = FakeStore()

    result = scrape.sweep(profile, client, store, now=1700000000, log=lambda *_: None)

    assert result.posts_collected == 0
    assert result.comments_collected == 0
    assert result.threads_deep_fetched == 0
    assert result.subreddits == {}
    assert result.top_patterns == []
    # Nothing fetched at all.
    assert client.listing_calls == []
    assert store.sweeps == [result]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# --------------------------------------------------------------------------- #
# combine_terms (one OR-joined search per subreddit)                           #
# --------------------------------------------------------------------------- #
def test_combined_query_quotes_and_joins_terms():
    assert scrape.combined_query(["i wish there was", "fax", "  still   fax "]) == (
        '"i wish there was" OR "fax" OR "still fax"'
    )
    assert scrape.combined_query(['say "hi"']) == '"say hi"'


def test_combine_terms_sends_one_search_per_subreddit():
    prof = Profile(
        name="multi",
        description="",
        subreddits=["nursing", "medicine"],
        search_terms=["fax", "pager", "i wish there was"],
    )
    client = FakeClient()
    scrape.sweep(prof, client, FakeStore(), log=lambda _m: None, combine_terms=True)
    assert client.search_calls == [
        ('"fax" OR "pager" OR "i wish there was"', "nursing"),
        ('"fax" OR "pager" OR "i wish there was"', "medicine"),
    ]

    plain = FakeClient()
    scrape.sweep(prof, plain, FakeStore(), log=lambda _m: None)
    assert len(plain.search_calls) == 6  # default: one search per term per sub
