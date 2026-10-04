# prospector — Frozen Interface Contract

**This file is the contract.** Every module is implemented against the signatures
below. Do **not** change a public signature without updating this file and every
consumer. The data models live in `prospector/models.py` (already implemented)
and `prospector/profiles.py` (already implemented). Import from there — never
redefine these types.

Package layout:

```
prospector/
  __init__.py        # done — re-exports models
  models.py          # done — Item, Match, LexiconRule, Profile, EvidenceThresholds,
                     #        CommentConfig, EvidenceItem, SweepResult
  profiles.py        # done — load_profile(), list_profiles(), resolve_profiles_dir()
  errors.py          # RedditError (shared by both transports)
  reddit_client.py   # AGENT A
  reddit_rss.py      # RSS transport (fallback after a .json 403)
  prune.py           # retention + deleted-content removal
  semantic.py        # vectors, semantic search, clusters ([semantic] extra)
  store.py           # AGENT B
  scorer.py          # AGENT C
  scrape.py          # AGENT D
  report.py          # AGENT E
  analyze.py         # AGENT E
  cli.py             # AGENT F
  mcp_server.py      # AGENT G
profiles/*.yaml      # done
tests/test_*.py      # each agent writes its own
```

General rules for all agents:
- Python ≥3.10, `from __future__ import annotations` at the top of every file.
- Standard library + the declared deps only (`httpx`, `typer`, `pyyaml`, `fastmcp`,
  and optionally `openai` for analyze). No other third-party imports.
- Pure functions where possible; pass `now`/`run_id` in (don't scatter `time.time()`
  / `uuid` calls) so things stay testable — but a default of `time.time()` /
  `uuid4().hex` is fine.
- Every agent writes a `tests/test_<module>.py` with `pytest` tests that run
  **offline** (no live network). Mock/stub Reddit and the LLM.
- Docstrings on public functions. Match the house style of models.py.

---

## `prospector/models.py`  (DONE — reference only)

```python
@dataclass Item:
    id: str; kind: str; subreddit: str; author: str; created_utc: int
    permalink: str; title: str|None; body: str; score: int; num_comments: int
    link_id: str|None; parent_id: str|None; pain_score: float
    matches: list[Match]; profile: str|None; fetched_at: int
    @property text -> str
    @classmethod from_reddit(data: dict, kind: str, profile=None, fetched_at=0) -> Item

@dataclass Match: pattern: str; weight: float
@dataclass LexiconRule: pattern: str; weight: float
@dataclass EvidenceThresholds: min_items=5; min_subreddits=3; min_authors=3
@dataclass CommentConfig: max_per_thread=40; min_score=2; depth=2
@dataclass Profile:
    name; description; subreddits: list[str]; search_terms: list[str]
    time_window="year"; listing_limit=100; max_threads=60
    pain_lexicon: list[LexiconRule]; pain_threshold=3.0
    evidence: EvidenceThresholds; comments: CommentConfig
    rss_comment_threads=10        # added in 0.2: RSS stage-2 thread floor
@dataclass EvidenceItem:
    id; permalink; quote; subreddit; author; score; created_utc
@dataclass SweepResult:
    run_id; profile; posts_collected; comments_collected; threads_deep_fetched
    subreddits: dict[str,int]; top_patterns: list[tuple[str,int]]
    started_at; finished_at; .as_dict() -> dict
```

## `prospector/profiles.py`  (DONE — reference only)
```python
load_profile(name_or_path: str, profiles_dir: Path|None=None) -> Profile
list_profiles(profiles_dir: Path|None=None) -> list[str]
resolve_profiles_dir(profiles_dir: Path|None=None) -> Path
```

---

## AGENT A — `prospector/reddit_client.py`

A thin, polite client over Reddit's public `.json` endpoints, with optional OAuth.

```python
DEFAULT_USER_AGENT = "python:prospector:0.2 (personal research; +https://github.com/011-sam-110/Prospector)"

class RedditClient:
    def __init__(self,
                 user_agent: str = DEFAULT_USER_AGENT,
                 cache_dir: str | Path | None = None,   # default: ./.cache/reddit
                 cache_ttl: int = 3600,                  # seconds; 0 disables cache
                 client_id: str | None = None,           # falls back to env REDDIT_CLIENT_ID
                 client_secret: str | None = None,       # falls back to env REDDIT_CLIENT_SECRET
                 min_interval: float | None = None,      # min seconds between requests;
                                                         # default 6.0 unauth, 0.6 with OAuth
                 timeout: float = 20.0,
                 transport: str = "auto",                # auto | json | rss
                 rss_interval: float | None = None,      # min seconds between RSS requests (20)
                 log: Callable[[str], object] | None = None): ...  # default: stderr

    @property
    def authenticated(self) -> bool: ...   # True if OAuth creds present + token obtained
    @property
    def using_rss(self) -> bool: ...        # True once the client reads the RSS feeds
    @property
    def transport_in_use(self) -> str: ...  # "json" | "rss"

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """Core fetch. `path` is e.g. '/r/nursing/.json' or '/r/x/comments/abc.json'.
        Uses oauth.reddit.com when authenticated else www.reddit.com. Adds the
        User-Agent, honors 429 Retry-After with exponential backoff (cap ~60s,
        a few retries), and reads/writes an on-disk JSON cache keyed by
        (path, params) with TTL `cache_ttl`. Returns parsed JSON (dict)."""

    def listing(self, subreddit: str, sort: str = "new", limit: int = 100,
                time_filter: str = "year", pages: int = 1) -> list[dict]:
        """Return a flat list of raw post `data` dicts (the inner t3 data).
        `pages` follows the `after` token up to that many pages of 100."""

    def search(self, query: str, subreddit: str | None = None,
               sort: str = "relevance", time_filter: str = "year",
               limit: int = 100, restrict_sr: bool = True) -> list[dict]:
        """Return raw post `data` dicts from search.json (sub-restricted if
        `subreddit` given)."""

    def comments(self, post_id: str, limit: int = 100, depth: int = 2,
                 min_score: int = 0) -> list[dict]:
        """Fetch /r/<sub>/comments/<id>.json (or /comments/<id>.json). Return a
        FLAT list of raw comment `data` dicts (walk the tree up to `depth`,
        skip 'more' stubs, drop comments below min_score). `post_id` may be a
        fullname (t3_abc) or bare id (abc)."""

    def info(self, fullnames: list[str]) -> list[dict]:
        """Current `data` of up to 100 posts/comments (/api/info). Never cached.
        Reddit leaves out ids that no longer exist."""
```
Transport (added in 0.2): with `transport="auto"` the first `.json` request that
gets HTTP 403 (or a block page) switches the client to
`prospector.reddit_rss.RssTransport` for the rest of its life. The RSS
transport returns the same `data` dict shapes (with `score` and `num_comments`
always 0, because feeds carry neither). `RedditError` now lives in
`prospector/errors.py` and has `status` and `blocked` attributes;
`prospector.reddit_client.RedditError` is the same class.
Notes for A:
- OAuth: client-credentials grant against `https://www.reddit.com/api/v1/access_token`
  with HTTP Basic (client_id, client_secret), `grant_type=client_credentials`.
  Cache the token in memory; refresh on expiry. If creds absent → unauthenticated.
- Be defensive: Reddit returns `{"error": 429}` style bodies and HTML on blocks.
  Raise a clear `RedditError(Exception)` (define it here) on hard failures.
- Tests: stub `get_json` (monkeypatch) and assert `listing/search/comments` parse
  fixtures correctly. No real network in tests.

## AGENT B — `prospector/store.py`

SQLite persistence + dedup + querying + evidence resolution.

```python
class Store:
    def __init__(self, db_path: str | Path = "prospector.db"): ...
        # opens connection, creates schema if absent (items, matches, sweeps).

    def upsert_items(self, items: list[Item]) -> int:
        """Insert-or-replace by Item.id. Also replaces that item's rows in the
        `matches` table from Item.matches. Returns count upserted."""

    def get_item(self, item_id: str) -> Item | None: ...

    def query(self, profile: str | None = None, subreddit: str | None = None,
              kind: str | None = None, min_pain: float = 0.0,
              contains: str | None = None, since: int | None = None,
              sort: str = "pain", limit: int = 100) -> list[Item]:
        """Filter stored items. `contains` = case-insensitive substring over
        title+body. `since` = created_utc lower bound. sort in
        {'pain','score','new','comments'}. Returns hydrated Item objects
        (including their matches)."""

    def get_evidence(self, ids: list[str]) -> list[EvidenceItem]:
        """Resolve EvidenceItem (permalink + a trimmed verbatim quote, ~300
        chars from title/body) for each id, in the order given. Skip unknown ids."""

    def record_sweep(self, result: SweepResult) -> None: ...

    def stats(self, profile: str | None = None) -> dict:
        """Return {'total', 'posts', 'comments', 'subreddits': {sub:count},
        'top_patterns': [(pattern,count)...], 'date_range': (min_utc,max_utc)}."""

    def close(self) -> None: ...
```
Notes for B: schema per PRD §6 (`items`, `matches`, `sweeps`). Use `sqlite3` with
`check_same_thread=False`. Store `Item.matches` as rows in `matches(item_id,
pattern, weight)`. Tests use an in-memory or temp-file db.

## AGENT C — `prospector/scorer.py`

Deterministic weighted-lexicon scoring.

```python
import re
def compile_lexicon(rules: list[LexiconRule]) -> list[tuple[re.Pattern, float]]:
    """Compile each rule's regex with re.IGNORECASE. Skip invalid regex with a
    warning (don't crash the run)."""

def score_text(text: str, compiled: list[tuple[re.Pattern, float]]
               ) -> tuple[float, list[Match]]:
    """Sum the weight of every rule that finds a match in `text` (each rule
    counts at most once). Return (total_score, [Match(pattern, weight) ...])."""

def score_item(item: Item, compiled) -> Item:
    """Score item.text, set item.pain_score and item.matches, return the item."""
```
Notes for C: pattern string stored in Match is the rule's raw pattern. Pure +
fully unit-tested (give text, assert score + matches).

## AGENT D — `prospector/scrape.py`

The two-stage pipeline tying client + scorer + store together.

```python
def sweep(profile: Profile, client: RedditClient, store: Store,
          run_id: str | None = None, now: int | None = None,
          time_window: str | None = None, listing_limit: int | None = None,
          max_threads: int | None = None,
          log=print,
          combine_terms: bool = False,              # added in 0.2
          rss_comment_threads: int | None = None,   # added in 0.2
          ) -> SweepResult:
    """
    Stage 1 (broad/cheap): for each sub in profile.subreddits, pull listing()
      (limit=listing_limit or profile.listing_limit) AND search() for each
      profile.search_terms. Dedup by id. Build Item via Item.from_reddit(...,
      kind='post', profile=profile.name, fetched_at=now). Score every post with
      the compiled lexicon. upsert into store.
    Stage 2 (targeted/deep): select posts with pain_score >= profile.pain_threshold
      OR high num_comments; take up to (max_threads or profile.max_threads),
      ordered by pain_score desc. For each, client.comments(...) bounded by
      profile.comments. Score comments, upsert.
    RSS transport (client.using_rss, added in 0.2): feeds carry no comment
      counts, so stage 2 reads at least (rss_comment_threads or
      profile.rss_comment_threads) threads: the threshold candidates first, then
      the next posts by pain_score desc, created_utc desc. The .json path is
      unchanged.
    Record + return a SweepResult (counts, per-sub tallies, top matched patterns).
    `run_id` defaults to uuid4().hex; `now` to int(time.time()). Be resilient:
    one sub failing must not abort the sweep (catch, log, continue).
    """
```
Notes for D: import RedditClient/Store/scorer by their interfaces above. Tests use
a fake client (returns canned `data` dicts) + temp Store; assert two-stage counts.

## AGENT E — `prospector/report.py` + `prospector/analyze.py`

Evidence-bound rendering. **This is the trust centerpiece — enforce the contract.**

`report.py`:
```python
@dataclass
class Cluster:
    label: str                 # human label (dominant matched theme)
    item_ids: list[str]
    evidence: list[EvidenceItem]
    subreddits: set[str]
    authors: set[str]
    aggregate_pain: float
    confidence: str            # 'low'|'medium'|'high'

def build_clusters(store: Store, profile: Profile, limit: int = 12) -> list[Cluster]:
    """Group high-pain items into candidate gaps. v1 grouping: bucket items by
    their dominant matched pattern (or a shared keyword), gather evidence via
    store.get_evidence, compute subreddit/author breadth + aggregate pain.
    ONLY keep clusters that satisfy evidence_ok(...). Sort by confidence then
    aggregate_pain."""

def evidence_ok(cluster: "Cluster | dict", thresholds: EvidenceThresholds) -> bool:
    """True iff distinct items >= min_items AND distinct subreddits >=
    min_subreddits AND distinct authors >= min_authors. The renderer MUST drop
    anything failing this."""

def render_report(profile: Profile, store: Store, *, analyze: bool = False,
                  model: str = "auto", generated: str | None = None) -> str:
    """Return a Markdown report. Default (analyze=False): stats-only clusters
    from build_clusters, each with confidence, breadth (N items / M subs / K
    authors), and verbatim quotes w/ permalinks (all evidence for small
    clusters; capped ~8). With analyze=True: call
    analyze.synthesize(clusters, profile, model) to add a one-paragraph thesis
    per gap — but STILL pass every gap through evidence_ok and only cite the
    evidence objects provided (no invented links). Always include the standing
    disclaimer: results are HYPOTHESES TO VALIDATE, not validated needs; no
    clinical/market claims. Footer notes generated timestamp if given."""
```
`analyze.py`:
```python
def available() -> bool:
    """True if an OpenAI-compatible endpoint is configured (env
    FREELLMAPI_BASE_URL or OPENAI_BASE_URL, plus FREELLMAPI_KEY/OPENAI_API_KEY)."""

def synthesize(clusters: list, profile: Profile, model: str = "auto"
               ) -> dict[str, str]:
    """For each cluster, ask the LLM for a 1-paragraph 'why this is an
    underserved gap' thesis, CONSTRAINED to the quotes provided (instruct it to
    not introduce facts beyond the evidence). Return {cluster.label: thesis}.
    Degrade gracefully: if not available(), return {} (report falls back to
    stats-only). Uses the freellmapi.co OpenAI-compatible gateway: base_url +
    key from env, model defaults to 'auto'. Use the `openai` package if present,
    else httpx POST to {base_url}/chat/completions."""
```
Notes for E: keep clustering simple but real; the contract enforcement is the
point. Tests: feed a temp Store with crafted items; assert under-evidenced
clusters are dropped and well-evidenced ones render with permalinks. analyze
tests must run offline (monkeypatch the LLM call / available()->False path).

## `prospector/reddit_rss.py` (added in 0.2)

```python
RSS_BASE = "https://www.reddit.com"        # old.reddit.com sends .rss to a login wall
class RssTransport:
    def __init__(self, user_agent: str, min_interval: float = 20.0,
                 timeout: float = 30.0, http=None, log=None): ...
    requests_made: int
    def get_feed(self, path: str, params: dict | None = None) -> list[dict]
    def listing(...) / search(...) / comments(...)   # same signatures as RedditClient
    def info(self, fullnames) -> list[dict]          # /api/info.rss, max 100 ids
def parse_feed(xml_bytes: bytes, path: str = "") -> list[dict]
```
Pacing: at least `min_interval` between requests, and when a response says
`x-ratelimit-remaining` < 1 the next request waits for `x-ratelimit-reset` + 1 s.
Measured on 2026-10-04 from a home IP: one request per clock minute.

## `prospector/prune.py` (added in 0.2)

```python
prune(conn, max_age_days=7.0, now=None, client=None, skip_fresh_hours=12.0,
      log=print) -> PruneResult
delete_items(conn, ids) -> (items_deleted, vectors_deleted)  # items, matches, vectors
purge_cache(cache_dir, max_age_hours=24.0, now=None) -> int
is_gone(title, body, author) -> bool        # [deleted] / [removed] / deleted author
```
Deletes items with `created_utc` older than `max_age_days`, items that read as
deleted or removed, and (with a `client`) items Reddit returns as deleted or
leaves out of a non-empty `/api/info` answer. An empty answer deletes nothing.

## `prospector/semantic.py` (added in 0.2, `[semantic]` extra)

```python
MODEL_NAME = "BAAI/bge-small-en-v1.5"; DIM = 384
CONTRACT_KEYS = ("permalink", "subreddit", "title", "quote", "score", "created_utc")
embed_pending(conn, embedder, batch_size=64, limit=None, now=None, log=...) -> EmbedStats
search(conn, embedder, query, limit=10, subreddit=None, since=None, kind=None) -> list[dict]
contract_view(hits) -> list[dict]           # exactly CONTRACT_KEYS per hit
clusters(conn, k=None, subreddit=None, profile=None, since=None, examples=3) -> list[dict]
default_embedder() -> Embedder              # lazy FastEmbedder (model loads on first use)
```
Tables: `item_vectors` (vec0: `item_id TEXT PRIMARY KEY`, `embedding float[384]`
cosine, `subreddit`, `kind`, `created_utc`) and `item_embeddings` (model, text
hash, time). A hit `score` is the cosine similarity (higher is closer), not the
Reddit vote score.

## AGENT F — `prospector/cli.py`

A `typer` app named `app`. Commands (all import the engine modules above):
```
prospector profiles
prospector sweep   PROFILE [--time year] [--limit 100] [--max-threads 60]
                           [--db prospector.db] [--oauth]
prospector query   PROFILE [--min-pain 3] [--sub nursing] [--contains fax]
                           [--sort pain] [--limit 50] [--db ...]
prospector report  PROFILE [--analyze] [--out PATH] [--db ...]
prospector export  PROFILE --format json|csv|md [--out PATH] [--db ...]
prospector mcp                      # exec the MCP server (calls mcp_server.main())
# added in 0.2
prospector sweep   PROFILE ... [--transport auto|json|rss] [--combine-terms]
                           [--rss-comment-threads N]
prospector embed   [--db ...] [--batch-size 64] [--limit N]
prospector semantic-search "QUERY" [--db ...] [--limit 10] [--sub X]
                           [--since-days D] [--json]
prospector clusters [--db ...] [--sub X] [--profile P] [--since-days D]
                           [--k 0] [--examples 3] [--json]
prospector prune   [--db ...] [--max-age-days 7] [--recheck/--no-recheck]
                           [--skip-fresh-hours 12] [--transport auto]
                           [--cache-dir PATH] [--cache-max-age-hours 24] [--json]
```
CONTRACT (another tool codes against it, keep it exact):
`prospector semantic-search "QUERY" --db PATH --limit N --json` prints one JSON
array to stdout. Each element has exactly the keys `permalink`, `subreddit`,
`title`, `quote`, `score`, `created_utc`. `score` is the cosine similarity
(float, higher is closer). `title` is a string (a comment shows its thread
title, or "" when that post is not stored). The command exits 0 and prints `[]`
when the store is empty or the file does not exist (it never creates the file).
Each command also prints a final `key=value` count line (`fetched=`,
`embedded=`, `pruned=`, `searched=`) for scripts.
Behavior: `sweep` builds RedditClient (OAuth auto if env creds present; `--oauth`
forces requiring them), Store, loads profile, calls scrape.sweep, prints the
SweepResult summary. `query` prints a compact table. `report` writes/echoes the
Markdown. `export` dumps store.query results. `mcp` calls
`prospector.mcp_server.main()`. Keep output readable (use typer/echo, no heavy
deps). `app` must be importable as `prospector.cli:app`. Provide a `main()` too.
Tests: use typer.testing.CliRunner against `profiles` and a `sweep` with a fake
client (monkeypatch RedditClient) — offline.

## AGENT G — `prospector/mcp_server.py`

A `fastmcp` server exposing the engine. Provide module-level `mcp` (FastMCP
instance) and `def main(): mcp.run()` (stdio). A shared Store/RedditClient is
created lazily (db path from env `PROSPECTOR_DB`, default 'prospector.db').

Tools (names EXACT — this is the contract Claude relies on):
```
reddit_profiles() -> list[str]
reddit_profile_get(name: str) -> dict                      # the profile config
reddit_sweep(profile: str, time: str = "", limit: int = 0,
             max_threads: int = 0) -> dict                 # SweepResult.as_dict()
reddit_search(query: str, subreddits: list[str] = [], sort: str = "relevance",
              time: str = "year", limit: int = 50) -> list[dict]   # scored, stored
reddit_fetch_thread(post_id: str, max_comments: int = 40,
                    min_score: int = 0) -> dict            # post + scored comments
reddit_query(profile: str = "", subreddit: str = "", min_pain: float = 0.0,
             contains: str = "", sort: str = "pain", limit: int = 50) -> list[dict]
reddit_get_evidence(ids: list[str]) -> list[dict]         # EvidenceItem dicts
reddit_stats(profile: str = "") -> dict
reddit_export(format: str = "json", profile: str = "", min_pain: float = 0.0,
              limit: int = 500) -> str                     # serialized payload / path
# added in 0.2 (need the [semantic] extra; no network)
reddit_semantic_search(query: str, limit: int = 10, subreddit: str = "",
                       since_days: float = 0.0) -> list[dict]  # hits with permalink + quote
reddit_clusters(subreddit: str = "", profile: str = "", since_days: float = 0.0,
                k: int = 0, examples: int = 3) -> list[dict]   # examples with permalink + quote
```
Each tool wraps the engine functions; serialize dataclasses to plain dicts.
`reddit_search`/`reddit_fetch_thread` hit Reddit live (and store results) so
Claude can drill; the rest read the store. Add concise docstrings (Claude reads
them). Tests: import the module, assert tools registered + that a stubbed engine
flows through (offline).
```
