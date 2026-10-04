<h1 align="center">prospector</h1>
<p align="center">Mine Reddit for unmet needs — a plug-and-play topic miner that doubles as an MCP server so Claude can do Reddit research.</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img src="https://img.shields.io/badge/MCP-11%20tools-8A2BE2">
</p>

```text
$ prospector profiles
Available profiles:
  - hospital-tech
  - saas-pain

$ prospector sweep hospital-tech        # two-stage scrape → scored SQLite store
$ prospector report hospital-tech       # evidence-bound Markdown report
$ prospector embed                      # vectors for new items ([semantic] extra)
$ prospector semantic-search "is there a tool that tracks this" --limit 5
```

`prospector` pulls Reddit content through the public `.json` endpoints, scores every
post and comment against a per-topic "pain" lexicon, stores the lot in SQLite, and renders
a report of recurring **unmet needs** — each one backed by real permalinks and verbatim
quotes. Point it at any niche by dropping in a YAML profile; the flagship profile hunts for
**a piece of tech missing from hospitals** that frontline staff wish existed. The same engine
runs as an **MCP server**, turning Claude into a Reddit research specialist that collects once
and reasons over the store many times.

The engine is deterministic plumbing — **no LLM is required** to scrape, score, or store.
Insight is the client's job: Claude via MCP, or an optional built-in `--analyze` report.

## ✨ Features
- **Reddit `.json` client** — listings, in-sub search, and comment trees; descriptive User-Agent, `429` Retry-After backoff, on-disk response cache. Optional free OAuth (env vars) lifts the rate limit ~10×.
- **RSS fallback**: when `.json` returns HTTP 403, the client reads the public Atom feeds on `www.reddit.com` for the rest of the run. It keeps at least 20 seconds between requests and obeys the `x-ratelimit-*` headers. On 2026-10-04 the measured budget from a home IP was one request per minute. Feeds carry no vote score and no comment count, so those fields are 0 on this path.
- **Semantic search and clusters** (optional `[semantic]` extra): `BAAI/bge-small-en-v1.5` vectors (fastembed, CPU) in a `sqlite-vec` table in the same SQLite file. `semantic-search` finds posts and comments by meaning. `clusters` groups items by meaning and shows each group's size, keywords and nearest quotes. Every hit and every example is a stored item with its permalink and a verbatim quote.
- **Retention and deletion**: `prune` deletes items posted more than 7 days ago (configurable), items that now read `[deleted]` or `[removed]`, and items that Reddit returns as deleted or no longer has (`/api/info`, 100 ids per request). Their lexicon matches and vectors go too, and old response-cache files are purged.
- **Two-stage scrape** — a broad, cheap post sweep, then comment trees fetched *only* for threads that clear the pain threshold or run hot. Spends the rate-limit budget where the signal is.
- **Deterministic pain scorer** — a per-profile weighted-regex lexicon gives every item a transparent `pain_score` plus the exact patterns that fired. No model, fully reproducible.
- **Evidence-bound reports** — a "gap" is *structurally dropped* unless it clears the profile's thresholds (≥N distinct items, across ≥M subreddits, from ≥K authors), each with a stored permalink + quote. The renderer cannot emit an unbacked claim.
- **Plug-and-play profiles** — a topic is one YAML file (`subreddits`, `search_terms`, `pain_lexicon`, thresholds). Swap the niche with zero code changes.
- **MCP server (FastMCP)** — 11 tools (`reddit_sweep`, `reddit_search`, `reddit_fetch_thread`, `reddit_query`, `reddit_get_evidence`, `reddit_stats`, `reddit_export`, `reddit_profiles`, `reddit_profile_get`, `reddit_semantic_search`, `reddit_clusters`) so Claude can drive the whole loop.
- **Optional standalone analysis** — `report --analyze` adds a one-paragraph thesis per gap via any OpenAI-compatible endpoint, constrained to the fetched evidence. Degrades to stats-only if no key is set.

## 🛠 Stack
Python · httpx · Typer · SQLite · PyYAML · FastMCP · (optional) fastembed + sqlite-vec · (optional) any OpenAI-compatible LLM

## 🚀 Run
```bash
pipx install prospector-reddit          # or: uvx prospector-reddit ...
# from source:
pip install -e ".[dev,analyze]"

prospector profiles                     # list topic profiles
prospector sweep hospital-tech          # collect + score + store
prospector query hospital-tech --min-pain 4 --sort pain
prospector report hospital-tech --out reports/hospital.md
prospector report hospital-tech --analyze   # + LLM thesis (needs an LLM endpoint)

# search by meaning (optional extra; the model downloads once, about 67 MB)
pip install -e ".[semantic]"
prospector sweep saas-pain --time week --combine-terms   # one OR search per sub
prospector embed                                         # vectors for new/changed items
prospector semantic-search "is there a tool that tracks this" --limit 5
prospector semantic-search "QUERY" --db PATH --limit 3 --json   # stable JSON contract
prospector clusters --sub selfhosted --examples 2
prospector prune --max-age-days 7                       # age + deleted-on-Reddit
```

`semantic-search --json` prints a JSON array. Each hit has exactly the keys
`permalink`, `subreddit`, `title`, `quote`, `score` and `created_utc`. `score` is the
cosine similarity to the query (higher is closer), not the Reddit vote score. An empty
or missing store prints `[]` and exits 0.

### Daily run on a server
`deploy/prospector-daily.sh` runs `sweep` for each profile, then `embed`, then `prune`,
then one search check, and prints `daily: fetched=N embedded=N pruned=N searched=N`. The
prune step runs even when a sweep fails. `deploy/install-systemd.sh` installs it as the
user units `prospector-sweep.service` and `prospector-sweep.timer` (daily at 09:20, with
`MemoryMax=2G`). Keep the store outside the checkout (default `~/prospector-data`).
Machine-local settings, such as `PROSPECTOR_SWEEP_PROFILES`, go in
`~/prospector-data/prospector.env`.

Higher throughput (optional, free): create a Reddit "script" app and export
`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` before sweeping — the client switches to
OAuth (100 req/min). LLM analysis reads `FREELLMAPI_BASE_URL`+`FREELLMAPI_KEY` (or the
`OPENAI_*` equivalents).

### Use it from Claude (MCP)
Register the server in your MCP client (`.mcp.json`):
```json
{ "mcpServers": { "prospector": { "command": "prospector", "args": ["mcp"] } } }
```
Then Claude can `reddit_sweep` a profile, `reddit_query` the store, drill hot threads with
`reddit_fetch_thread`, and resolve citations with `reddit_get_evidence` — collect once,
reason many.

## 🧠 How it works
```
 profiles/*.yaml ─┐
                  ▼
   RedditClient ──► two-stage scrape ──► lexicon scorer ──► SQLite store
   (.json/OAuth)      posts→comments        pain_score          │
                                                                ▼
                              evidence-bound renderer ◄── Claude (MCP)  or  --analyze
                              (drops under-evidenced gaps)
```
The core engine never invents anything — it only surfaces what it actually fetched, and the
report renderer enforces the evidence contract, so every claimed gap is traceable to real
Reddit permalinks and quotes.

## 🗺 Roadmap
Code complete and verified locally — **145 offline unit tests pass**, all modules import, the CLI and
the full two-stage sweep run end to end, and all 11 MCP tools register. Built with a frozen
interface contract (`INTERFACES.md`) so the modules integrate cleanly.

- **Known limitation — Reddit blocks datacenter/VPN IPs.** Unauthenticated `.json` (and even
  OAuth) returns `403` from VPN/hosting-provider IP ranges. On 2026-10-04 `.json` also returned
  `403` from a home IP, while the RSS feeds returned `200`. The default `auto` transport then
  uses RSS. The RSS budget is small (about one request per minute), so a daily sweep of three
  profiles takes hours.
- **Known limitation: Reddit Data API terms.** Reddit says traffic without OAuth can be
  blocked at any time, and it asks builders to register under its Responsible Builder Policy.
  This tool is for personal, non-commercial research. It uses a pretrained embedding model and
  trains nothing on Reddit content. Keep the store private: never commit it.
- **Known limitation — results are *hypotheses to validate*, not validated needs.** Reddit is not
  ground truth and venting is not a market; the medical profile makes **no clinical claim**.
- [ ] Generate a real flagship `hospital-tech` report (pending a live sweep from a clean IP).
- [x] Semantic search and clusters over stored posts and comments (`[semantic]` extra).
- [ ] Trend deltas — surface gaps that are *rising* over time.

## 📄 License
MIT — see [LICENSE](LICENSE). Read-only and non-commercial by design; respects Reddit's terms,
no bulk-data redistribution.
