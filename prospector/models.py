"""Core data models — the frozen contract every module builds against.

These dataclasses are the shared vocabulary of the engine. Do not change their
public fields without updating INTERFACES.md and every consumer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Match:
    """A single lexicon pattern that fired on an item (explainability)."""

    pattern: str
    weight: float


@dataclass
class LexiconRule:
    """One weighted regex rule from a profile's ``pain_lexicon``."""

    pattern: str  # raw regex, matched case-insensitively
    weight: float


# --------------------------------------------------------------------------- #
# Reddit content                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    """A Reddit post or comment, normalized.

    ``id`` is the Reddit *fullname* (``t3_xxx`` for posts, ``t1_xxx`` for
    comments) and is the primary key everywhere.
    """

    id: str
    kind: str  # 'post' | 'comment'
    subreddit: str
    author: str
    created_utc: int
    permalink: str  # absolute https URL — the evidence anchor
    title: Optional[str] = None  # posts only
    body: str = ""  # selftext or comment body
    score: int = 0
    num_comments: int = 0  # posts only
    link_id: Optional[str] = None  # parent post fullname (comments)
    parent_id: Optional[str] = None
    pain_score: float = 0.0
    matches: list[Match] = field(default_factory=list)
    profile: Optional[str] = None
    fetched_at: int = 0

    @property
    def text(self) -> str:
        """Title + body, for scoring and quoting."""
        return "\n".join(p for p in (self.title, self.body) if p).strip()

    @classmethod
    def from_reddit(
        cls,
        data: dict,
        kind: str,
        profile: Optional[str] = None,
        fetched_at: int = 0,
    ) -> "Item":
        """Build an :class:`Item` from a raw Reddit ``data`` payload.

        Accepts the inner ``data`` dict of a ``t3``/``t1`` thing (i.e. the
        object inside ``{"kind": "t3", "data": {...}}``).
        """
        permalink = data.get("permalink", "")
        if permalink and permalink.startswith("/"):
            permalink = "https://www.reddit.com" + permalink
        name = data.get("name") or (
            f"t3_{data['id']}" if kind == "post" else f"t1_{data['id']}"
        )
        return cls(
            id=name,
            kind=kind,
            subreddit=data.get("subreddit", "") or "",
            author=data.get("author", "") or "[deleted]",
            created_utc=int(data.get("created_utc", 0) or 0),
            permalink=permalink,
            title=data.get("title"),
            body=data.get("selftext", "") if kind == "post" else data.get("body", ""),
            score=int(data.get("score", 0) or 0),
            num_comments=int(data.get("num_comments", 0) or 0),
            link_id=data.get("link_id"),
            parent_id=data.get("parent_id"),
            profile=profile,
            fetched_at=fetched_at,
        )


# --------------------------------------------------------------------------- #
# Profiles                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceThresholds:
    """The trust contract: minimum evidence breadth for a gap to be emitted."""

    min_items: int = 5
    min_subreddits: int = 3
    min_authors: int = 3


@dataclass
class CommentConfig:
    """Stage-2 comment-fetch budget."""

    max_per_thread: int = 40
    min_score: int = 2
    depth: int = 2


@dataclass
class Profile:
    """A plug-and-play topic profile (loaded from ``profiles/<name>.yaml``)."""

    name: str
    description: str
    subreddits: list[str]
    search_terms: list[str] = field(default_factory=list)
    time_window: str = "year"  # hour|day|week|month|year|all
    listing_limit: int = 100
    max_threads: int = 60
    pain_lexicon: list[LexiconRule] = field(default_factory=list)
    pain_threshold: float = 3.0
    evidence: EvidenceThresholds = field(default_factory=EvidenceThresholds)
    comments: CommentConfig = field(default_factory=CommentConfig)
    #: On the RSS transport (no comment counts), stage 2 reads the comments of
    #: at least this many posts, ranked by pain score, then by recency.
    rss_comment_threads: int = 10


# --------------------------------------------------------------------------- #
# Evidence + run results                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceItem:
    """A single piece of citable evidence, resolved from the store."""

    id: str
    permalink: str
    quote: str
    subreddit: str
    author: str
    score: int
    created_utc: int


@dataclass
class SweepResult:
    """Summary of one two-stage sweep run."""

    run_id: str
    profile: str
    posts_collected: int = 0
    comments_collected: int = 0
    threads_deep_fetched: int = 0
    subreddits: dict[str, int] = field(default_factory=dict)  # sub -> item count
    top_patterns: list[tuple[str, int]] = field(default_factory=list)
    started_at: int = 0
    finished_at: int = 0

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "posts_collected": self.posts_collected,
            "comments_collected": self.comments_collected,
            "threads_deep_fetched": self.threads_deep_fetched,
            "subreddits": self.subreddits,
            "top_patterns": self.top_patterns,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
