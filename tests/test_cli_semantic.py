"""CLI tests for ``embed``, ``semantic-search``, ``clusters`` and ``prune``.

The ``semantic-search --json`` output is a contract another tool codes against:
a JSON array of objects with exactly the keys permalink, subreddit, title,
quote, score, created_utc, and ``[]`` with exit code 0 when the store is empty.
"""

from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from prospector import cli
from prospector.cli import app
from prospector.store import Store
from semantic_fakes import FakeEmbedder, make_item

runner = CliRunner()
KEYS = ["permalink", "subreddit", "title", "quote", "score", "created_utc"]


def _json_out(result) -> list:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_semantic_search_json_on_a_missing_store_prints_empty_list(tmp_path):
    db = tmp_path / "missing.db"
    result = runner.invoke(app, ["semantic-search", "anything", "--db", str(db), "--limit", "3", "--json"])
    assert _json_out(result) == []
    assert not db.exists()  # a search never creates a store


def test_semantic_search_json_on_an_empty_store_prints_empty_list(tmp_path):
    db = tmp_path / "empty.db"
    Store(db).close()
    result = runner.invoke(app, ["semantic-search", "anything", "--db", str(db), "--limit", "3", "--json"])
    assert _json_out(result) == []


def _filled_store(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("numpy")
    from prospector import semantic

    fake = FakeEmbedder()
    monkeypatch.setattr(semantic, "default_embedder", lambda: fake)
    db = tmp_path / "s.db"
    store = Store(db)
    store.upsert_items(
        [
            make_item("t3_a", "we still fax discharge forms", title="Fax forms", age_days=1),
            make_item("t3_b", "the pager beeps all night", title="Pager", age_days=2),
            make_item("t1_c", "fax machine jammed again", link_id="t3_a", age_days=1),
        ]
    )
    store.close()
    return db, fake


def test_embed_then_semantic_search_json_contract(tmp_path, monkeypatch):
    db, _fake = _filled_store(tmp_path, monkeypatch)
    embedded = runner.invoke(app, ["embed", "--db", str(db)])
    assert embedded.exit_code == 0, embedded.output
    assert "embedded=3" in embedded.stdout

    result = runner.invoke(app, ["semantic-search", "fax forms", "--db", str(db), "--limit", "2", "--json"])
    hits = _json_out(result)
    assert len(hits) == 2
    for hit in hits:
        assert list(hit) == KEYS
        assert hit["permalink"].startswith("https://www.reddit.com/")
        assert isinstance(hit["score"], float)
        assert isinstance(hit["created_utc"], int)
    assert hits[0]["title"] in {"Fax forms"}


def test_semantic_search_human_output_prints_the_count(tmp_path, monkeypatch):
    db, _fake = _filled_store(tmp_path, monkeypatch)
    runner.invoke(app, ["embed", "--db", str(db)])
    result = runner.invoke(app, ["semantic-search", "pager", "--db", str(db), "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert "searched=3" in result.stdout
    assert "https://www.reddit.com/" in result.stdout


def test_clusters_json(tmp_path, monkeypatch):
    db, _fake = _filled_store(tmp_path, monkeypatch)
    runner.invoke(app, ["embed", "--db", str(db)])
    result = runner.invoke(app, ["clusters", "--db", str(db), "--k", "2", "--json"])
    groups = _json_out(result)
    assert sum(g["size"] for g in groups) == 3
    assert all(g["examples"] for g in groups)


def test_prune_command_prints_counts(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    now = int(time.time())
    items = [
        make_item("t3_new", "recent", title="New"),
        make_item("t3_old", "old", title="Old"),
        make_item("t1_del", "[deleted]", author="[deleted]", link_id="t3_new"),
    ]
    items[0].created_utc = now - 3600  # one hour old
    items[1].created_utc = now - 8 * 86400  # eight days old
    items[2].created_utc = now - 3600
    store = Store(db)
    store.upsert_items(items)
    store.close()
    result = runner.invoke(app, ["prune", "--db", str(db), "--no-recheck", "--cache-dir", str(tmp_path / "c")])
    assert result.exit_code == 0, result.output
    assert "pruned=2" in result.stdout
    assert "expired=1" in result.stdout
    assert "gone_in_store=1" in result.stdout
    assert "remaining=1" in result.stdout


def test_sweep_rejects_an_unknown_transport():
    result = runner.invoke(app, ["sweep", "demo", "--transport", "ftp"])
    assert result.exit_code == 2


def test_sweep_passes_combine_terms_and_prints_fetched(monkeypatch, tmp_path):
    from prospector.models import Profile, SweepResult

    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: Profile(name="demo", description="", subreddits=["a"]))

    class _Client:
        def __init__(self, *a, **k):
            self.kwargs = k
            self.transport_in_use = "rss"

        def close(self):
            pass

    captured = {}

    def fake_sweep(profile, client, store, **kwargs):
        captured.update(kwargs)
        captured["client"] = client
        return SweepResult(run_id="r", profile="demo", posts_collected=4, comments_collected=6)

    monkeypatch.setattr(cli, "RedditClient", _Client)
    monkeypatch.setattr(cli.scrape, "sweep", fake_sweep)
    result = runner.invoke(
        app,
        [
            "sweep", "demo", "--db", str(tmp_path / "x.db"), "--combine-terms",
            "--transport", "rss", "--rss-comment-threads", "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["combine_terms"] is True
    assert captured["rss_comment_threads"] == 3
    assert captured["client"].kwargs["transport"] == "rss"
    assert "fetched=10 posts=4 comments=6 transport=rss" in result.stdout
