"""Offline tests for :mod:`prospector.prune`.

The core check: one prune run removes every item posted more than 7 days ago
and every item that was deleted or removed on Reddit, together with its lexicon
matches and its vector, and keeps everything else.
"""

from __future__ import annotations

import os
import time

import pytest

from prospector.models import Match
from prospector.prune import PruneResult, is_gone, is_gone_data, prune, purge_cache
from prospector.store import Store
from semantic_fakes import DAY, NOW, FakeEmbedder, make_item


def _ids(store: Store) -> set[str]:
    return {r[0] for r in store.conn.execute("SELECT id FROM items").fetchall()}


def _seed(store: Store) -> None:
    first = make_item("t3_new1", "fresh post about fax machines", title="Fresh", age_days=1)
    first.matches = [Match(pattern="fax", weight=1.0)]
    old = make_item("t3_old1", "old post about pagers", title="Old", age_days=7.5)
    old.matches = [Match(pattern="pager", weight=1.0)]
    store.upsert_items(
        [
            first,
            make_item("t3_edge", "posted just inside the window", title="Edge", age_days=6.9),
            old,
            make_item("t1_old2", "a month old comment", age_days=30, link_id="t3_old1"),
            make_item("t1_del1", "[deleted]", age_days=1, author="[deleted]", link_id="t3_new1"),
            make_item("t1_rem1", "[removed]", age_days=2, link_id="t3_new1"),
            make_item("t3_rem2", "[ Removed by Reddit ]", title="Was here", age_days=2),
            make_item("t1_ok1", "a normal comment that stays", age_days=3, link_id="t3_new1"),
        ]
    )


def test_prune_removes_items_older_than_7_days_and_deleted_items(tmp_path):
    store = Store(tmp_path / "p.db")
    _seed(store)
    result = prune(store.conn, max_age_days=7, now=NOW, log=lambda _m: None)

    assert _ids(store) == {"t3_new1", "t3_edge", "t1_ok1"}
    assert result.expired == 2  # t3_old1 (7.5 days), t1_old2 (30 days)
    assert result.gone_in_store == 3  # [deleted] author, [removed], [ Removed by Reddit ]
    assert result.deleted == 5
    assert result.remaining == 3
    # The lexicon matches of a deleted item go too.
    patterns = {r[0] for r in store.conn.execute("SELECT pattern FROM matches").fetchall()}
    assert patterns == {"fax"}
    store.close()


def test_prune_deletes_the_vectors_of_pruned_items(tmp_path):
    semantic = pytest.importorskip("prospector.semantic")
    pytest.importorskip("sqlite_vec")
    store = Store(tmp_path / "p.db")
    _seed(store)
    # Embed while the deleted items are still "live" so every item has a vector.
    store.conn.execute("UPDATE items SET body = 'live text', author = 'x' WHERE id IN ('t1_del1', 't1_rem1')")
    store.conn.execute("UPDATE items SET body = 'live text' WHERE id = 't3_rem2'")
    store.conn.commit()
    semantic.embed_pending(store.conn, FakeEmbedder(), now=NOW)
    assert semantic.vector_count(store.conn) == 8
    # Reddit now shows them as deleted / removed.
    store.conn.execute("UPDATE items SET body = '[deleted]', author = '[deleted]' WHERE id = 't1_del1'")
    store.conn.execute("UPDATE items SET body = '[removed]' WHERE id IN ('t1_rem1', 't3_rem2')")
    store.conn.commit()

    result = prune(store.conn, max_age_days=7, now=NOW, log=lambda _m: None)

    assert result.deleted == 5
    assert result.vectors_deleted == 5
    vec_ids = {r[0] for r in store.conn.execute("SELECT item_id FROM item_vectors").fetchall()}
    assert vec_ids == {"t3_new1", "t3_edge", "t1_ok1"}
    assert semantic.vector_count(store.conn) == 3
    store.close()


class _InfoClient:
    """Fake client: ``info`` answers from a dict and records each batch."""

    def __init__(self, live: dict[str, dict], empty: bool = False, fail: bool = False):
        self.live = live
        self.empty = empty
        self.fail = fail
        self.batches: list[list[str]] = []

    def info(self, names):
        self.batches.append(list(names))
        if self.fail:
            raise RuntimeError("HTTP 429")
        if self.empty:
            return []
        return [self.live[n] for n in names if n in self.live]


def test_recheck_removes_items_deleted_or_missing_on_reddit(tmp_path):
    store = Store(tmp_path / "p.db")
    store.upsert_items(
        [
            make_item("t3_live", "still up", title="Live", age_days=2, fetched_age_days=1),
            make_item("t3_gone", "was up", title="Gone", age_days=2, fetched_age_days=1),
            make_item("t1_dead", "was a comment", age_days=2, fetched_age_days=1, link_id="t3_live"),
            make_item("t1_fresh", "fetched an hour ago", age_days=2, fetched_age_days=0.04, link_id="t3_live"),
        ]
    )
    client = _InfoClient(
        {
            "t3_live": {"name": "t3_live", "title": "Live", "selftext": "still up", "author": "a"},
            "t1_dead": {"name": "t1_dead", "body": "[deleted]", "author": "[deleted]"},
            # t3_gone is left out: Reddit no longer has it.
        }
    )
    result = prune(store.conn, now=NOW, client=client, skip_fresh_hours=12, log=lambda _m: None)

    assert _ids(store) == {"t3_live", "t1_fresh"}
    assert result.gone_on_reddit == 1
    assert result.missing_on_reddit == 1
    assert result.rechecked == 3
    assert result.recheck_requests == 1
    # The item fetched in the last 12 hours was not rechecked.
    assert "t1_fresh" not in client.batches[0]
    store.close()


def test_an_empty_recheck_answer_deletes_nothing(tmp_path):
    store = Store(tmp_path / "p.db")
    store.upsert_items([make_item("t3_a", "x", title="A", age_days=2, fetched_age_days=1)])
    result = prune(store.conn, now=NOW, client=_InfoClient({}, empty=True), log=lambda _m: None)
    assert _ids(store) == {"t3_a"}
    assert result.recheck_inconclusive == 1
    assert result.deleted == 0
    store.close()


def test_recheck_stops_after_three_failed_requests(tmp_path):
    store = Store(tmp_path / "p.db")
    store.upsert_items(
        [
            make_item(f"t3_x{i:03d}", "x", title="X", age_days=2, fetched_age_days=1)
            for i in range(450)
        ]
    )
    client = _InfoClient({}, fail=True)
    result = prune(store.conn, now=NOW, client=client, log=lambda _m: None)
    assert len(client.batches) == 3  # 5 batches needed, stopped after 3 failures
    assert result.recheck_inconclusive == 5
    assert result.deleted == 0
    store.close()


def test_recheck_sends_batches_of_100(tmp_path):
    store = Store(tmp_path / "p.db")
    store.upsert_items(
        [make_item(f"t3_y{i:03d}", "y", title="Y", age_days=2, fetched_age_days=1) for i in range(250)]
    )
    live = {f"t3_y{i:03d}": {"name": f"t3_y{i:03d}", "title": "Y", "selftext": "y", "author": "a"} for i in range(250)}
    client = _InfoClient(live)
    result = prune(store.conn, now=NOW, client=client, log=lambda _m: None)
    assert [len(b) for b in client.batches] == [100, 100, 50]
    assert result.deleted == 0 and result.rechecked == 250
    store.close()


def test_is_gone_rules():
    assert is_gone("t", "[deleted]", "a")
    assert is_gone("t", "[removed]", "a")
    assert is_gone("t", "  [ Removed by Reddit ]  ", "a")
    assert is_gone("[deleted by user]", "", "a")
    assert is_gone("t", "text", "[deleted]")
    assert not is_gone("Why was this [removed]?", "the mods removed my post", "a")
    assert not is_gone(None, "normal", "a")
    assert is_gone_data({"name": "t3_a", "selftext": "", "author": "a", "removed_by_category": "moderator"})
    assert not is_gone_data({"name": "t3_a", "title": "ok", "selftext": "", "author": "a"})


def test_purge_cache_deletes_old_files_only(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = cache / "old.json"
    new = cache / "new.json"
    other = cache / "keep.txt"
    for f in (old, new, other):
        f.write_text("{}")
    stale = time.time() - 30 * 3600
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    assert purge_cache(cache, max_age_hours=24) == 1
    assert not old.exists() and new.exists() and other.exists()
    assert purge_cache(tmp_path / "missing", max_age_hours=24) == 0


def test_prune_result_total():
    r = PruneResult(expired=2, gone_in_store=1, gone_on_reddit=3, missing_on_reddit=4)
    assert r.deleted == 10
    assert r.as_dict()["deleted"] == 10
