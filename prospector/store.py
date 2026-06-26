"""SQLite persistence — dedup, querying, and evidence resolution.

The store is the engine's memory. It is deliberately dumb plumbing: it persists
exactly what the scraper fetched (posts, comments, their lexicon ``matches`` and
per-run ``sweeps``), deduplicates on the Reddit fullname, and hands back hydrated
:class:`~prospector.models.Item` objects for querying and reporting.

Schema (PRD §6):

``items``
    one row per Reddit thing, keyed by the fullname (``t3_xxx`` / ``t1_xxx``).
``matches``
    explainability — which lexicon ``pattern`` fired on which item and its
    ``weight``. Rebuilt wholesale whenever an item is upserted.
``sweeps``
    one row per sweep run, with ``params`` + ``stats`` stored as JSON blobs.

The renderer's evidence contract leans on this layer: a permalink can only ever
be cited if its item is actually in ``items`` (see :meth:`Store.get_evidence`).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from prospector.models import EvidenceItem, Item, Match, SweepResult

# Fixed column order for the ``items`` table — used for both DDL and upserts so
# the INSERT value tuple always lines up with the schema.
_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "subreddit",
    "author",
    "created_utc",
    "title",
    "body",
    "score",
    "num_comments",
    "permalink",
    "link_id",
    "parent_id",
    "pain_score",
    "fetched_at",
    "profile",
)

# Allowed ``sort`` values mapped to ORDER BY expressions. A secondary
# ``created_utc DESC`` keeps ordering stable when the primary key ties.
_SORT_EXPRESSIONS: dict[str, str] = {
    "pain": "pain_score DESC, created_utc DESC",
    "score": "score DESC, created_utc DESC",
    "new": "created_utc DESC",
    "comments": "num_comments DESC, created_utc DESC",
}

# Verbatim quote budget for resolved evidence (characters).
_QUOTE_MAX = 300


class Store:
    """SQLite-backed persistence for scraped Reddit items.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. Defaults to ``prospector.db`` in the
        working directory. Pass ``":memory:"`` for an ephemeral in-process db.
        The schema is created on first use if it does not already exist.
    """

    def __init__(self, db_path: str | Path = "prospector.db") -> None:
        self.db_path = str(db_path)
        # check_same_thread=False so the same Store can be shared by the CLI and
        # the MCP server's worker threads; callers must still serialize writes.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Pragmas: WAL for concurrent reads, foreign keys for the matches link.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # :memory: and some platforms reject WAL — harmless
            pass
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def _create_schema(self) -> None:
        """Create the ``items`` / ``matches`` / ``sweeps`` tables if absent."""
        cur = self.conn
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id           TEXT PRIMARY KEY,
                kind         TEXT,
                subreddit    TEXT,
                author       TEXT,
                created_utc  INTEGER,
                title        TEXT,
                body         TEXT,
                score        INTEGER,
                num_comments INTEGER,
                permalink    TEXT,
                link_id      TEXT,
                parent_id    TEXT,
                pain_score   REAL,
                fetched_at   INTEGER,
                profile      TEXT
            );

            CREATE TABLE IF NOT EXISTS matches (
                item_id TEXT,
                pattern TEXT,
                weight  REAL
            );

            CREATE TABLE IF NOT EXISTS sweeps (
                run_id      TEXT PRIMARY KEY,
                profile     TEXT,
                started_at  INTEGER,
                finished_at INTEGER,
                params      TEXT,
                stats       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_items_profile   ON items(profile);
            CREATE INDEX IF NOT EXISTS idx_items_subreddit ON items(subreddit);
            CREATE INDEX IF NOT EXISTS idx_items_kind      ON items(kind);
            CREATE INDEX IF NOT EXISTS idx_items_pain      ON items(pain_score);
            CREATE INDEX IF NOT EXISTS idx_matches_item    ON matches(item_id);
            CREATE INDEX IF NOT EXISTS idx_matches_pattern ON matches(pattern);
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def upsert_items(self, items: list[Item]) -> int:
        """Insert-or-replace ``items`` by :attr:`Item.id`.

        Each item's rows in the ``matches`` table are rebuilt wholesale from
        :attr:`Item.matches` (delete-then-insert), so re-upserting an item with
        a changed score replaces both its row and its explainability cleanly.

        Returns the number of items written.
        """
        if not items:
            return 0

        placeholders = ", ".join("?" for _ in _ITEM_COLUMNS)
        col_list = ", ".join(_ITEM_COLUMNS)
        insert_sql = (
            f"INSERT OR REPLACE INTO items ({col_list}) VALUES ({placeholders})"
        )

        cur = self.conn
        count = 0
        for item in items:
            cur.execute(insert_sql, self._item_to_row(item))
            # Replace this item's match rows.
            cur.execute("DELETE FROM matches WHERE item_id = ?", (item.id,))
            if item.matches:
                cur.executemany(
                    "INSERT INTO matches (item_id, pattern, weight) VALUES (?, ?, ?)",
                    [
                        (item.id, str(m.pattern), float(m.weight))
                        for m in item.matches
                    ],
                )
            count += 1
        self.conn.commit()
        return count

    def record_sweep(self, result: SweepResult) -> None:
        """Persist a :class:`SweepResult` as one row in ``sweeps``.

        ``params`` captures the run knobs and ``stats`` the full result dict
        (counts, per-subreddit tallies, top patterns) — both as JSON text so the
        schema stays stable as the result shape evolves.
        """
        params = {
            "posts_collected": result.posts_collected,
            "comments_collected": result.comments_collected,
            "threads_deep_fetched": result.threads_deep_fetched,
        }
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sweeps
                (run_id, profile, started_at, finished_at, params, stats)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                result.run_id,
                result.profile,
                int(result.started_at or 0),
                int(result.finished_at or 0),
                json.dumps(params),
                json.dumps(result.as_dict()),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get_item(self, item_id: str) -> Optional[Item]:
        """Return the hydrated :class:`Item` for ``item_id``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return None
        matches = self._load_matches([item_id]).get(item_id, [])
        return self._row_to_item(row, matches)

    def query(
        self,
        profile: str | None = None,
        subreddit: str | None = None,
        kind: str | None = None,
        min_pain: float = 0.0,
        contains: str | None = None,
        since: int | None = None,
        sort: str = "pain",
        limit: int = 100,
    ) -> list[Item]:
        """Filter stored items and return hydrated :class:`Item` objects.

        Parameters
        ----------
        profile:
            Restrict to items collected under this profile name.
        subreddit:
            Restrict to a subreddit (matched case-insensitively).
        kind:
            ``'post'`` or ``'comment'``.
        min_pain:
            Lower bound on ``pain_score`` (inclusive).
        contains:
            Case-insensitive substring searched across ``title`` + ``body``.
        since:
            Lower bound on ``created_utc`` (inclusive).
        sort:
            One of ``{'pain', 'score', 'new', 'comments'}`` (unknown values
            fall back to ``'pain'``).
        limit:
            Maximum number of rows to return.
        """
        where: list[str] = []
        args: list[object] = []

        if profile:
            where.append("profile = ?")
            args.append(profile)
        if subreddit:
            where.append("LOWER(subreddit) = LOWER(?)")
            args.append(subreddit)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if min_pain:
            where.append("pain_score >= ?")
            args.append(float(min_pain))
        if contains:
            where.append(
                "LOWER(COALESCE(title, '') || ' ' || COALESCE(body, '')) "
                "LIKE ? ESCAPE '\\'"
            )
            args.append(f"%{self._escape_like(contains.lower())}%")
        if since is not None:
            where.append("created_utc >= ?")
            args.append(int(since))

        order_by = _SORT_EXPRESSIONS.get(sort, _SORT_EXPRESSIONS["pain"])
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_by} LIMIT ?"
        args.append(int(limit))

        rows = self.conn.execute(sql, args).fetchall()
        if not rows:
            return []
        matches_by_id = self._load_matches([r["id"] for r in rows])
        return [
            self._row_to_item(r, matches_by_id.get(r["id"], [])) for r in rows
        ]

    def get_evidence(self, ids: list[str]) -> list[EvidenceItem]:
        """Resolve :class:`EvidenceItem` records for ``ids``, in order.

        Each evidence record carries the item's permalink plus a trimmed,
        verbatim ~300-char quote drawn from its title/body. Unknown ids are
        silently skipped, so the caller's order is preserved for those found.
        """
        if not ids:
            return []
        # Fetch all requested rows in one query, then re-order in Python so the
        # caller's ordering (and duplicates) are honored exactly.
        unique_ids = list(dict.fromkeys(ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = self.conn.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders})", unique_ids
        ).fetchall()
        by_id = {r["id"]: r for r in rows}

        out: list[EvidenceItem] = []
        for item_id in ids:
            row = by_id.get(item_id)
            if row is None:
                continue
            out.append(
                EvidenceItem(
                    id=row["id"],
                    permalink=row["permalink"] or "",
                    quote=self._make_quote(row["title"], row["body"]),
                    subreddit=row["subreddit"] or "",
                    author=row["author"] or "",
                    score=int(row["score"] or 0),
                    created_utc=int(row["created_utc"] or 0),
                )
            )
        return out

    def stats(self, profile: str | None = None) -> dict:
        """Return corpus statistics, optionally scoped to one ``profile``.

        The returned dict has::

            {
              'total':       int,
              'posts':       int,
              'comments':    int,
              'subreddits':  {sub: count, ...},
              'top_patterns': [(pattern, count), ...],   # most frequent first
              'date_range':  (min_created_utc, max_created_utc),  # or (None, None)
            }
        """
        where = ""
        args: list[object] = []
        if profile:
            where = " WHERE profile = ?"
            args = [profile]

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM items{where}", args
        ).fetchone()[0]
        posts = self.conn.execute(
            f"SELECT COUNT(*) FROM items{where}{' AND' if where else ' WHERE'} "
            "kind = 'post'",
            args,
        ).fetchone()[0]
        comments = self.conn.execute(
            f"SELECT COUNT(*) FROM items{where}{' AND' if where else ' WHERE'} "
            "kind = 'comment'",
            args,
        ).fetchone()[0]

        sub_rows = self.conn.execute(
            f"SELECT subreddit, COUNT(*) AS n FROM items{where} "
            "GROUP BY subreddit ORDER BY n DESC, subreddit ASC",
            args,
        ).fetchall()
        subreddits = {r["subreddit"]: r["n"] for r in sub_rows if r["subreddit"]}

        # top_patterns needs to join matches -> items to honor the profile filter.
        if profile:
            pat_rows = self.conn.execute(
                "SELECT m.pattern AS pattern, COUNT(*) AS n "
                "FROM matches m JOIN items i ON i.id = m.item_id "
                "WHERE i.profile = ? "
                "GROUP BY m.pattern ORDER BY n DESC, m.pattern ASC LIMIT 50",
                [profile],
            ).fetchall()
        else:
            pat_rows = self.conn.execute(
                "SELECT pattern, COUNT(*) AS n FROM matches "
                "GROUP BY pattern ORDER BY n DESC, pattern ASC LIMIT 50"
            ).fetchall()
        top_patterns = [(r["pattern"], r["n"]) for r in pat_rows]

        date_row = self.conn.execute(
            f"SELECT MIN(created_utc) AS lo, MAX(created_utc) AS hi FROM items{where}",
            args,
        ).fetchone()
        date_range = (date_row["lo"], date_row["hi"])

        return {
            "total": total,
            "posts": posts,
            "comments": comments,
            "subreddits": subreddits,
            "top_patterns": top_patterns,
            "date_range": date_range,
        }

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Close the underlying SQLite connection."""
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _item_to_row(item: Item) -> tuple:
        """Flatten an :class:`Item` into the ``_ITEM_COLUMNS`` value tuple."""
        return (
            item.id,
            item.kind,
            item.subreddit,
            item.author,
            int(item.created_utc or 0),
            item.title,
            item.body or "",
            int(item.score or 0),
            int(item.num_comments or 0),
            item.permalink or "",
            item.link_id,
            item.parent_id,
            float(item.pain_score or 0.0),
            int(item.fetched_at or 0),
            item.profile,
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row, matches: list[Match]) -> Item:
        """Hydrate an :class:`Item` from a row plus its preloaded matches."""
        return Item(
            id=row["id"],
            kind=row["kind"] or "",
            subreddit=row["subreddit"] or "",
            author=row["author"] or "",
            created_utc=int(row["created_utc"] or 0),
            permalink=row["permalink"] or "",
            title=row["title"],
            body=row["body"] or "",
            score=int(row["score"] or 0),
            num_comments=int(row["num_comments"] or 0),
            link_id=row["link_id"],
            parent_id=row["parent_id"],
            pain_score=float(row["pain_score"] or 0.0),
            matches=matches,
            profile=row["profile"],
            fetched_at=int(row["fetched_at"] or 0),
        )

    def _load_matches(self, item_ids: list[str]) -> dict[str, list[Match]]:
        """Batch-load match rows for the given item ids, preserving row order."""
        if not item_ids:
            return {}
        unique_ids = list(dict.fromkeys(item_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = self.conn.execute(
            f"SELECT item_id, pattern, weight FROM matches "
            f"WHERE item_id IN ({placeholders}) ORDER BY rowid",
            unique_ids,
        ).fetchall()
        out: dict[str, list[Match]] = {}
        for r in rows:
            out.setdefault(r["item_id"], []).append(
                Match(pattern=r["pattern"], weight=float(r["weight"] or 0.0))
            )
        return out

    @staticmethod
    def _escape_like(text: str) -> str:
        """Escape LIKE wildcards so ``contains`` is a literal substring."""
        return (
            text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )

    @staticmethod
    def _make_quote(title: str | None, body: str | None) -> str:
        """Build a trimmed, verbatim quote from an item's title/body."""
        text = "\n".join(p for p in (title, body) if p).strip()
        text = " ".join(text.split())  # collapse whitespace for a clean quote
        if len(text) > _QUOTE_MAX:
            # Trim on the final word boundary within budget where possible.
            cut = text[:_QUOTE_MAX].rsplit(" ", 1)[0].rstrip()
            if not cut:  # one very long token — hard cut
                cut = text[:_QUOTE_MAX]
            text = cut + "…"  # ellipsis
        return text
