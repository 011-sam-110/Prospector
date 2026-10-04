"""Shared test helpers for the semantic layer: a fake embedder and item builders.

The fake embedder hashes words into a 384-dimension bag-of-words vector, so two
texts that share words are close. It needs no model download and no network.
"""

from __future__ import annotations

import hashlib
import math
import re

from prospector.models import Item

NOW = 1_791_200_000  # 2026-10-05, a fixed "now" for age tests
DAY = 86_400

_WORD = re.compile(r"[a-z]+")


class FakeEmbedder:
    name = "fake-bow-384"
    dim = 384

    def __init__(self):
        self.document_calls = 0
        self.query_calls = 0

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim
            vec[slot] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        self.document_calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return self._vec(text)


def make_item(
    item_id: str,
    body: str,
    title: str | None = None,
    sub: str = "examplenursing",
    age_days: float = 1.0,
    author: str = "someone",
    kind: str | None = None,
    link_id: str | None = None,
    fetched_age_days: float | None = None,
    profile: str = "demo",
) -> Item:
    kind = kind or ("post" if item_id.startswith("t3_") else "comment")
    bare = item_id.split("_", 1)[1]
    thread = (link_id or item_id).split("_", 1)[1]
    permalink = f"https://www.reddit.com/r/{sub}/comments/{thread}/x/"
    if kind == "comment":
        permalink += f"{bare}/"
    fetched = fetched_age_days if fetched_age_days is not None else min(age_days, 0.5)
    return Item(
        id=item_id,
        kind=kind,
        subreddit=sub,
        author=author,
        created_utc=int(NOW - age_days * DAY),
        permalink=permalink,
        title=title,
        body=body,
        link_id=link_id,
        profile=profile,
        fetched_at=int(NOW - fetched * DAY),
    )
