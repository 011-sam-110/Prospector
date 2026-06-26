"""Evidence-bound report renderer — the trust centerpiece.

A "gap" is never emitted on vibes. ``build_clusters`` groups high-pain items by
their dominant matched pattern, resolves verbatim evidence from the store, and
then *structurally drops* any cluster that does not clear the profile's evidence
thresholds (``evidence_ok``): enough distinct items, across enough distinct
subreddits, from enough distinct authors. The Markdown renderer only ever cites
the evidence objects the store actually returned — there is no path by which an
invented permalink or quote can reach the page.

With ``analyze=True`` an optional LLM (see :mod:`prospector.analyze`) adds a
one-paragraph thesis per surviving gap, constrained to the same quotes; if no
model is configured the report silently falls back to stats-only clusters.

Every report carries the standing disclaimer: these are **hypotheses to
validate**, not validated needs — no clinical or market claims.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prospector.models import EvidenceItem, EvidenceThresholds, Profile

if TYPE_CHECKING:  # pragma: no cover - typing only; store.py is a sibling module
    from prospector.store import Store


# How many high-pain items to pull from the store before clustering.
_QUERY_LIMIT = 1000

# Confidence ordering for sorting (high first).
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

DISCLAIMER = (
    "> **These are HYPOTHESES TO VALIDATE, not validated needs.** Reddit is not "
    "ground truth and venting is not a market. Evidence breadth and confidence "
    "below are derived from what was actually fetched — they are a prompt for "
    "real validation (interviews, field study, regulatory/clinical review), not "
    "a conclusion. Nothing here is a clinical, medical, or market claim."
)


# --------------------------------------------------------------------------- #
# Cluster model                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Cluster:
    """A candidate gap: a group of evidence-backed items sharing a theme."""

    label: str  # human-readable dominant matched theme
    item_ids: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    subreddits: set[str] = field(default_factory=set)
    authors: set[str] = field(default_factory=set)
    aggregate_pain: float = 0.0
    confidence: str = "low"  # 'low' | 'medium' | 'high'


# --------------------------------------------------------------------------- #
# Evidence contract                                                           #
# --------------------------------------------------------------------------- #
def _counts(cluster: "Cluster | dict") -> tuple[int, int, int]:
    """Return ``(distinct_items, distinct_subreddits, distinct_authors)``."""
    if isinstance(cluster, dict):
        item_ids = cluster.get("item_ids") or cluster.get("items") or []
        subreddits = cluster.get("subreddits") or set()
        authors = cluster.get("authors") or set()
    else:
        item_ids = cluster.item_ids
        subreddits = cluster.subreddits
        authors = cluster.authors
    return len(set(item_ids)), len(set(subreddits)), len(set(authors))


def evidence_ok(cluster: "Cluster | dict", thresholds: EvidenceThresholds) -> bool:
    """True iff the cluster clears *all three* breadth thresholds.

    The contract (PRD §8): a gap may not be emitted unless it cites at least
    ``min_items`` distinct items, across at least ``min_subreddits`` subreddits,
    from at least ``min_authors`` distinct authors. The renderer **must** drop
    anything that fails this — it is structural, not advisory.
    """
    n_items, n_subs, n_authors = _counts(cluster)
    return (
        n_items >= thresholds.min_items
        and n_subs >= thresholds.min_subreddits
        and n_authors >= thresholds.min_authors
    )


def _confidence(
    n_items: int, n_subs: int, n_authors: int, t: EvidenceThresholds
) -> str:
    """Derive a confidence band purely from evidence breadth (never vibes).

    Diversity-weighted: subreddit and author spread are the hard-to-fake
    anti-fluke signals (a gap echoed across many subreddits by many distinct
    people is far more credible than the same raw item count from one chatty
    thread), so they drive the band; raw item count only needs modest headroom.

    Pre-condition: the cluster already cleared ``evidence_ok`` (so the floor is
    ``low``). ``high`` = at least double the subreddit *and* author floors with
    at least one item above the item floor; ``medium`` = comfortably above both
    diversity floors; otherwise ``low``.
    """
    mi, ms, ma = t.min_items, t.min_subreddits, t.min_authors
    if n_subs >= 2 * ms and n_authors >= 2 * ma and n_items >= mi + 1:
        return "high"
    if n_subs >= ms + 1 and n_authors >= ma + 1:
        return "medium"
    return "low"


# --------------------------------------------------------------------------- #
# Clustering                                                                  #
# --------------------------------------------------------------------------- #
def _dominant_pattern(item) -> str | None:
    """The pattern of the item's highest-weight match (stable tie-break)."""
    matches = getattr(item, "matches", None) or []
    if not matches:
        return None
    # max() returns the first element among equal maxima → stable on insertion.
    best = max(matches, key=lambda m: m.weight)
    return best.pattern


def _humanize_pattern(pattern: str) -> str:
    """Turn a raw regex into a short readable theme label.

    Collapses each alternation group to its first option, strips regex
    metacharacters, and tidies whitespace. Falls back to the raw pattern if the
    cleanup empties it out.
    """
    s = pattern
    # Replace innermost ``(a|b|c)`` groups with their first alternative, repeat
    # until stable to handle several groups.
    while True:
        new = re.sub(
            r"\(([^()]*)\)",
            lambda m: m.group(1).split("|")[0],
            s,
        )
        if new == s:
            break
        s = new
    s = re.sub(r"[?*+\\^$.{}\[\]]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return pattern
    return s[0].upper() + s[1:]


def build_clusters(store: "Store", profile: Profile, limit: int = 12) -> list[Cluster]:
    """Group high-pain items into evidence-backed candidate gaps.

    1. Pull items with ``pain_score >= profile.pain_threshold`` (sorted by pain).
    2. Bucket them by their dominant matched pattern (the highest-weight match).
    3. Resolve verbatim evidence for each bucket via ``store.get_evidence`` and
       compute subreddit/author breadth + aggregate pain.
    4. **Keep only clusters that pass** :func:`evidence_ok` — the rest are
       dropped, structurally.
    5. Sort by confidence then aggregate pain, and truncate to ``limit``.
    """
    items = store.query(
        profile=profile.name,
        min_pain=profile.pain_threshold,
        sort="pain",
        limit=_QUERY_LIMIT,
    )

    # Bucket by dominant pattern, de-duping items by id within each bucket.
    buckets: dict[str, dict[str, object]] = {}
    pain_by_id: dict[str, float] = {}
    for it in items:
        pattern = _dominant_pattern(it)
        if pattern is None:
            continue  # no explainable signal → cannot anchor a gap
        buckets.setdefault(pattern, {})[it.id] = it
        pain_by_id[it.id] = float(getattr(it, "pain_score", 0.0) or 0.0)

    clusters: list[Cluster] = []
    used_labels: set[str] = set()
    for pattern, by_id in buckets.items():
        bucket_ids = list(by_id.keys())
        evidence = store.get_evidence(bucket_ids)
        if not evidence:
            continue
        # Anchor counts to citable evidence only — you can claim only what you
        # can show.
        item_ids = [ev.id for ev in evidence]
        subreddits = {ev.subreddit for ev in evidence if ev.subreddit}
        authors = {ev.author for ev in evidence if ev.author}
        distinct_ids = set(item_ids)
        aggregate_pain = sum(pain_by_id.get(i, 0.0) for i in distinct_ids)

        n_items, n_subs, n_authors = len(distinct_ids), len(subreddits), len(authors)
        confidence = _confidence(n_items, n_subs, n_authors, profile.evidence)

        label = _humanize_pattern(pattern)
        # Keep labels unique so analyze()/lookups stay 1:1 with clusters.
        if label in used_labels:
            label = f"{label} [{pattern}]"
        used_labels.add(label)

        cluster = Cluster(
            label=label,
            item_ids=item_ids,
            evidence=list(evidence),
            subreddits=subreddits,
            authors=authors,
            aggregate_pain=aggregate_pain,
            confidence=confidence,
        )
        # THE CONTRACT: drop anything that does not clear the thresholds.
        if not evidence_ok(cluster, profile.evidence):
            continue
        clusters.append(cluster)

    clusters.sort(
        key=lambda c: (_CONFIDENCE_RANK.get(c.confidence, 0), c.aggregate_pain),
        reverse=True,
    )
    return clusters[:limit]


# --------------------------------------------------------------------------- #
# Markdown rendering                                                          #
# --------------------------------------------------------------------------- #
def _clean_quote(quote: str, max_len: int = 280) -> str:
    """Collapse whitespace and bound the length of a verbatim quote."""
    q = " ".join(str(quote or "").split())
    if len(q) > max_len:
        q = q[: max_len - 1].rstrip() + "…"
    return q


def _meter(confidence: str) -> str:
    filled = {"high": 5, "medium": 3, "low": 1}.get(confidence, 1)
    return "●" * filled + "○" * (5 - filled)


def _render_evidence_lines(evidence: list[EvidenceItem], n: int = 8) -> list[str]:
    """Up to ``n`` verbatim quotes, each anchored to its permalink.

    Small clusters show *all* their evidence (full traceability); the cap only
    bounds very large clusters so a single gap can't bloat the report.
    """
    lines: list[str] = []
    for ev in evidence[:n]:
        quote = _clean_quote(ev.quote)
        sub = ev.subreddit or "?"
        author = ev.author or "[deleted]"
        link = ev.permalink or ""
        lines.append(
            f'- "{quote}" — r/{sub} · u/{author} · ↑{ev.score} · '
            f"[source]({link})"
        )
    return lines


def render_report(
    profile: Profile,
    store: "Store",
    *,
    analyze: bool = False,
    model: str = "auto",
    generated: str | None = None,
) -> str:
    """Render the evidence-bound Markdown report for ``profile``.

    Default (``analyze=False``): stats-only clusters from :func:`build_clusters`,
    each with confidence, breadth (``N items / M subreddits / K authors``) and
    3-5 verbatim quotes with permalinks, plus an appendix of the raw ranked
    clusters.

    With ``analyze=True``: each surviving gap also gets a one-paragraph thesis
    from :func:`prospector.analyze.synthesize` — but every gap still passes
    ``evidence_ok`` and only the engine-provided evidence is cited. If no model
    is configured the thesis is simply omitted (graceful fallback).

    The standing disclaimer is always present.
    """
    clusters = build_clusters(store, profile)

    theses: dict[str, str] = {}
    if analyze and clusters:
        # Imported lazily and aliased so the local ``analyze`` flag does not
        # shadow the module; keeps the LLM path entirely optional.
        from prospector import analyze as _analyze

        try:
            theses = _analyze.synthesize(clusters, profile, model) or {}
        except Exception:
            theses = {}

    out: list[str] = []
    out.append(f"# prospector report — {profile.name}")
    out.append("")
    if profile.description:
        out.append(_clean_quote(profile.description, max_len=600))
        out.append("")
    out.append(DISCLAIMER)
    out.append("")

    if not clusters:
        t = profile.evidence
        out.append("## No evidence-backed gaps")
        out.append("")
        out.append(
            "No candidate gap cleared the evidence contract for this profile "
            f"(needs ≥ {t.min_items} distinct items across ≥ "
            f"{t.min_subreddits} subreddits from ≥ {t.min_authors} distinct "
            "authors). Collect more before drawing conclusions — under-evidenced "
            "clusters are dropped on purpose."
        )
        out.append("")
        out.append(_footer(generated))
        return "\n".join(out).rstrip() + "\n"

    out.append(f"## Candidate gaps ({len(clusters)})")
    out.append("")
    for i, c in enumerate(clusters, start=1):
        n_items, n_subs, n_authors = _counts(c)
        out.append(f"### Gap {i}: {c.label}")
        out.append(
            f"Confidence: {_meter(c.confidence)} ({c.confidence})   "
            f"Evidence: {n_items} items · {n_subs} subreddits · "
            f"{n_authors} authors · aggregate pain {c.aggregate_pain:.1f}"
        )
        thesis = theses.get(c.label)
        if thesis:
            out.append("")
            out.append(f"Why underserved: {' '.join(thesis.split())}")
        out.append("")
        out.append("Evidence:")
        out.extend(_render_evidence_lines(c.evidence))
        out.append("")

    # Appendix — raw ranked clusters (compact).
    out.append("## Appendix — ranked clusters")
    out.append("")
    out.append("| # | Theme | Items | Subs | Authors | Aggregate pain | Confidence |")
    out.append("|---|-------|-------|------|---------|----------------|------------|")
    for i, c in enumerate(clusters, start=1):
        n_items, n_subs, n_authors = _counts(c)
        theme = c.label.replace("|", "\\|")
        out.append(
            f"| {i} | {theme} | {n_items} | {n_subs} | {n_authors} | "
            f"{c.aggregate_pain:.1f} | {c.confidence} |"
        )
    out.append("")
    out.append(_footer(generated))
    return "\n".join(out).rstrip() + "\n"


def _footer(generated: str | None) -> str:
    stamp = generated or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    return (
        f"---\n_Generated by prospector at {stamp}. Evidence-bound: every gap "
        "above cites real fetched Reddit items; under-evidenced clusters were "
        "dropped._"
    )
