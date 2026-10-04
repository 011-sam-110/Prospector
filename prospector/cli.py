"""Command-line interface — the standalone / open-source face of prospector.

A thin :mod:`typer` app that wires the engine modules together:

  * ``profiles``  — list the available topic profiles
  * ``sweep``     — run the two-stage Reddit sweep for a profile
  * ``query``     — print a compact table of stored items
  * ``report``    — render the evidence-bound Markdown report
  * ``export``    — dump stored items as json / csv / md
  * ``embed``     - write vectors for new or changed items (``[semantic]`` extra)
  * ``semantic-search`` - find stored items by meaning
  * ``clusters``  - group stored items by meaning, with quotes
  * ``prune``     - delete old items and items deleted on Reddit
  * ``mcp``       — run the MCP server so Claude becomes the "brain"

The CLI itself holds no business logic; it loads profiles, builds a
:class:`~prospector.reddit_client.RedditClient` + :class:`~prospector.store.Store`,
and delegates to :func:`prospector.scrape.sweep` and
:func:`prospector.report.render_report`. ``app`` is importable as
``prospector.cli:app`` and ``main()`` is the console entry point.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import time as _time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer

from prospector import prune as prune_mod
from prospector import scrape
from prospector.models import Item, SweepResult
from prospector.profiles import list_profiles, load_profile
from prospector.reddit_client import TRANSPORTS, RedditClient, default_cache_dir
from prospector.report import render_report
from prospector.store import Store

# Upper bound on rows pulled for an export (effectively "everything stored").
_EXPORT_LIMIT = 10_000
_VALID_FORMATS = ("json", "csv", "md")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Mine Reddit for unmet needs — fetch, score, store and render real signal.",
)


# --------------------------------------------------------------------------- #
# Small presentation helpers                                                  #
# --------------------------------------------------------------------------- #
def _short(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` chars (single line, ASCII-safe ellipsis)."""
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _title_of(item: Item) -> str:
    """Best human label for an item: its title, else the first line of the body."""
    if item.title:
        return item.title
    body = (item.body or "").strip()
    return body.splitlines()[0] if body else "(no title)"


def _load(profile_name: str):
    """Load a profile, turning a missing profile into a clean CLI error."""
    try:
        return load_profile(profile_name)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


def _err(message: str) -> None:
    """Print a progress or error line to stderr (stdout stays machine-readable)."""
    typer.echo(str(message), err=True)


def _check_transport(transport: str) -> None:
    if transport not in TRANSPORTS:
        _err(f"Unknown transport '{transport}'. Choose one of: {', '.join(TRANSPORTS)}.")
        raise typer.Exit(code=2)


def _open_existing(db: str) -> Optional[sqlite3.Connection]:
    """Open ``db`` only when the file exists. A search never creates a store."""
    if db != ":memory:" and not Path(db).is_file():
        return None
    return sqlite3.connect(db, timeout=30.0)


def _semantic():
    """Import the semantic layer (its own imports are optional deps, loaded late)."""
    from prospector import semantic

    return semantic


def _since(days: Optional[float]) -> Optional[int]:
    if days is None or days <= 0:
        return None
    return int(_time.time() - float(days) * 86400)


def _print_sweep_summary(result: SweepResult) -> None:
    """Pretty-print a :class:`SweepResult` to stdout."""
    duration = max(0, int(result.finished_at) - int(result.started_at))
    typer.echo("")
    typer.echo(f"Sweep complete  (run {result.run_id})")
    typer.echo(f"  profile:            {result.profile}")
    typer.echo(f"  posts collected:    {result.posts_collected}")
    typer.echo(f"  comments collected: {result.comments_collected}")
    typer.echo(f"  threads deep-read:  {result.threads_deep_fetched}")
    typer.echo(f"  duration:           {duration}s")
    if result.subreddits:
        typer.echo("  per-subreddit:")
        for sub, count in sorted(result.subreddits.items(), key=lambda kv: (-kv[1], kv[0])):
            typer.echo(f"    r/{sub}: {count}")
    if result.top_patterns:
        typer.echo("  top pain patterns:")
        for pattern, count in result.top_patterns[:10]:
            typer.echo(f"    {count:>4}  {pattern}")


def _print_item_table(items: list[Item]) -> None:
    """Print a compact ``id | sub | pain | score | title`` table."""
    if not items:
        typer.echo("No items match.")
        return
    header = f"{'id':<14} {'subreddit':<18} {'pain':>5} {'score':>6}  title"
    typer.echo(header)
    typer.echo("-" * len(header))
    for it in items:
        typer.echo(
            f"{_short(it.id, 14):<14} "
            f"{_short(it.subreddit, 18):<18} "
            f"{it.pain_score:>5.1f} "
            f"{it.score:>6}  "
            f"{_short(_title_of(it), 60)}"
        )
    typer.echo("")
    typer.echo(f"{len(items)} item(s).")


# --------------------------------------------------------------------------- #
# Serialization (export)                                                       #
# --------------------------------------------------------------------------- #
def _items_to_json(items: list[Item]) -> str:
    return json.dumps([asdict(it) for it in items], indent=2, ensure_ascii=False)


def _items_to_csv(items: list[Item]) -> str:
    columns = [
        "id",
        "kind",
        "subreddit",
        "author",
        "created_utc",
        "score",
        "num_comments",
        "pain_score",
        "permalink",
        "title",
        "body",
    ]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for it in items:
        writer.writerow(
            {
                "id": it.id,
                "kind": it.kind,
                "subreddit": it.subreddit,
                "author": it.author,
                "created_utc": it.created_utc,
                "score": it.score,
                "num_comments": it.num_comments,
                "pain_score": it.pain_score,
                "permalink": it.permalink,
                "title": it.title or "",
                "body": (it.body or "").replace("\r\n", "\n"),
            }
        )
    return buf.getvalue()


def _items_to_md(items: list[Item], profile: str) -> str:
    lines = [f"# Export: {profile} ({len(items)} items)", ""]
    for it in items:
        lines.append(f"## [{it.pain_score:.1f}] r/{it.subreddit} — {_title_of(it)}")
        lines.append(
            f"- score: {it.score} · comments: {it.num_comments} · author: {it.author}"
        )
        if it.permalink:
            lines.append(f"- {it.permalink}")
        if it.matches:
            patterns = ", ".join(f"`{m.pattern}`" for m in it.matches)
            lines.append(f"- matched: {patterns}")
        body = (it.body or "").strip()
        if body:
            lines.append("")
            lines.append(f"> {_short(body, 300)}")
        lines.append("")
    return "\n".join(lines)


def _serialize(items: list[Item], fmt: str, profile: str) -> str:
    if fmt == "json":
        return _items_to_json(items)
    if fmt == "csv":
        return _items_to_csv(items)
    return _items_to_md(items, profile)


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #
@app.command()
def profiles() -> None:
    """List the available topic profiles (``profiles/*.yaml``)."""
    names = list_profiles()
    if not names:
        typer.echo("No profiles found.")
        return
    typer.echo("Available profiles:")
    for name in names:
        typer.echo(f"  - {name}")


@app.command()
def sweep(
    profile: str = typer.Argument(..., help="Profile name or path to a .yaml file."),
    time: Optional[str] = typer.Option(
        None, "--time", help="Time window override: hour|day|week|month|year|all."
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Stage-1 listing limit override (posts per sub)."
    ),
    max_threads: Optional[int] = typer.Option(
        None, "--max-threads", help="Stage-2 deep-fetch thread budget override."
    ),
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
    oauth: bool = typer.Option(
        False,
        "--oauth",
        help="Require Reddit OAuth (needs REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET).",
    ),
    transport: str = typer.Option(
        "auto",
        "--transport",
        help="auto (.json, then RSS after a 403) | json | rss.",
    ),
    combine_terms: bool = typer.Option(
        False,
        "--combine-terms",
        help="One OR-joined search per subreddit in place of one per term.",
    ),
) -> None:
    """Run the two-stage Reddit sweep for PROFILE and store the scored results."""
    _check_transport(transport)
    prof = _load(profile)

    has_creds = bool(
        os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")
    )
    if oauth and not has_creds:
        typer.echo(
            "--oauth was given but REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set "
            "in the environment.",
            err=True,
        )
        raise typer.Exit(code=2)

    mode = "OAuth" if has_creds else "anonymous"
    typer.echo(f"Sweeping '{prof.name}' as {mode} -> {db}")

    client = RedditClient(transport=transport, log=_err)
    store = Store(db)
    try:
        sweep_kwargs = dict(
            time_window=time,
            listing_limit=limit,
            max_threads=max_threads,
            log=lambda message: typer.echo(str(message)),
        )
        if combine_terms:
            sweep_kwargs["combine_terms"] = True
        result = scrape.sweep(prof, client, store, **sweep_kwargs)
        _print_sweep_summary(result)
        rss = getattr(client, "_rss", None)
        typer.echo(
            f"fetched={result.posts_collected + result.comments_collected} "
            f"posts={result.posts_collected} comments={result.comments_collected} "
            f"transport={getattr(client, 'transport_in_use', 'json')} "
            f"rss_requests={getattr(rss, 'requests_made', 0)}"
        )
    finally:
        store.close()
        close = getattr(client, "close", None)
        if callable(close):
            close()


@app.command()
def query(
    profile: str = typer.Argument(..., help="Profile name to filter stored items by."),
    min_pain: float = typer.Option(0.0, "--min-pain", help="Minimum pain score."),
    sub: Optional[str] = typer.Option(None, "--sub", help="Restrict to a subreddit."),
    contains: Optional[str] = typer.Option(
        None, "--contains", help="Case-insensitive substring over title+body."
    ),
    sort: str = typer.Option("pain", "--sort", help="pain|score|new|comments."),
    limit: int = typer.Option(50, "--limit", help="Max rows to show."),
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
) -> None:
    """Print a compact table of stored items matching the filters."""
    store = Store(db)
    try:
        items = store.query(
            profile=profile,
            subreddit=sub,
            min_pain=min_pain,
            contains=contains,
            sort=sort,
            limit=limit,
        )
    finally:
        store.close()
    _print_item_table(items)


@app.command()
def report(
    profile: str = typer.Argument(..., help="Profile name or path to a .yaml file."),
    analyze: bool = typer.Option(
        False, "--analyze", help="Add an LLM thesis per gap (graceful if no LLM)."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Write the Markdown here instead of stdout."
    ),
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
) -> None:
    """Render the evidence-bound Markdown report for PROFILE."""
    prof = _load(profile)
    store = Store(db)
    try:
        markdown = render_report(prof, store, analyze=analyze)
    finally:
        store.close()

    if out is not None:
        out = Path(out)
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"Wrote report to {out}")
    else:
        typer.echo(markdown)


@app.command()
def export(
    profile: str = typer.Argument(..., help="Profile name to export stored items for."),
    format: str = typer.Option(
        "json", "--format", "-f", help="Output format: json|csv|md."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Write the payload here instead of stdout."
    ),
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
) -> None:
    """Dump stored items for PROFILE as json, csv or md."""
    fmt = format.lower()
    if fmt not in _VALID_FORMATS:
        typer.echo(
            f"Unknown format '{format}'. Choose one of: {', '.join(_VALID_FORMATS)}.",
            err=True,
        )
        raise typer.Exit(code=2)

    store = Store(db)
    try:
        items = store.query(profile=profile, limit=_EXPORT_LIMIT, sort="pain")
    finally:
        store.close()

    payload = _serialize(items, fmt, profile)
    if out is not None:
        out = Path(out)
        out.write_text(payload, encoding="utf-8")
        typer.echo(f"Wrote {len(items)} item(s) to {out}")
    else:
        typer.echo(payload)


@app.command()
def embed(
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
    batch_size: int = typer.Option(64, "--batch-size", help="Items per model call."),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Embed at most this many items in this run."
    ),
) -> None:
    """Write vectors for stored items that are new or changed (semantic extra)."""
    semantic = _semantic()
    store = Store(db)
    try:
        stats = semantic.embed_pending(
            store.conn,
            semantic.default_embedder(),
            batch_size=batch_size,
            limit=limit,
            log=_err,
        )
    except semantic.SemanticUnavailable as exc:
        _err(str(exc))
        raise typer.Exit(code=1)
    finally:
        store.close()
    typer.echo(
        f"embedded={stats.embedded} unchanged={stats.unchanged} "
        f"skipped_empty={stats.skipped_empty} skipped_gone={stats.skipped_gone} "
        f"vectors={stats.vectors} model={stats.model}"
    )


@app.command("semantic-search")
def semantic_search(
    query: str = typer.Argument(..., help="What to look for, in plain words."),
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
    limit: int = typer.Option(10, "--limit", help="Maximum number of hits."),
    sub: Optional[str] = typer.Option(None, "--sub", help="Only this subreddit."),
    since_days: Optional[float] = typer.Option(
        None, "--since-days", help="Only items posted in the last N days."
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print a JSON array of {permalink, subreddit, title, quote, score, "
        "created_utc}. score = cosine similarity, higher is closer.",
    ),
) -> None:
    """Find stored posts and comments by meaning, not by exact words."""
    semantic = _semantic()
    hits: list[dict] = []
    conn = _open_existing(db)
    if conn is not None:
        try:
            if semantic.has_vectors(conn):
                hits = semantic.search(
                    conn,
                    semantic.default_embedder(),
                    query,
                    limit=max(1, int(limit)),
                    subreddit=sub,
                    since=_since(since_days),
                )
        except semantic.SemanticUnavailable as exc:
            _err(str(exc))
            raise typer.Exit(code=1)
        except sqlite3.DatabaseError as exc:
            _err(f"Cannot read the store at {db}: {exc}")
            raise typer.Exit(code=1)
        finally:
            conn.close()

    if as_json:
        typer.echo(json.dumps(semantic.contract_view(hits), ensure_ascii=True))
        return
    if not hits:
        typer.echo("No matches (the store has no vectors yet, or nothing is close).")
    for rank, hit in enumerate(hits, start=1):
        typer.echo(
            f"{rank:>2}. [{hit['score']:.3f}] r/{hit['subreddit']} "
            f"{_short(hit['title'] or '(comment)', 70)}"
        )
        typer.echo(f"    {hit['permalink']}")
        typer.echo(f"    > {_short(hit['quote'], 160)}")
    typer.echo(f"searched={len(hits)}")


@app.command()
def clusters(
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
    sub: Optional[str] = typer.Option(None, "--sub", help="Only this subreddit."),
    profile: Optional[str] = typer.Option(
        None, "--profile", help="Only items collected under this profile."
    ),
    since_days: Optional[float] = typer.Option(
        None, "--since-days", help="Only items posted in the last N days."
    ),
    k: int = typer.Option(0, "--k", help="Number of clusters (0 = choose from the data)."),
    examples: int = typer.Option(3, "--examples", help="Quotes per cluster."),
    as_json: bool = typer.Option(False, "--json", help="Print the clusters as JSON."),
) -> None:
    """Group stored items by meaning. Each group shows its size and real quotes."""
    semantic = _semantic()
    groups: list[dict] = []
    conn = _open_existing(db)
    if conn is not None:
        try:
            groups = semantic.clusters(
                conn,
                k=k or None,
                subreddit=sub,
                profile=profile,
                since=_since(since_days),
                examples=examples,
            )
        except semantic.SemanticUnavailable as exc:
            _err(str(exc))
            raise typer.Exit(code=1)
        finally:
            conn.close()

    if as_json:
        typer.echo(json.dumps(groups, ensure_ascii=True, indent=2))
        return
    if not groups:
        typer.echo("No clusters (the store has no vectors that match).")
        return
    total = sum(g["size"] for g in groups)
    typer.echo(f"{len(groups)} clusters over {total} items")
    for group in groups:
        subs = ", ".join(f"r/{s} {n}" for s, n in list(group["subreddits"].items())[:4])
        typer.echo("")
        typer.echo(
            f"#{group['cluster']}  {group['size']} items ({group['share'] * 100:.1f}%)  "
            f"keywords: {', '.join(group['keywords']) or '-'}"
        )
        typer.echo(f"    {subs}")
        for ex in group["examples"]:
            typer.echo(f"    - {ex['permalink']}")
            typer.echo(f"      > {_short(ex['quote'], 150)}")


@app.command()
def prune(
    db: str = typer.Option("prospector.db", "--db", help="SQLite store path."),
    max_age_days: float = typer.Option(
        prune_mod.DEFAULT_MAX_AGE_DAYS,
        "--max-age-days",
        help="Delete items posted longer ago than this.",
    ),
    recheck: bool = typer.Option(
        True,
        "--recheck/--no-recheck",
        help="Ask Reddit which stored items were deleted or removed (network).",
    ),
    skip_fresh_hours: float = typer.Option(
        prune_mod.DEFAULT_SKIP_FRESH_HOURS,
        "--skip-fresh-hours",
        help="Do not recheck items fetched in the last N hours.",
    ),
    transport: str = typer.Option(
        "auto", "--transport", help="auto | json | rss, for the recheck."
    ),
    cache_dir: Optional[Path] = typer.Option(
        None, "--cache-dir", help="Response cache to purge (default: the client cache)."
    ),
    cache_max_age_hours: float = typer.Option(
        prune_mod.DEFAULT_CACHE_MAX_AGE_HOURS,
        "--cache-max-age-hours",
        help="Delete cache files older than this.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the counts as JSON."),
) -> None:
    """Delete items older than the age limit and items deleted or removed on Reddit."""
    _check_transport(transport)
    store = Store(db)
    client = RedditClient(transport=transport, log=_err) if recheck else None
    try:
        result = prune_mod.prune(
            store.conn,
            max_age_days=max_age_days,
            client=client,
            skip_fresh_hours=skip_fresh_hours,
            log=_err,
        )
    except Exception as exc:  # noqa: BLE001 - SemanticUnavailable and sqlite errors
        _err(f"prune failed: {exc}")
        raise typer.Exit(code=1)
    finally:
        store.close()
        if client is not None:
            client.close()
    result.cache_files_deleted = prune_mod.purge_cache(
        cache_dir or default_cache_dir(), cache_max_age_hours
    )
    if as_json:
        typer.echo(json.dumps(result.as_dict()))
        return
    typer.echo(
        f"pruned={result.deleted} expired={result.expired} "
        f"gone_in_store={result.gone_in_store} gone_on_reddit={result.gone_on_reddit} "
        f"missing_on_reddit={result.missing_on_reddit} rechecked={result.rechecked} "
        f"recheck_requests={result.recheck_requests} "
        f"recheck_inconclusive={result.recheck_inconclusive} "
        f"vectors_deleted={result.vectors_deleted} "
        f"cache_files_deleted={result.cache_files_deleted} remaining={result.remaining}"
    )


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio so Claude can drive the engine."""
    # Imported lazily: the rest of the CLI must load even if fastmcp is missing.
    try:
        from prospector import mcp_server
    except Exception as exc:  # pragma: no cover - import-time env issue
        typer.echo(f"Could not load the MCP server: {exc}", err=True)
        typer.echo(
            "Install the optional 'fastmcp' dependency to use 'prospector mcp'.",
            err=True,
        )
        raise typer.Exit(code=1)
    mcp_server.main()


def main() -> None:
    """Console entry point (``prospector ...``)."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
