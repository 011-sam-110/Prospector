"""Offline tests for :mod:`prospector.semantic`.

They use the real ``sqlite-vec`` extension and a fake bag-of-words embedder
(no model download). The module is skipped when ``sqlite-vec`` or ``numpy`` is
not installed (the ``[semantic]`` extra is optional).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("numpy")

from prospector import semantic  # noqa: E402
from prospector.store import Store  # noqa: E402
from semantic_fakes import NOW, FakeEmbedder, make_item  # noqa: E402

FAX = [
    ("t3_f1", "Still faxing discharge forms", "the fax machine jams and we fax forms all day"),
    ("t3_f2", "Fax to pharmacy", "we fax every order to pharmacy, the fax line is always busy"),
    ("t3_f3", "Fax again", "another fax day, faxing referrals by hand to the clinic fax"),
]
PAGER = [
    ("t3_p1", "Pagers in 2026", "our pager beeps at night, pager batteries die, pager system is old"),
    ("t3_p2", "Lost my pager", "lost the pager again, the pager network drops pages"),
    ("t3_p3", "Pager fatigue", "pager alarms all shift, pager noise and pager overload"),
]


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "s.db")
    items = [make_item(i, body, title=title, sub="examplenursing") for i, title, body in FAX]
    items += [make_item(i, body, title=title, sub="examplemedicine") for i, title, body in PAGER]
    items.append(
        make_item("t1_c1", "the fax machine ate my forms again", link_id="t3_f1", sub="examplenursing")
    )
    items.append(make_item("t3_gone", "[removed]", title="Removed post"))
    items.append(make_item("t3_empty", "", title=None))
    store.upsert_items(items)
    return store


def test_empty_store_has_no_vectors_and_search_returns_nothing(tmp_path):
    store = Store(tmp_path / "e.db")
    embedder = FakeEmbedder()
    assert semantic.has_vectors(store.conn) is False
    assert semantic.search(store.conn, embedder, "fax") == []
    assert semantic.clusters(store.conn) == []
    assert embedder.query_calls == 0  # no model call on an empty store
    store.close()


def test_embed_pending_embeds_new_items_once(tmp_path):
    store = _store(tmp_path)
    embedder = FakeEmbedder()
    first = semantic.embed_pending(store.conn, embedder, now=NOW)
    assert first.embedded == 7  # 6 posts + 1 comment
    assert first.skipped_gone == 1
    assert first.skipped_empty == 1
    assert first.vectors == 7

    again = semantic.embed_pending(store.conn, embedder, now=NOW)
    assert again.embedded == 0
    assert again.unchanged == 7

    # A changed body is embedded again.
    store.conn.execute("UPDATE items SET body = 'edited text about fax' WHERE id = 't3_f2'")
    store.conn.commit()
    third = semantic.embed_pending(store.conn, embedder, now=NOW)
    assert third.embedded == 1
    assert third.vectors == 7
    store.close()


def test_search_ranks_by_meaning_and_returns_evidence(tmp_path):
    store = _store(tmp_path)
    embedder = FakeEmbedder()
    semantic.embed_pending(store.conn, embedder, now=NOW)

    hits = semantic.search(store.conn, embedder, "fax machine forms", limit=4)
    assert len(hits) == 4
    assert all(h["id"] in {"t3_f1", "t3_f2", "t3_f3", "t1_c1"} for h in hits)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 < s <= 1.0 for s in scores)

    top = hits[0]
    assert set(semantic.CONTRACT_KEYS) <= set(top)
    assert top["permalink"].startswith("https://www.reddit.com/r/examplenursing/comments/")
    assert top["quote"]

    comment = next(h for h in hits if h["id"] == "t1_c1")
    # A comment has no title of its own: it shows its thread title.
    assert comment["title"] == "Still faxing discharge forms"
    assert comment["kind"] == "comment"
    store.close()


def test_search_filters_by_subreddit_and_age(tmp_path):
    store = _store(tmp_path)
    embedder = FakeEmbedder()
    semantic.embed_pending(store.conn, embedder, now=NOW)

    only_medicine = semantic.search(store.conn, embedder, "fax", limit=10, subreddit="ExampleMedicine")
    assert {h["subreddit"] for h in only_medicine} == {"examplemedicine"}
    assert len(only_medicine) == 3

    none_recent = semantic.search(store.conn, embedder, "fax", limit=10, since=NOW + 1)
    assert none_recent == []
    store.close()


def test_contract_view_has_exactly_the_six_keys(tmp_path):
    store = _store(tmp_path)
    embedder = FakeEmbedder()
    semantic.embed_pending(store.conn, embedder, now=NOW)
    view = semantic.contract_view(semantic.search(store.conn, embedder, "pager", limit=3))
    assert len(view) == 3
    for hit in view:
        assert list(hit) == ["permalink", "subreddit", "title", "quote", "score", "created_utc"]
        assert isinstance(hit["score"], float)
        assert isinstance(hit["created_utc"], int)
        assert isinstance(hit["title"], str)
    store.close()


def test_clusters_split_two_topics_with_quotes(tmp_path):
    store = _store(tmp_path)
    semantic.embed_pending(store.conn, FakeEmbedder(), now=NOW)

    groups = semantic.clusters(store.conn, k=2, examples=2)
    assert [g["cluster"] for g in groups] == [1, 2]
    assert sorted(g["size"] for g in groups) == [3, 4]
    by_size = {g["size"]: g for g in groups}
    fax, pager = by_size[4], by_size[3]
    assert "fax" in fax["keywords"]
    assert "pager" in pager["keywords"]
    assert pager["subreddits"] == {"examplemedicine": 3}
    for group in groups:
        assert len(group["examples"]) == 2
        for example in group["examples"]:
            assert example["permalink"].startswith("https://www.reddit.com/")
            assert example["quote"]
    store.close()


def test_clusters_filter_by_subreddit(tmp_path):
    store = _store(tmp_path)
    semantic.embed_pending(store.conn, FakeEmbedder(), now=NOW)
    groups = semantic.clusters(store.conn, subreddit="examplemedicine", k=1)
    assert len(groups) == 1 and groups[0]["size"] == 3
    store.close()


def test_lazy_embedder_does_not_build_the_model_until_used():
    built = []

    def factory():
        built.append(1)
        return FakeEmbedder()

    lazy = semantic.LazyEmbedder(factory, name="fake-bow-384", dim=384)
    assert lazy.loaded is False and built == []
    lazy.embed_query("x")
    assert lazy.loaded is True and built == [1]
