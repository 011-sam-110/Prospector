"""Offline tests for :mod:`prospector.store`.

Round-trips items through a temp-file SQLite store, exercises dedup, the query
filters/sorting, evidence resolution, sweep recording, and stats. No network,
no LLM — pure local SQLite.
"""

from __future__ import annotations

import pytest

from prospector.models import Item, Match, SweepResult
from prospector.store import Store


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    """A fresh file-backed Store, closed on teardown."""
    s = Store(tmp_path / "prospector.db")
    try:
        yield s
    finally:
        s.close()


def _post(
    item_id: str,
    *,
    subreddit: str = "nursing",
    author: str = "alice",
    title: str = "title",
    body: str = "body",
    score: int = 10,
    num_comments: int = 3,
    pain: float = 5.0,
    created: int = 1000,
    profile: str = "hospital-tech",
    matches=None,
) -> Item:
    return Item(
        id=item_id,
        kind="post",
        subreddit=subreddit,
        author=author,
        created_utc=created,
        permalink=f"https://www.reddit.com/r/{subreddit}/comments/{item_id}/",
        title=title,
        body=body,
        score=score,
        num_comments=num_comments,
        pain_score=pain,
        matches=matches or [],
        profile=profile,
        fetched_at=12345,
    )


# --------------------------------------------------------------------------- #
# Schema / lifecycle
# --------------------------------------------------------------------------- #
def test_schema_created(store):
    names = {
        r[0]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"items", "matches", "sweeps"} <= names


def test_memory_db_works():
    s = Store(":memory:")
    try:
        assert s.upsert_items([_post("t3_a")]) == 1
        assert s.get_item("t3_a") is not None
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Upsert / round-trip / dedup
# --------------------------------------------------------------------------- #
def test_upsert_and_get_roundtrip(store):
    item = _post(
        "t3_a",
        matches=[Match("i wish", 3.0), Match("manual", 1.0)],
    )
    assert store.upsert_items([item]) == 1

    got = store.get_item("t3_a")
    assert got is not None
    assert got.id == "t3_a"
    assert got.subreddit == "nursing"
    assert got.author == "alice"
    assert got.pain_score == 5.0
    assert got.profile == "hospital-tech"
    # matches hydrated, order preserved
    assert [(m.pattern, m.weight) for m in got.matches] == [
        ("i wish", 3.0),
        ("manual", 1.0),
    ]


def test_get_item_unknown_returns_none(store):
    assert store.get_item("t3_missing") is None


def test_upsert_empty_returns_zero(store):
    assert store.upsert_items([]) == 0


def test_dedup_same_id_kept_once_latest_wins(store):
    store.upsert_items([_post("t3_a", score=10, pain=5.0, matches=[Match("a", 1.0)])])
    # Re-upsert the same id with new score/pain and different matches.
    store.upsert_items([_post("t3_a", score=99, pain=8.0, matches=[Match("b", 2.0)])])

    rows = store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert rows == 1  # only one physical row

    got = store.get_item("t3_a")
    assert got.score == 99  # latest score wins
    assert got.pain_score == 8.0
    # matches were replaced wholesale, not appended
    assert [(m.pattern, m.weight) for m in got.matches] == [("b", 2.0)]
    match_rows = store.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    assert match_rows == 1


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #
def test_query_filters_and_sort(store):
    store.upsert_items(
        [
            _post("t3_a", subreddit="nursing", pain=8.0, score=5, created=100,
                  num_comments=1, title="fax machines", body="we still fax"),
            _post("t3_b", subreddit="medicine", pain=2.0, score=50, created=200,
                  num_comments=20, title="hello", body="no pain words"),
            _post("t3_c", subreddit="nursing", pain=6.0, score=1, created=300,
                  num_comments=5, title="manual entry", body="double charting"),
        ]
    )
    # add a comment to test kind filtering
    comment = Item(
        id="t1_x", kind="comment", subreddit="nursing", author="bob",
        created_utc=400, permalink="https://www.reddit.com/r/nursing/comments/a/x/",
        title=None, body="i wish there was a tool", score=3, pain_score=4.0,
        profile="hospital-tech",
    )
    store.upsert_items([comment])

    # sort by pain (default) — highest first
    ids = [i.id for i in store.query()]
    assert ids[0] == "t3_a"  # pain 8 is top

    # min_pain filter
    high = store.query(min_pain=5.0)
    assert {i.id for i in high} == {"t3_a", "t3_c"}

    # subreddit filter (case-insensitive)
    nursing = store.query(subreddit="NURSING")
    assert {i.id for i in nursing} == {"t3_a", "t3_c", "t1_x"}

    # kind filter
    posts = store.query(kind="post")
    assert "t1_x" not in {i.id for i in posts}
    comments = store.query(kind="comment")
    assert {i.id for i in comments} == {"t1_x"}

    # contains (case-insensitive over title+body)
    fax = store.query(contains="FAX")
    assert {i.id for i in fax} == {"t3_a"}
    charting = store.query(contains="double charting")
    assert {i.id for i in charting} == {"t3_c"}

    # since filter
    recent = store.query(since=250)
    assert {i.id for i in recent} == {"t3_c", "t1_x"}

    # sort by score
    by_score = store.query(sort="score")
    assert by_score[0].id == "t3_b"  # score 50

    # sort by new
    by_new = store.query(sort="new")
    assert by_new[0].id == "t1_x"  # created 400

    # sort by comments
    by_comments = store.query(sort="comments")
    assert by_comments[0].id == "t3_b"  # 20 comments

    # limit
    assert len(store.query(limit=2)) == 2


def test_query_contains_escapes_wildcards(store):
    store.upsert_items(
        [
            _post("t3_a", title="100% manual", body="literal percent"),
            _post("t3_b", title="anything else", body="zzz"),
        ]
    )
    # '%' must be treated as a literal, not a wildcard.
    res = store.query(contains="100%")
    assert {i.id for i in res} == {"t3_a"}


def test_query_profile_filter(store):
    store.upsert_items(
        [
            _post("t3_a", profile="hospital-tech"),
            _post("t3_b", profile="other-niche"),
        ]
    )
    res = store.query(profile="other-niche")
    assert {i.id for i in res} == {"t3_b"}


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def test_get_evidence_order_and_skip_unknown(store):
    store.upsert_items(
        [
            _post("t3_a", title="A title", body="A body"),
            _post("t3_b", title="B title", body="B body"),
        ]
    )
    ev = store.get_evidence(["t3_b", "t3_missing", "t3_a"])
    # unknown skipped, requested order preserved
    assert [e.id for e in ev] == ["t3_b", "t3_a"]
    e = ev[0]
    assert e.permalink.startswith("https://www.reddit.com/")
    assert e.subreddit == "nursing"
    assert e.author == "alice"
    assert "B title" in e.quote and "B body" in e.quote


def test_get_evidence_quote_trimmed(store):
    long_body = "word " * 200  # ~1000 chars
    store.upsert_items([_post("t3_long", title="", body=long_body)])
    ev = store.get_evidence(["t3_long"])
    assert len(ev) == 1
    quote = ev[0].quote
    assert quote.endswith("…")
    assert len(quote) <= 301  # ~300 chars + ellipsis


def test_get_evidence_empty(store):
    assert store.get_evidence([]) == []


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #
def test_record_sweep(store):
    result = SweepResult(
        run_id="run123",
        profile="hospital-tech",
        posts_collected=5,
        comments_collected=2,
        threads_deep_fetched=1,
        subreddits={"nursing": 4, "medicine": 3},
        top_patterns=[("i wish", 3), ("manual", 1)],
        started_at=1000,
        finished_at=2000,
    )
    store.record_sweep(result)

    row = store.conn.execute(
        "SELECT run_id, profile, started_at, finished_at, stats FROM sweeps "
        "WHERE run_id = ?",
        ("run123",),
    ).fetchone()
    assert row is not None
    assert row["profile"] == "hospital-tech"
    assert row["started_at"] == 1000
    assert row["finished_at"] == 2000
    import json

    stats = json.loads(row["stats"])
    assert stats["posts_collected"] == 5
    assert stats["subreddits"] == {"nursing": 4, "medicine": 3}


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def test_stats(store):
    store.upsert_items(
        [
            _post("t3_a", subreddit="nursing", created=100,
                  matches=[Match("i wish", 3.0), Match("manual", 1.0)]),
            _post("t3_b", subreddit="medicine", created=500,
                  matches=[Match("i wish", 3.0)]),
        ]
    )
    comment = Item(
        id="t1_x", kind="comment", subreddit="nursing", author="bob",
        created_utc=300, permalink="p", body="c", profile="hospital-tech",
        matches=[Match("manual", 1.0)],
    )
    store.upsert_items([comment])

    st = store.stats(profile="hospital-tech")
    assert st["total"] == 3
    assert st["posts"] == 2
    assert st["comments"] == 1
    assert st["subreddits"] == {"nursing": 2, "medicine": 1}
    # top patterns: 'i wish' appears twice, 'manual' twice — both lead
    pattern_counts = dict(st["top_patterns"])
    assert pattern_counts["i wish"] == 2
    assert pattern_counts["manual"] == 2
    assert st["date_range"] == (100, 500)


def test_stats_empty(store):
    st = store.stats()
    assert st["total"] == 0
    assert st["posts"] == 0
    assert st["comments"] == 0
    assert st["subreddits"] == {}
    assert st["top_patterns"] == []
    assert st["date_range"] == (None, None)


def test_stats_profile_isolation(store):
    store.upsert_items(
        [
            _post("t3_a", profile="hospital-tech", matches=[Match("p1", 1.0)]),
            _post("t3_b", profile="other", matches=[Match("p2", 1.0)]),
        ]
    )
    st = store.stats(profile="hospital-tech")
    assert st["total"] == 1
    # top_patterns must respect the profile join
    assert dict(st["top_patterns"]) == {"p1": 1}
