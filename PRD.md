# prospector — Product Requirements Document

> **One-liner:** An open-source, plug-and-play engine that mines Reddit (via the public `.json` endpoints) for unmet needs — and exposes itself as an **MCP server so Claude becomes a Reddit research specialist**. Flagship demo: surface underserved hospital/medical tech gaps, each backed by real, traceable evidence.

- **Name:** `prospector` (metaphor: panning Reddit for gold — i.e. unmet needs)
- **Author:** Sampo (`011-sam-110`)
- **License:** MIT · **Distribution:** PyPI / `uvx` / `pipx` · README per `_shared/README-DESIGN.md`
- **Status:** PRD draft — 2026-06-26
- **Stack:** Python · `httpx` · `fastmcp` · `typer` · `sqlite3` · `pyyaml`

---

## 1. Problem & motivation

Frontline workers vent on Reddit about broken workflows, missing tools, and "I wish there was a…" gaps far more candidly than in any survey or sales call. That raw, unfiltered signal is a goldmine for finding **a genuinely underserved niche** — but it's buried across hundreds of subreddits and thousands of comment threads, and naive scraping/summarizing produces *plausible-sounding fiction* rather than defensible opportunities.

`prospector` turns that signal into **evidence-bound, reproducible opportunity hypotheses**. The first target niche: **a piece of tech missing from hospitals that could be useful.** But the engine is topic-agnostic — the hospital hunt is just profile #1.

### Why this works (feasibility, honestly stated)
- **Reddit `.json` is real and free.** Appending `.json` to any URL returns structured data (`/r/X/.json`, `/r/X/search.json?q=…`, `/r/X/comments/ID.json`). No API wrapper required.
- **Caveats we design around:** unauthenticated is ~10 req/min and needs a descriptive `User-Agent`; cloud IPs get flaky; Reddit search depth is shallow (~250–1000/query). Mitigations: optional free OAuth (→100 req/min), polite backoff + on-disk cache, and **many targeted subreddit queries** instead of one broad search.
- **The hard 70% is signal extraction, not scraping.** That's why the trust contract (§8) is the centerpiece.

---

## 2. Goals / Non-goals

### Goals (v1)
1. A **deterministic core engine** — fetch → store → score → dedup → export — with **no mandatory LLM dependency**.
2. **Two interfaces over one engine:** a CLI (standalone/OSS) and an **MCP server** (so Claude drives the research loop).
3. **Plug-and-play profiles:** point at any niche by editing a YAML file — zero code changes.
4. An **evidence-bound output contract** so every surfaced "gap" is traceable to real Reddit permalinks + quotes.
5. Ship the **flagship `hospital-tech` profile** + one example second profile to prove reusability.
6. Optional **standalone `--analyze`** report via `freellmapi`/Groq (insight without Claude in the loop).

### Non-goals (v1)
- ❌ No GUI / web dashboard (CLI + MCP + Markdown reports only).
- ❌ No scheduling/daemon (one-shot runs; cron is the user's job).
- ❌ No platforms beyond Reddit (no HN/Twitter/forums — clean seam for later).
- ❌ No posting/writing to Reddit ever (read-only).
- ❌ Not a medical-validity oracle — output is **hypotheses to validate**, never clinical or market conclusions.
- ❌ No bulk-data redistribution (respects Reddit ToS; non-commercial research framing).
- ❌ English-only lexicon in v1.

---

## 3. Users & use cases

| User | How they use it |
|---|---|
| **Sampo (flagship)** | `prospector sweep hospital-tech` → drive Claude via MCP → get ranked, evidence-backed hospital tech-gap hypotheses. |
| **Claude (via MCP)** | Becomes a Reddit research specialist: sweep → query store → drill hot threads → pull evidence → write the contract-bound report. |
| **OSS user, no Claude** | `prospector sweep <profile> && prospector report <profile> --analyze` → standalone Markdown report via freellmapi. |
| **OSS user, own pipeline** | Uses the engine as a library / `export` to JSON/CSV and analyzes themselves. |

---

## 4. Architecture

```
                         ┌─────────────────────────────┐
                         │      CORE ENGINE (hands)     │   ← no mandatory LLM
                         │  reddit client (.json/OAuth) │
                         │  two-stage scraper           │
                         │  lexicon pain-scorer         │
                         │  SQLite store + dedup        │
                         │  evidence-bound renderer     │
                         │  profile loader (YAML)       │
                         └───────────┬─────────┬────────┘
                  ┌──────────────────┘         └──────────────────┐
        ┌─────────▼──────────┐               ┌────────────────────▼─────────┐
        │  CLI  (typer)      │               │  MCP server (fastmcp)        │
        │  sweep/query/      │               │  8 tools → Claude is BRAIN   │
        │  report/export/mcp │               │  collect-once, reason-many   │
        │  + optional        │               └──────────────────────────────┘
        │  --analyze (freellmapi)
        └────────────────────┘
```

**Principle:** the engine is deterministic plumbing. *Intelligence lives in the client* — Claude via MCP (primary), or the optional `--analyze` LLM path (standalone). The engine never invents; it only fetches, scores, stores, and renders what it actually pulled.

---

## 5. Reddit access layer

- **Default: unauthenticated `.json`** — zero config, ~10 req/min, polite throttle. Anyone can clone-and-run instantly.
- **Opt-in OAuth** — drop `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` env vars (free Reddit "script" app) → 100 req/min. Used for deep hospital sweeps.
- **Always:** descriptive `User-Agent` (`prospector/<ver> (+github.com/011-sam-110/prospector)`), honor `429 Retry-After` with exponential backoff, and an **on-disk response cache** (keyed by URL+params, TTL configurable) so re-runs and drilling don't re-hit Reddit.
- **Pagination** via `after` token, `limit=100`/page; per-sweep caps to bound request budget.

---

## 6. Data model (SQLite)

```sql
items(
  id          TEXT PRIMARY KEY,   -- reddit fullname: t3_xxx (post) / t1_xxx (comment)
  kind        TEXT,               -- 'post' | 'comment'
  subreddit   TEXT,
  author      TEXT,
  created_utc INTEGER,
  title       TEXT,               -- posts only
  body        TEXT,               -- selftext / comment body
  score       INTEGER,
  num_comments INTEGER,           -- posts only
  permalink   TEXT,               -- absolute reddit URL (the evidence anchor)
  link_id     TEXT,               -- parent post for comments
  parent_id   TEXT,
  pain_score  REAL,               -- from lexicon scorer
  fetched_at  INTEGER,
  profile     TEXT                -- which profile collected it
);
matches(item_id TEXT, pattern TEXT, weight REAL);   -- explainability: WHY it scored
sweeps(run_id TEXT, profile TEXT, started_at INT, finished_at INT, params JSON, stats JSON);
```
Dedup on `id` (upsert; keep latest score). Reports are written to `reports/`, not the DB.

---

## 7. Profile schema (the plug-and-play unit)

`profiles/<name>.yaml`:
```yaml
name: hospital-tech
description: Find tech missing from hospitals that frontline staff wish existed.
subreddits: [nursing, medicine, hospitalist, healthIT, BMET, medicaldevices,
             respiratorytherapy, emergencymedicine, Residency, CRNA,
             nursepractitioner, medlabprofessionals, Paramedics, ems]
search_terms: ["i wish there was", "still fax", "no system to", "workaround",
               "manual", "double charting", "why do we still"]
time_window: year            # hour|day|week|month|year|all
listing_limit: 100           # posts per sub in stage 1
max_threads: 60              # stage-2 comment-tree budget
pain_lexicon:                # deterministic weighted scoring
  - {pattern: "i wish( there was| we had)?",            w: 3}
  - {pattern: "still (fax|paper|on paper|by hand)",     w: 3}
  - {pattern: "no (way|tool|app|system|software) to",   w: 2}
  - {pattern: "why (is there no|isn'?t there|do we still)", w: 2}
  - {pattern: "(work[- ]?around|double (entry|charting))", w: 2}
  - {pattern: "(clunky|outdated|broken|takes forever)", w: 1}
  - {pattern: "manual(ly)?",                            w: 1}
pain_threshold: 3            # gates stage-2 deep fetch + ranking eligibility
evidence:                    # the trust contract thresholds
  min_items: 5
  min_subreddits: 3
  min_authors: 3
comments: {max_per_thread: 40, min_score: 2, depth: 2}
```
**Reuse test (success metric):** a second niche runs by dropping in a new YAML — no code change.

---

## 8. Trust: evidence-bound output contract  ⭐ centerpiece

The thing that makes this credible rather than confident fiction.

- **A "gap" may not be emitted** unless it cites **≥ `min_items` distinct items**, across **≥ `min_subreddits` subreddits**, from **≥ `min_authors` distinct authors** — each with a stored `{id, permalink, verbatim quote}`.
- The **renderer enforces this and drops under-evidenced gaps.** Not advisory — structural.
- **Claude cannot fabricate a permalink** because the MCP only ever returns IDs the engine actually fetched (`reddit_get_evidence(ids)` resolves from the DB).
- **Confidence** = `f(evidence breadth, recency, aggregate pain_score, author diversity)` — derived, not vibes.
- Every report carries a standing disclaimer: **these are hypotheses to validate, not validated needs** (Reddit ≠ ground truth; complaints ≠ a market; medical claims need real clinical/regulatory validation).

---

## 9. Pipeline (two-stage scrape)

1. **Stage 1 — broad & cheap.** Sweep each profile subreddit's listings + `search.json` for `search_terms`. Pull **posts only** (title + selftext). Score every item with the lexicon → `pain_score`. Store all; record `matches` for explainability.
2. **Stage 2 — targeted & deep.** For threads that **pass `pain_threshold`** *or* are high-engagement, fetch the comment tree (bounded by `comments.*`). Score and store comments. This spends the rate-limit budget only where the gold is.
3. **Dedup & persist** into SQLite; write a `sweeps` run record with stats.

---

## 10. Interfaces

### 10a. CLI (`typer`)
```
prospector profiles                         # list available profiles
prospector sweep   <profile> [--time month] [--limit 100] [--max-threads 60] [--oauth]
prospector query   <profile> [--min-pain 3] [--sub nursing] [--contains "fax"] [--sort pain]
prospector report  <profile> [--analyze] [--out reports/<profile>-<date>.md]
prospector export  <profile> --format json|csv|md
prospector mcp                              # run the MCP server over stdio
```
`report` without `--analyze` → **stats-only** evidence clusters (zero LLM). With `--analyze` → freellmapi/Groq synthesizes gap narratives **constrained to the evidence the engine provides** (same contract as §8).

### 10b. MCP tools (`fastmcp`) — collect-once, reason-many
| Tool | Purpose |
|---|---|
| `reddit_profiles()` | list profiles |
| `reddit_profile_get(name)` | inspect a profile's config |
| `reddit_sweep(profile, time?, limit?, max_threads?)` | **heavy:** run the two-stage collection into the store → run summary |
| `reddit_search(query, subreddits?, sort?, time?, limit?)` | **live:** targeted ad-hoc search (also stored + scored) |
| `reddit_fetch_thread(post_id, max_comments?, min_score?)` | **live:** drill one thread's comment tree |
| `reddit_query(filters)` | **store:** filter collected items (sub, min_pain, since, contains, sort) |
| `reddit_get_evidence(ids)` | **store:** resolve `{id, permalink, quote, sub, author, score}` for the report |
| `reddit_stats(profile?)` / `reddit_export(format, filters?)` | corpus stats / dump to json·csv·md |

**Claude's loop:** `sweep → query → drill hot threads → get_evidence → write contract-bound report`.

---

## 11. Flagship profile output (report format)

`reports/hospital-tech-<date>.md`, per gap:
```
### Gap 1: <thesis in one line>
Confidence: ●●●○○ (medium)   Evidence: 11 items · 5 subreddits · 9 authors
Why underserved: <one-paragraph synthesis, claims only from evidence below>
Evidence:
  - "we still fax discharge summaries to the SNF, it's 2026" — r/nursing, u/…, ↑142  [permalink]
  - "no way to see which pump is alarming without walking the floor" — r/BMET, …  [permalink]
  ...
```
Plus an appendix of raw ranked clusters (frequency · aggregate pain_score · subs).

---

## 12. Open-source packaging
- **Repo:** `011-sam-110/prospector` (public, MIT).
- **Install:** `uvx prospector …` / `pipx install prospector` / `pip install prospector`.
- **README** per `_shared/README-DESIGN.md` (hero, visual-led, honest — "accurate not humble"). Includes a real screenshot of an MCP-driven session and a sample hospital report.
- **MCP setup** doc: how to register `prospector mcp` in Claude Code's `.mcp.json`.
- Honest **limitations/ethics** section (rate limits, Reddit ToS, "hypotheses not conclusions", no medical claims).

---

## 13. Milestones
| # | Deliverable |
|---|---|
| **M0** | Repo scaffold, `pyproject.toml`, profile loader, SQLite store |
| **M1** | Reddit client: `.json` + optional OAuth, UA, 429 backoff, disk cache |
| **M2** | Two-stage scraper + weighted-lexicon scorer + dedup |
| **M3** | CLI: `sweep` / `query` / `export` |
| **M4** | Evidence-bound renderer (`report`) + optional `--analyze` (freellmapi) |
| **M5** | MCP server (8 tools) over the engine |
| **M6** | `hospital-tech` + one example profile; README; publish to GitHub + PyPI |

---

## 14. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Rate limits / IP blocks | unauth-polite default + opt-in OAuth; backoff; disk cache; honest README note |
| Lexicon misses paraphrases | tunable per-profile lexicon; **v1.1 optional embedding rerank** |
| **Hallucinated gaps** | evidence-bound contract (structural, renderer-enforced) |
| Selection/survivorship bias (Reddit ≠ reality) | frame output as **hypotheses to validate**; show evidence breadth + confidence |
| Small-sample over-claiming | min-evidence thresholds; confidence derived from breadth |
| Medical-domain overreach | explicit "not clinical/market advice; needs real validation" disclaimer |
| Reddit ToS | read-only, non-commercial research, no bulk redistribution — stated in README |

---

## 15. Success metrics
- ✅ One OAuth sweep of `hospital-tech` stores several hundred scored items within rate limits, no manual babysitting.
- ✅ **100%** of report gaps satisfy the evidence threshold (renderer guarantees it).
- ✅ A **second profile** runs with zero code changes (YAML only).
- ✅ Claude completes `sweep → report` end-to-end via MCP in a single session.
- ✅ Flagship run yields **≥3 distinct, evidence-backed hospital tech-gap hypotheses**.

---

## 16. Future (v1.1+)
Embedding/semantic rerank · more domain profiles · trend/recency deltas (gap rising over time) · cross-subreddit author-diversity weighting · HN/forum adapters behind the same profile seam · optional gap-validation helper (search for existing products solving each gap).
