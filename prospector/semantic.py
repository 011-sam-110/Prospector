"""Semantic layer: vectors, search by meaning, and topic clusters.

Optional: install with ``pip install "prospector-reddit[semantic]"``. It adds
``fastembed`` (ONNX, CPU only), ``sqlite-vec`` and ``numpy``. The rest of the
engine works without them.

Storage lives in the same SQLite file as the items:

``item_vectors``
    a ``sqlite-vec`` ``vec0`` virtual table: one 384-dimension cosine vector
    per item, keyed by the item fullname, with ``subreddit`` (lower case),
    ``kind`` and ``created_utc`` as filter columns.
``item_embeddings``
    one bookkeeping row per vector: model name, a hash of the embedded text and
    the time. :func:`embed_pending` uses it to embed only new or changed items.

The model is ``BAAI/bge-small-en-v1.5``, a pretrained model. Nothing is trained
on Reddit content. Every search hit and every cluster example is a stored item
with its real permalink and a verbatim quote, so the evidence contract holds.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

from prospector.prune import is_gone
from prospector.store import Store

__all__ = [
    "MODEL_NAME",
    "DIM",
    "QUERY_INSTRUCTION",
    "CONTRACT_KEYS",
    "SemanticUnavailable",
    "Embedder",
    "FastEmbedder",
    "LazyEmbedder",
    "EmbedStats",
    "default_embedder",
    "load_extension",
    "ensure_schema",
    "has_vectors",
    "vector_count",
    "embed_pending",
    "search",
    "contract_view",
    "clusters",
]

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384

#: BGE v1.5 retrieval instruction, added to queries only (never to documents).
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

#: The exact keys of one ``semantic-search --json`` hit. Another tool codes
#: against this list: do not add, rename or remove a key.
CONTRACT_KEYS = ("permalink", "subreddit", "title", "quote", "score", "created_utc")

VEC_TABLE = "item_vectors"
META_TABLE = "item_embeddings"

#: Characters of an item that are embedded. The model truncates at 512 tokens.
_MAX_EMBED_CHARS = 2000

_TOKEN = re.compile(r"[a-z][a-z0-9'+-]{2,}")
_STOPWORDS = frozenset(
    """
    the and for that this with you your are was were have has had not but they them
    their there what when where which who whom will would could should can just like
    from about into than then also some any all its it's i'm i've don't doesn't didn't
    can't won't isn't aren't wasn't one two get got getting been being more most much
    very really out our ours his her hers him she he we us me my mine because only
    over under again here how why yes no too own same such each other off once
    does did doing done make made know think thing things want way even still well
    use used using lot lots since while after before something anything nothing
    """.split()
)


class SemanticUnavailable(RuntimeError):
    """The optional ``[semantic]`` extra (fastembed, sqlite-vec, numpy) is missing."""


_INSTALL_HINT = (
    "the semantic layer needs the optional extra: "
    'pip install "prospector-reddit[semantic]"'
)


class Embedder(Protocol):
    """What the semantic layer needs from an embedding model."""

    name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def default_model_cache() -> Path:
    """Model cache: ``PROSPECTOR_MODEL_CACHE`` or ``~/.cache/prospector/fastembed``.

    One shared, stable folder means the model downloads once (about 67 MB) and
    every caller (CLI, MCP server, timer) reuses it.
    """
    env = os.environ.get("PROSPECTOR_MODEL_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "prospector" / "fastembed"


class FastEmbedder:
    """``fastembed`` wrapper for ``BAAI/bge-small-en-v1.5`` (CPU, ONNX)."""

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        cache_dir: Optional[Path | str] = None,
        threads: Optional[int] = None,
        batch_size: int = 32,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise SemanticUnavailable(_INSTALL_HINT) from exc
        if threads is None and os.environ.get("PROSPECTOR_EMBED_THREADS"):
            threads = int(os.environ["PROSPECTOR_EMBED_THREADS"])
        folder = Path(cache_dir) if cache_dir else default_model_cache()
        folder.mkdir(parents=True, exist_ok=True)
        self.name = model_name
        self.dim = DIM
        self.batch_size = int(batch_size)
        self._model = TextEmbedding(
            model_name=model_name, cache_dir=str(folder), threads=threads
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.passage_embed(list(texts), batch_size=self.batch_size)
        return [list(map(float, v)) for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = next(iter(self._model.query_embed(QUERY_INSTRUCTION + text)))
        return list(map(float, vector))


class LazyEmbedder:
    """An :class:`Embedder` that builds the real model on its first call.

    ``name`` and ``dim`` are known up front, so a run with nothing to embed or
    an empty store never loads the model (or needs ``fastembed`` installed).
    """

    def __init__(
        self,
        factory: Callable[[], Embedder],
        name: str = MODEL_NAME,
        dim: int = DIM,
    ) -> None:
        self._factory = factory
        self._inner: Optional[Embedder] = None
        self.name = name
        self.dim = dim

    def _model(self) -> Embedder:
        if self._inner is None:
            self._inner = self._factory()
        return self._inner

    @property
    def loaded(self) -> bool:
        """``True`` once the real model was built."""
        return self._inner is not None

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._model().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._model().embed_query(text)


_DEFAULT_EMBEDDER: Optional[Embedder] = None


def default_embedder() -> Embedder:
    """The process-wide embedder: a :class:`LazyEmbedder` over :class:`FastEmbedder`."""
    global _DEFAULT_EMBEDDER
    if _DEFAULT_EMBEDDER is None:
        _DEFAULT_EMBEDDER = LazyEmbedder(FastEmbedder)
    return _DEFAULT_EMBEDDER


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #
def load_extension(conn: sqlite3.Connection) -> None:
    """Load ``sqlite-vec`` into ``conn`` (a no-op when it is already loaded)."""
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return
    except sqlite3.OperationalError:
        pass
    try:
        import sqlite_vec
    except ImportError as exc:
        raise SemanticUnavailable(_INSTALL_HINT) from exc
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except AttributeError as exc:  # pragma: no cover - Python built without it
        raise SemanticUnavailable(
            "this Python's sqlite3 cannot load extensions, so sqlite-vec cannot run"
        ) from exc
    finally:
        try:
            conn.enable_load_extension(False)
        except AttributeError:  # pragma: no cover
            pass


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table'", (name,)
    ).fetchone()
    return row is not None


def ensure_schema(conn: sqlite3.Connection, dim: int = DIM) -> None:
    """Create the vector table and the bookkeeping table if they are missing."""
    load_extension(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {META_TABLE} (
            item_id     TEXT PRIMARY KEY,
            model       TEXT NOT NULL,
            text_hash   TEXT NOT NULL,
            embedded_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0(
            item_id TEXT PRIMARY KEY,
            embedding float[{int(dim)}] distance_metric=cosine,
            subreddit TEXT,
            kind TEXT,
            created_utc INTEGER
        )
        """
    )
    conn.commit()


def vector_count(conn: sqlite3.Connection) -> int:
    """Number of stored vectors (counted from the bookkeeping table)."""
    if not _table_exists(conn, META_TABLE):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {META_TABLE}").fetchone()[0])


def has_vectors(conn: sqlite3.Connection) -> bool:
    """``True`` when the store holds at least one vector. Needs no extension."""
    return vector_count(conn) > 0


# --------------------------------------------------------------------------- #
# Embedding                                                                    #
# --------------------------------------------------------------------------- #
def embed_text(title: Optional[str], body: Optional[str]) -> str:
    """The text that is embedded for an item: title and body, trimmed."""
    text = "\n\n".join(p.strip() for p in (title, body) if p and p.strip())
    return text[:_MAX_EMBED_CHARS]


def _text_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode("utf-8")).hexdigest()


def _to_blob(vector: Sequence[float]) -> bytes:
    import sqlite_vec

    return sqlite_vec.serialize_float32(list(vector))


@dataclass
class EmbedStats:
    """Counts from one :func:`embed_pending` run."""

    embedded: int = 0
    unchanged: int = 0
    skipped_empty: int = 0
    skipped_gone: int = 0
    vectors: int = 0
    model: str = MODEL_NAME

    def as_dict(self) -> dict:
        return asdict(self)


def embed_pending(
    conn: sqlite3.Connection,
    embedder: Embedder,
    batch_size: int = 64,
    limit: Optional[int] = None,
    now: Optional[int] = None,
    log: Callable[[str], object] = lambda _m: None,
) -> EmbedStats:
    """Embed every item that has no vector, or whose text or model changed.

    Items with no text, or that read as deleted/removed, are skipped (the prune
    step deletes the latter). Commits after each batch, so an interrupted run
    keeps its progress.
    """
    ensure_schema(conn, embedder.dim)
    if now is None:
        now = int(time.time())
    stats = EmbedStats(model=embedder.name)
    rows = conn.execute(
        f"""
        SELECT i.id, i.kind, i.subreddit, i.created_utc, i.title, i.body, i.author,
               e.text_hash, e.model
        FROM items i LEFT JOIN {META_TABLE} e ON e.item_id = i.id
        ORDER BY i.created_utc DESC
        """
    ).fetchall()

    pending: list[tuple] = []
    for row in rows:
        item_id, kind, subreddit, created, title, body, author, old_hash, old_model = row
        if is_gone(title, body, author):
            stats.skipped_gone += 1
            continue
        text = embed_text(title, body)
        if not text:
            stats.skipped_empty += 1
            continue
        digest = _text_hash(embedder.name, text)
        if old_hash == digest and old_model == embedder.name:
            stats.unchanged += 1
            continue
        pending.append((item_id, kind, subreddit, created, text, digest))
    if limit is not None:
        pending = pending[: max(0, int(limit))]

    size = max(1, int(batch_size))
    for start in range(0, len(pending), size):
        batch = pending[start : start + size]
        vectors = embedder.embed_documents([p[4] for p in batch])
        with conn:
            for (item_id, kind, subreddit, created, _text, digest), vector in zip(
                batch, vectors
            ):
                conn.execute(f"DELETE FROM {VEC_TABLE} WHERE item_id = ?", (item_id,))
                conn.execute(
                    f"INSERT INTO {VEC_TABLE} (item_id, embedding, subreddit, kind, created_utc) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item_id,
                        _to_blob(vector),
                        (subreddit or "").lower(),
                        kind or "",
                        int(created or 0),
                    ),
                )
                conn.execute(
                    f"INSERT OR REPLACE INTO {META_TABLE} "
                    "(item_id, model, text_hash, embedded_at) VALUES (?, ?, ?, ?)",
                    (item_id, embedder.name, digest, int(now)),
                )
        stats.embedded += len(batch)
        log(f"[embed] {stats.embedded}/{len(pending)}")
    stats.vectors = vector_count(conn)
    return stats


# --------------------------------------------------------------------------- #
# Search                                                                       #
# --------------------------------------------------------------------------- #
def _hydrate(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    """Load items (plus the parent post title of comments) by id."""
    out: dict[str, sqlite3.Row] = {}
    unique = list(dict.fromkeys(ids))
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT i.id, i.kind, i.subreddit, i.title, i.body, i.permalink,
                       i.created_utc, i.author, p.title AS parent_title
                FROM items i LEFT JOIN items p ON p.id = i.link_id
                WHERE i.id IN ({marks})
                """,
                chunk,
            ).fetchall():
                out[row["id"]] = row
    finally:
        conn.row_factory = old_factory
    return out


def _hit(row: sqlite3.Row, similarity: float) -> dict:
    """One hit: the six contract keys first, then ``id`` and ``kind``."""
    return {
        "permalink": row["permalink"] or "",
        "subreddit": row["subreddit"] or "",
        "title": row["title"] or row["parent_title"] or "",
        "quote": Store._make_quote(row["title"], row["body"]),
        "score": round(float(similarity), 4),
        "created_utc": int(row["created_utc"] or 0),
        "id": row["id"],
        "kind": row["kind"] or "",
    }


def search(
    conn: sqlite3.Connection,
    embedder: Embedder,
    query: str,
    limit: int = 10,
    subreddit: Optional[str] = None,
    since: Optional[int] = None,
    kind: Optional[str] = None,
) -> list[dict]:
    """Return the stored items closest in meaning to ``query``, best first.

    ``score`` is the cosine similarity between the query and the item (1.0 is
    identical, higher is closer). It is not the Reddit vote score. Returns
    ``[]`` when the store has no vectors, without loading the model.
    """
    if not has_vectors(conn) or not (query or "").strip():
        return []
    load_extension(conn)
    k = max(1, min(int(limit), 4096))
    where = ["embedding MATCH ?", "k = ?"]
    args: list[object] = [_to_blob(embedder.embed_query(query.strip())), k]
    if subreddit:
        where.append("subreddit = ?")
        args.append(subreddit.lower())
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if since is not None:
        where.append("created_utc >= ?")
        args.append(int(since))
    rows = conn.execute(
        f"SELECT item_id, distance FROM {VEC_TABLE} WHERE {' AND '.join(where)} "
        "ORDER BY distance",
        args,
    ).fetchall()
    found = _hydrate(conn, [r[0] for r in rows])
    hits = []
    for item_id, distance in rows:
        row = found.get(item_id)
        if row is None:  # a vector whose item was deleted outside prune
            continue
        hits.append(_hit(row, 1.0 - float(distance)))
    return hits[:k]


def contract_view(hits: list[dict]) -> list[dict]:
    """Project hits onto exactly :data:`CONTRACT_KEYS` (the CLI ``--json`` shape)."""
    return [{key: hit[key] for key in CONTRACT_KEYS} for hit in hits]


# --------------------------------------------------------------------------- #
# Clusters                                                                     #
# --------------------------------------------------------------------------- #
def _load_vectors(
    conn: sqlite3.Connection,
    subreddit: Optional[str],
    profile: Optional[str],
    since: Optional[int],
    max_items: int,
):
    import numpy as np

    where: list[str] = []
    args: list[object] = []
    if subreddit:
        where.append("LOWER(subreddit) = LOWER(?)")
        args.append(subreddit)
    if profile:
        where.append("profile = ?")
        args.append(profile)
    if since is not None:
        where.append("created_utc >= ?")
        args.append(int(since))
    sql = "SELECT id FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    allowed = {r[0] for r in conn.execute(sql, args).fetchall()}
    if not allowed:
        return [], None

    load_extension(conn)
    ids: list[str] = []
    rows: list = []
    for item_id, blob in conn.execute(
        f"SELECT item_id, embedding FROM {VEC_TABLE}"
    ).fetchall():
        if item_id in allowed:
            ids.append(item_id)
            rows.append(np.frombuffer(blob, dtype=np.float32))
    if not ids:
        return [], None
    if len(ids) > max_items:
        # Keep the newest items when the set is too large.
        created = dict(conn.execute("SELECT id, created_utc FROM items").fetchall())
        order = sorted(range(len(ids)), key=lambda i: created.get(ids[i], 0), reverse=True)
        keep = sorted(order[:max_items])
        ids = [ids[i] for i in keep]
        rows = [rows[i] for i in keep]
    matrix = np.vstack(rows).astype(np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, matrix / norms


def _kmeans(matrix, k: int, seed: int = 0, iterations: int = 50):
    """Spherical k-means (cosine) with k-means++ seeding. Deterministic for a seed."""
    import numpy as np

    rng = np.random.default_rng(seed)
    n = matrix.shape[0]
    first = int(rng.integers(n))
    centers = [matrix[first]]
    nearest = 1.0 - matrix @ matrix[first]
    for _ in range(1, k):
        weights = np.clip(nearest, 0.0, None)
        total = float(weights.sum())
        if total <= 0:
            pick = int(rng.integers(n))
        else:
            pick = int(rng.choice(n, p=weights / total))
        centers.append(matrix[pick])
        nearest = np.minimum(nearest, 1.0 - matrix @ matrix[pick])
    centroids = np.vstack(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iterations):
        labels = (matrix @ centroids.T).argmax(axis=1)
        updated = centroids.copy()
        for j in range(k):
            members = matrix[labels == j]
            if len(members):
                mean = members.mean(axis=0)
                norm = float(np.linalg.norm(mean))
                updated[j] = mean / norm if norm else centroids[j]
        if np.allclose(updated, centroids):
            break
        centroids = updated
    return labels, centroids


def _keywords(texts_by_cluster: dict[int, list[str]], top: int = 6) -> dict[int, list[str]]:
    """Distinctive words per cluster: share of the cluster's items that use a
    word, weighted down when many clusters use it (a simple c-TF-IDF)."""
    doc_terms: dict[int, list[set[str]]] = {}
    for label, texts in texts_by_cluster.items():
        doc_terms[label] = [
            {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}
            for text in texts
        ]
    clusters_with: Counter[str] = Counter()
    per_cluster: dict[int, Counter[str]] = {}
    for label, docs in doc_terms.items():
        counts: Counter[str] = Counter()
        for terms in docs:
            counts.update(terms)
        per_cluster[label] = counts
        clusters_with.update(counts.keys())
    n_clusters = max(1, len(doc_terms))
    out: dict[int, list[str]] = {}
    for label, counts in per_cluster.items():
        size = max(1, len(doc_terms[label]))
        scored = [
            (count / size * math.log(1.0 + n_clusters / clusters_with[term]), term)
            for term, count in counts.items()
            if count >= 2
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        out[label] = [term for _score, term in scored[:top]]
    return out


def clusters(
    conn: sqlite3.Connection,
    k: Optional[int] = None,
    subreddit: Optional[str] = None,
    profile: Optional[str] = None,
    since: Optional[int] = None,
    examples: int = 3,
    max_items: int = 20000,
    seed: int = 0,
) -> list[dict]:
    """Group stored items by meaning and describe each group.

    Each cluster has its ``size``, its ``share`` of the items, distinctive
    ``keywords``, a per-``subreddits`` count and ``examples``: the items nearest
    the cluster centre, each with its permalink and verbatim quote. Clusters are
    sorted by size, largest first. ``k`` defaults to about sqrt(n/2), from 2 to
    12. Returns ``[]`` when no vectors match.
    """
    if not has_vectors(conn):
        return []
    try:
        import numpy as np
    except ImportError as exc:
        raise SemanticUnavailable(_INSTALL_HINT) from exc
    ids, matrix = _load_vectors(conn, subreddit, profile, since, int(max_items))
    if not ids:
        return []
    n = len(ids)
    if k is None or int(k) <= 0:
        k = max(2, min(12, int(round(math.sqrt(n / 2.0)))))
    k = max(1, min(int(k), n))
    labels, centroids = _kmeans(matrix, k, seed=seed)
    sims = (matrix * centroids[labels]).sum(axis=1)
    found = _hydrate(conn, ids)

    members: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        members.setdefault(int(label), []).append(index)
    texts = {
        label: [
            embed_text(found[ids[i]]["title"], found[ids[i]]["body"])
            for i in idx
            if ids[i] in found
        ]
        for label, idx in members.items()
    }
    words = _keywords(texts)

    out = []
    for label, idx in members.items():
        ranked = sorted(idx, key=lambda i: float(sims[i]), reverse=True)
        subs: Counter[str] = Counter(
            found[ids[i]]["subreddit"] for i in idx if ids[i] in found
        )
        sample = []
        for i in ranked:
            row = found.get(ids[i])
            if row is None:
                continue
            sample.append(_hit(row, float(sims[i])))
            if len(sample) >= max(0, int(examples)):
                break
        out.append(
            {
                "size": len(idx),
                "share": round(len(idx) / n, 4),
                "keywords": words.get(label, []),
                "subreddits": dict(subs.most_common()),
                "examples": sample,
            }
        )
    out.sort(key=lambda c: (-c["size"], c["keywords"]))
    for number, cluster in enumerate(out, start=1):
        cluster["cluster"] = number
    return [
        {"cluster": c["cluster"], **{key: c[key] for key in c if key != "cluster"}}
        for c in out
    ]
