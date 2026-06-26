"""Offline tests for the typer CLI.

Everything here is hermetic: no Reddit, no SQLite-on-disk we care about, no LLM.
We monkeypatch the engine seams the CLI imports (``list_profiles``,
``load_profile``, ``RedditClient``, ``Store``, ``scrape.sweep``,
``render_report``) and drive commands through :class:`typer.testing.CliRunner`,
asserting both the user-facing output and that the CLI handed the right objects
to the engine.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from prospector import cli
from prospector.cli import app
from prospector.models import Item, Match, Profile, SweepResult

runner = CliRunner()


# --------------------------------------------------------------------------- #
# Fixtures / fakes                                                            #
# --------------------------------------------------------------------------- #
def _profile() -> Profile:
    return Profile(
        name="demo",
        description="a demo profile",
        subreddits=["nursing", "medicine"],
        search_terms=["i wish there was"],
    )


def _item(item_id: str, sub: str, pain: float, title: str) -> Item:
    return Item(
        id=item_id,
        kind="post",
        subreddit=sub,
        author="someone",
        created_utc=1_700_000_000,
        permalink=f"https://www.reddit.com/r/{sub}/comments/{item_id}/x/",
        title=title,
        body="They still fax everything; I wish there was a tool.",
        score=42,
        num_comments=7,
        pain_score=pain,
        matches=[Match(pattern="still fax", weight=3.0)],
        profile="demo",
    )


class _FakeStore:
    """A stand-in Store that records construction + close, and serves canned rows."""

    instances: list["_FakeStore"] = []

    def __init__(self, db_path="prospector.db", *args, **kwargs):
        self.db_path = db_path
        self.closed = False
        self.query_kwargs = None
        self.rows: list[Item] = []
        _FakeStore.instances.append(self)

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        return list(self.rows)

    def close(self):
        self.closed = True


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _FakeClient.instances.append(self)

    @property
    def authenticated(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# profiles                                                                    #
# --------------------------------------------------------------------------- #
def test_profiles_lists_names(monkeypatch):
    monkeypatch.setattr(cli, "list_profiles", lambda *a, **k: ["alpha", "hospital-tech"])
    result = runner.invoke(app, ["profiles"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "hospital-tech" in result.output


def test_profiles_empty(monkeypatch):
    monkeypatch.setattr(cli, "list_profiles", lambda *a, **k: [])
    result = runner.invoke(app, ["profiles"])
    assert result.exit_code == 0, result.output
    assert "No profiles found." in result.output


# --------------------------------------------------------------------------- #
# sweep                                                                       #
# --------------------------------------------------------------------------- #
def test_sweep_invokes_engine_offline(monkeypatch, tmp_path):
    _FakeStore.instances.clear()
    _FakeClient.instances.clear()
    prof = _profile()
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: prof)
    monkeypatch.setattr(cli, "RedditClient", _FakeClient)
    monkeypatch.setattr(cli, "Store", _FakeStore)
    # No env creds -> "anonymous" mode, and no live network is touched.
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

    captured = {}

    def fake_sweep(profile, client, store, **kwargs):
        captured["profile"] = profile
        captured["client"] = client
        captured["store"] = store
        captured["kwargs"] = kwargs
        return SweepResult(
            run_id="run123",
            profile=profile.name,
            posts_collected=7,
            comments_collected=12,
            threads_deep_fetched=3,
            subreddits={"nursing": 5, "medicine": 2},
            top_patterns=[("still fax", 4)],
            started_at=100,
            finished_at=130,
        )

    monkeypatch.setattr(cli.scrape, "sweep", fake_sweep)

    db = tmp_path / "x.db"
    result = runner.invoke(
        app,
        [
            "sweep",
            "demo",
            "--db",
            str(db),
            "--time",
            "week",
            "--limit",
            "10",
            "--max-threads",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    # SweepResult summary made it to stdout.
    assert "run123" in result.output
    assert "posts collected:    7" in result.output
    assert "comments collected: 12" in result.output
    assert "still fax" in result.output
    assert "anonymous" in result.output

    # CLI passed the loaded profile + the objects it built to scrape.sweep.
    assert captured["profile"] is prof
    assert captured["client"] is _FakeClient.instances[-1]
    assert captured["store"] is _FakeStore.instances[-1]
    assert captured["kwargs"]["time_window"] == "week"
    assert captured["kwargs"]["listing_limit"] == 10
    assert captured["kwargs"]["max_threads"] == 5
    # The store path came from --db and the store was closed.
    assert _FakeStore.instances[-1].db_path == str(db)
    assert _FakeStore.instances[-1].closed is True


def test_sweep_oauth_without_creds_errors(monkeypatch):
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: _profile())
    monkeypatch.setattr(cli, "RedditClient", _FakeClient)
    monkeypatch.setattr(cli, "Store", _FakeStore)
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

    called = {"sweep": False}

    def fake_sweep(*a, **k):
        called["sweep"] = True
        raise AssertionError("scrape.sweep must not run when --oauth fails")

    monkeypatch.setattr(cli.scrape, "sweep", fake_sweep)

    result = runner.invoke(app, ["sweep", "demo", "--oauth"])
    assert result.exit_code == 2
    assert "REDDIT_CLIENT_ID" in result.output
    assert called["sweep"] is False


def test_sweep_missing_profile_is_clean_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("Profile 'nope' not found. Available: demo")

    monkeypatch.setattr(cli, "load_profile", boom)
    result = runner.invoke(app, ["sweep", "nope"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --------------------------------------------------------------------------- #
# query                                                                       #
# --------------------------------------------------------------------------- #
def test_query_prints_table(monkeypatch):
    _FakeStore.instances.clear()
    store = _FakeStore()
    store.rows = [
        _item("t3_aaa", "nursing", 7.0, "Still faxing in 2026"),
        _item("t3_bbb", "medicine", 3.0, "Why no app for handoffs"),
    ]
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)

    result = runner.invoke(
        app, ["query", "demo", "--min-pain", "2", "--sort", "pain", "--limit", "5"]
    )
    assert result.exit_code == 0, result.output
    assert "t3_aaa" in result.output
    assert "nursing" in result.output
    assert "Still faxing in 2026" in result.output
    # Filters were forwarded to the store.
    assert store.query_kwargs["profile"] == "demo"
    assert store.query_kwargs["min_pain"] == 2.0
    assert store.query_kwargs["sort"] == "pain"
    assert store.query_kwargs["limit"] == 5
    assert store.closed is True


def test_query_empty(monkeypatch):
    store = _FakeStore()
    store.rows = []
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)
    result = runner.invoke(app, ["query", "demo"])
    assert result.exit_code == 0, result.output
    assert "No items match." in result.output


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #
def test_report_echoes_markdown(monkeypatch):
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: _profile())
    store = _FakeStore()
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)
    seen = {}

    def fake_render(profile, st, *, analyze=False, **kwargs):
        seen["profile"] = profile
        seen["store"] = st
        seen["analyze"] = analyze
        return "# Report\n\nHYPOTHESES TO VALIDATE"

    monkeypatch.setattr(cli, "render_report", fake_render)

    result = runner.invoke(app, ["report", "demo", "--analyze"])
    assert result.exit_code == 0, result.output
    assert "# Report" in result.output
    assert seen["analyze"] is True
    assert seen["store"] is store
    assert store.closed is True


def test_report_writes_out(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_profile", lambda *a, **k: _profile())
    monkeypatch.setattr(cli, "Store", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(cli, "render_report", lambda *a, **k: "# Saved report")

    out = tmp_path / "report.md"
    result = runner.invoke(app, ["report", "demo", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "# Saved report"
    assert "Wrote report to" in result.output


# --------------------------------------------------------------------------- #
# export                                                                      #
# --------------------------------------------------------------------------- #
def test_export_json(monkeypatch):
    store = _FakeStore()
    store.rows = [_item("t3_aaa", "nursing", 7.0, "Still faxing")]
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)

    result = runner.invoke(app, ["export", "demo", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "t3_aaa"
    assert payload[0]["matches"][0]["pattern"] == "still fax"
    assert store.query_kwargs["profile"] == "demo"


def test_export_csv_to_file(monkeypatch, tmp_path):
    store = _FakeStore()
    store.rows = [_item("t3_aaa", "nursing", 7.0, "Still faxing")]
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)

    out = tmp_path / "dump.csv"
    result = runner.invoke(app, ["export", "demo", "-f", "csv", "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert "id,kind,subreddit" in text.splitlines()[0]
    assert "t3_aaa" in text
    assert "Wrote 1 item(s) to" in result.output


def test_export_md(monkeypatch):
    store = _FakeStore()
    store.rows = [_item("t3_aaa", "nursing", 7.0, "Still faxing")]
    monkeypatch.setattr(cli, "Store", lambda *a, **k: store)
    result = runner.invoke(app, ["export", "demo", "--format", "md"])
    assert result.exit_code == 0, result.output
    assert "# Export: demo" in result.output
    assert "r/nursing" in result.output


def test_export_bad_format_errors(monkeypatch):
    monkeypatch.setattr(cli, "Store", lambda *a, **k: _FakeStore())
    result = runner.invoke(app, ["export", "demo", "--format", "xml"])
    assert result.exit_code == 2
    assert "Unknown format" in result.output
