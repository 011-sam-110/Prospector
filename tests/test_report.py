"""Offline tests for the evidence-bound renderer (Agent E).

No network, no real LLM. We feed a duck-typed ``FakeStore`` (only ``query`` and
``get_evidence`` are exercised by the renderer) and assert the trust contract:

  * under-evidenced clusters are DROPPED;
  * well-evidenced clusters render with their verbatim quotes + permalinks;
  * the standing "hypotheses to validate" disclaimer is always present;
  * ``--analyze`` degrades to stats-only when no LLM endpoint is configured;
  * with a stubbed LLM the thesis is rendered, still citing only the evidence.
"""

from __future__ import annotations

import pytest

from prospector import analyze
from prospector.models import (
    EvidenceItem,
    EvidenceThresholds,
    Item,
    Match,
    Profile,
)
from prospector.report import (
    Cluster,
    build_clusters,
    evidence_ok,
    render_report,
)

# Exact pattern strings (the dominant match drives clustering).
P_FAX = "still (fax|faxing|paper|on paper|by hand|hand-?writ)"
P_PAGER = "(pager|fax machine)"


# --------------------------------------------------------------------------- #
# Fixtures / fakes                                                            #
# --------------------------------------------------------------------------- #
class FakeStore:
    """Minimal duck-typed stand-in: just ``query`` + ``get_evidence``."""

    def __init__(self, items: list[Item]):
        self._items = list(items)

    def query(
        self,
        profile=None,
        subreddit=None,
        kind=None,
        min_pain: float = 0.0,
        contains=None,
        since=None,
        sort: str = "pain",
        limit: int = 100,
    ) -> list[Item]:
        rows = [it for it in self._items if it.pain_score >= min_pain]
        rows.sort(key=lambda it: it.pain_score, reverse=True)
        return rows[:limit]

    def get_evidence(self, ids: list[str]) -> list[EvidenceItem]:
        by_id = {it.id: it for it in self._items}
        out: list[EvidenceItem] = []
        for i in ids:
            it = by_id.get(i)
            if it is None:
                continue  # mirror the real store: skip unknown ids
            out.append(
                EvidenceItem(
                    id=it.id,
                    permalink=it.permalink,
                    quote=(it.text or it.body)[:300],
                    subreddit=it.subreddit,
                    author=it.author,
                    score=it.score,
                    created_utc=it.created_utc,
                )
            )
        return out


def _item(
    id_: str,
    subreddit: str,
    author: str,
    pattern: str,
    weight: float,
    body: str,
) -> Item:
    return Item(
        id=id_,
        kind="comment",
        subreddit=subreddit,
        author=author,
        created_utc=1_700_000_000,
        permalink=f"https://www.reddit.com/r/{subreddit}/comments/{id_[3:]}",
        title=None,
        body=body,
        score=42,
        num_comments=0,
        pain_score=weight,
        matches=[Match(pattern=pattern, weight=weight)],
        profile="hospital-tech",
    )


def _profile() -> Profile:
    return Profile(
        name="hospital-tech",
        description="Find tech missing from hospitals.",
        subreddits=["nursing", "medicine", "ems"],
        pain_threshold=3.0,
        evidence=EvidenceThresholds(min_items=5, min_subreddits=3, min_authors=3),
    )


def _store() -> FakeStore:
    items: list[Item] = []
    # Well-evidenced "still fax" cluster: 6 items, 6 subs, 6 authors → PASSES.
    fax_meta = [
        ("nursing", "u_alice", "we still fax discharge summaries to the SNF, it's 2026"),
        ("medicine", "u_bob", "still faxing referrals by hand every single shift"),
        ("ems", "u_carol", "why do we still fax run sheets to the receiving hospital"),
        ("BMET", "u_dave", "the EHR still prints to a fax queue, it's archaic"),
        ("hospitalist", "u_erin", "still on paper for med rec, fax everything"),
        ("healthIT", "u_frank", "we hand-write then fax, double work every time"),
    ]
    for n, (sub, auth, body) in enumerate(fax_meta, start=1):
        items.append(_item(f"t3_f{n}", sub, auth, P_FAX, 3.0, body))

    # Under-evidenced "pager" cluster: 2 items, 1 sub, 1 author → DROPPED.
    items.append(
        _item("t3_p1", "nursing", "u_alice", P_PAGER, 3.0, "the pager system is broken again")
    )
    items.append(
        _item("t3_p2", "nursing", "u_alice", P_PAGER, 3.0, "another pager outage on the floor")
    )
    return FakeStore(items)


# --------------------------------------------------------------------------- #
# evidence_ok contract                                                        #
# --------------------------------------------------------------------------- #
def test_evidence_ok_accepts_broad_cluster():
    t = EvidenceThresholds(min_items=5, min_subreddits=3, min_authors=3)
    c = Cluster(
        label="x",
        item_ids=["a", "b", "c", "d", "e"],
        subreddits={"s1", "s2", "s3"},
        authors={"u1", "u2", "u3"},
    )
    assert evidence_ok(c, t) is True


def test_evidence_ok_rejects_narrow_cluster():
    t = EvidenceThresholds(min_items=5, min_subreddits=3, min_authors=3)
    narrow = Cluster(
        label="x",
        item_ids=["a", "b"],
        subreddits={"s1"},
        authors={"u1"},
    )
    assert evidence_ok(narrow, t) is False


def test_evidence_ok_accepts_dict_form():
    t = EvidenceThresholds(min_items=5, min_subreddits=3, min_authors=3)
    d = {
        "item_ids": ["a", "b", "c", "d", "e", "f"],
        "subreddits": {"s1", "s2", "s3", "s4"},
        "authors": {"u1", "u2", "u3"},
    }
    assert evidence_ok(d, t) is True
    d["authors"] = {"u1"}
    assert evidence_ok(d, t) is False


# --------------------------------------------------------------------------- #
# build_clusters — drops under-evidenced, keeps well-evidenced                 #
# --------------------------------------------------------------------------- #
def test_build_clusters_drops_under_evidenced():
    profile = _profile()
    clusters = build_clusters(_store(), profile)

    labels = [c.label for c in clusters]
    # The fax cluster survives; the pager cluster is dropped.
    assert any("fax" in lbl.lower() for lbl in labels)
    assert not any("pager" in lbl.lower() for lbl in labels)
    assert len(clusters) == 1

    fax = clusters[0]
    assert len(set(fax.item_ids)) == 6
    assert len(fax.subreddits) == 6
    assert len(fax.authors) == 6
    assert fax.aggregate_pain == pytest.approx(18.0)  # 6 items * 3.0
    assert fax.confidence == "high"  # well above 2x every threshold


# --------------------------------------------------------------------------- #
# render_report — quotes, permalinks, disclaimer                               #
# --------------------------------------------------------------------------- #
def test_render_report_stats_only():
    md = render_report(_profile(), _store())

    # Disclaimer is always present.
    assert "HYPOTHESES TO VALIDATE" in md

    # The surviving gap renders with verbatim quotes + every permalink.
    assert "discharge summaries to the SNF" in md
    for n in range(1, 7):
        assert f"/comments/f{n}" in md

    # The dropped pager cluster must not leak any evidence into the report.
    assert "/comments/p1" not in md
    assert "/comments/p2" not in md
    assert "pager outage" not in md

    # Breadth line + appendix present.
    assert "6 items" in md
    assert "3 subreddits" not in md  # we have 6, not the bare threshold
    assert "Appendix" in md

    # No LLM thesis without --analyze.
    assert "Why underserved:" not in md


def test_render_report_no_clusters_still_disclaims():
    empty = FakeStore([])
    md = render_report(_profile(), empty)
    assert "HYPOTHESES TO VALIDATE" in md
    assert "No evidence-backed gaps" in md


def test_render_report_footer_timestamp():
    md = render_report(_profile(), _store(), generated="2026-06-26 12:00 UTC")
    assert "2026-06-26 12:00 UTC" in md


# --------------------------------------------------------------------------- #
# analyze — offline fallback + stubbed synthesis                               #
# --------------------------------------------------------------------------- #
def _clear_llm_env(monkeypatch):
    for var in (
        "FREELLMAPI_BASE_URL",
        "OPENAI_BASE_URL",
        "FREELLMAPI_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_analyze_unavailable_returns_empty(monkeypatch):
    _clear_llm_env(monkeypatch)
    assert analyze.available() is False
    assert analyze.synthesize([Cluster(label="x")], _profile()) == {}


def test_render_with_analyze_falls_back_when_unavailable(monkeypatch):
    # Force the unavailable path even if the host has creds in its env.
    monkeypatch.setattr(analyze, "available", lambda: False)
    md = render_report(_profile(), _store(), analyze=True)
    # Stats-only fallback: gap + disclaimer present, no synthesized thesis.
    assert "HYPOTHESES TO VALIDATE" in md
    assert "discharge summaries to the SNF" in md
    assert "Why underserved:" not in md


def test_synthesize_uses_stubbed_chat(monkeypatch):
    monkeypatch.setenv("FREELLMAPI_BASE_URL", "https://freellmapi.co/v1")
    monkeypatch.setenv("FREELLMAPI_KEY", "test-key")

    calls = {}

    def fake_chat(base_url, api_key, model, system, user, **kwargs):
        calls["base_url"] = base_url
        calls["api_key"] = api_key
        # Prove the model only ever sees the evidence we handed it.
        assert "discharge summaries to the SNF" in user
        return "This recurring fax workflow looks like a hypothesis worth validating."

    monkeypatch.setattr(analyze, "_chat", fake_chat)

    clusters = build_clusters(_store(), _profile())
    theses = analyze.synthesize(clusters, _profile())

    assert calls["base_url"] == "https://freellmapi.co/v1"
    assert calls["api_key"] == "test-key"
    assert set(theses.keys()) == {c.label for c in clusters}
    assert all("hypothesis worth validating" in t for t in theses.values())


def test_render_with_analyze_includes_thesis(monkeypatch):
    monkeypatch.setenv("FREELLMAPI_BASE_URL", "https://freellmapi.co/v1")
    monkeypatch.setenv("FREELLMAPI_KEY", "test-key")
    monkeypatch.setattr(
        analyze,
        "_chat",
        lambda *a, **k: "Synthesized thesis grounded only in the quotes above.",
    )

    md = render_report(_profile(), _store(), analyze=True)
    assert "Why underserved:" in md
    assert "Synthesized thesis grounded only in the quotes above." in md
    # Even with analyze, the disclaimer and real permalinks remain.
    assert "HYPOTHESES TO VALIDATE" in md
    assert "/comments/f1" in md
