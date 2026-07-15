# CLAUDE.md — Micro-Cap Gov Contract Monitor (Investor / Analyst Track)

_Last updated: 2026-07-15_

## What this project is
An agent that watches U.S. federal contract activity for a **curated watchlist of
publicly traded small- and micro-cap companies** and, when a company wins a
material award, produces **two scores** plus a written analysis:

1. **Catalyst score (1–10)** — how likely is this award to *significantly move the
   stock*? Driven by materiality (award size vs. market cap and revenue), new award
   vs. recompete, sole-source/OTA vs. competed, float/liquidity, and an
   "already priced in?" check (was it 8-K'd / expected).
2. **Value score (1–10)** — the underlying business through a **Warren Buffett /
   Charlie Munger / Mohnish Pabrai** value-investing lens: quality + margin of safety.

The thesis: public procurement data (USASpending, SEC EDGAR, SAM.gov) surfaces
material contract wins before the broader market prices them in, creating tradeable
signals for smaller defense / GovIT / space / drone / AI companies. The catalyst
score says "this just happened and it matters"; the value score says "and here's
whether the company is any good."

### This is one of two separate tracks — keep them apart
- **This repo (investor track):** trading signals from contract awards. GitHub repo
  `micro-cap-gov-contract-monitor` (renamed from `Gov_award_monitor`), deployed on Render.
- **Production-company track (separate repo `AFP-Sub-tracker`):** AFP bidding on
  federal *media/video* contracts. **Do not pull that track's logic, NAICS codes,
  or config into here.** They are intentionally separate to keep context clean.

## Division of labor
- **Claude Code (this environment) owns all technical execution** — writing,
  running, testing, and deploying code here.
- The strategy / research / watchlist decisions come from Jordan (and his Claude.ai
  investor Project). When strategy is genuinely unspecified, ask; do **not** re-ask
  things this file or the conversation already answers.

## Current state → target
- `monitor.py` today: rule-based. Deterministic pipeline that, per watchlist company,
  fetches awards/filings (`fetch_usaspending_awards`, `fetch_sam_awards`,
  `fetch_edgar_filings`), keyword-filters, and scores with a fixed formula
  (`score_award`, ~line 93), then emails an HTML digest.
- **Target: replace the rule-based `score_award` with an LLM agentic scoring step**
  (Claude Messages API + `tool_use`) that returns structured JSON with **both**
  scores, rationale, confidence, and sources checked. Everything *around* the scorer
  stays deterministic.

## Pipeline (target architecture)
1. **Ingest (deterministic, keep as-is):** for each watchlist company, pull recent
   awards/filings from **USASpending + EDGAR** (primary) and **SAM.gov** (secondary —
   see auth wall below). De-dup against `data/seen_awards.json`.
2. **Enrich (new):** for a company with a fresh award, fetch fundamentals from
   **EDGAR `companyfacts`** (by CIK/ticker — already known from the watchlist) and a
   price/market-cap quote. Compute the metric set below.
3. **Agentic scoring (new, bounded):** call Claude with `tool_use` tools
   (`fetch_fundamentals`, `fetch_quote`, `fetch_filings`) and force **structured JSON**:
   ```
   {
     "ticker": "...",
     "catalyst": { "score": 1-10, "materiality_pct_mktcap": ..,
                   "materiality_pct_revenue": .., "new_vs_recompete": "..",
                   "contract_type": "..", "already_priced_in": bool, "rationale": ".." },
     "value":    { "score": 1-10,
                   "pillars": { "moat":0-2, "returns_on_capital":0-2,
                                "balance_sheet":0-1.5, "owner_earnings_fcf":0-1.5,
                                "management_capital_allocation":0-1, "margin_of_safety":0-2 },
                   "key_metrics": { .. }, "rationale": ".." },
     "confidence": "high|medium|low",
     "sources_checked": [ .. ]
   }
   ```
   Bounded tool loop — no open-ended autonomy.
4. **Rank + render:** HTML digest **sorted by value score**, with the catalyst as the
   "why it surfaced." Reuse `format_alert_email` / `send_email`.

### Value rubric (the six pillars → the value score)
| Pillar | Lens | Measures |
|---|---|---|
| Moat / business quality | Buffett | durable advantage, pricing power, contract incumbency |
| Return on capital | Munger | ROIC, ROE |
| Balance-sheet strength | Buffett | debt/equity, interest coverage, liquidity, cash |
| Owner earnings / FCF | Buffett | free cash flow, FCF margin, cash conversion |
| Management & capital allocation | Pabrai | insider ownership, buybacks vs. dilution |
| Margin of safety / valuation | Pabrai | P/E, EV/EBITDA, P/FCF vs. intrinsic value |

Pillar weights live in `config.json` so they can be tuned without code changes.

### Key metrics to surface per company
market cap, price, revenue (TTM) + growth, gross/operating/net margin, ROE, ROIC,
FCF + FCF margin, debt/equity, interest coverage, current ratio, cash, P/E, EV/EBITDA,
P/FCF, insider ownership %, share-count trend — plus the catalyst: contract value,
agency, sole-source/OTA flag, award as % of revenue and % of market cap.

## Hard rules — do not violate
- **No investment advice framing.** Output is **research / screening only.** Every
  digest carries a disclaimer: not a recommendation to buy or sell; do your own due
  diligence. The agent surfaces and rates; it never tells the user to trade, and it
  never places trades.
- **No watchlist self-management / no full workflow autonomy.** Full autonomy adds
  failure surface for a reliability-critical alert system. The watchlist
  (`data/watchlist.json`) is human-curated. Add complexity only where it delivers
  clear signal value. (A future *optional* discovery pass — surfacing off-watchlist
  public recipients for **manual** review — must never auto-add to the watchlist.)
- **NAICS: AI / Defense / Tech only.** The media/production codes (512110, 512191,
  512199, 541810, 611430) belong to the *other* track — keep them out of here.
- **Never commit secrets.** API keys load from env vars only (see below).
  `.env` and `data/seen_awards.json` are gitignored. (Repo history was previously
  scrubbed of leaked credentials — do not reintroduce any.)
  **Before the first push from a fresh clone, rotate any previously exposed
  credentials (SAM.gov key, Gmail app password, GitHub token) and authenticate with a
  freshly generated GitHub token — never embed a token in the git remote URL.**
- **Idempotent runs.** De-dup on `data/seen_awards.json` so re-runs don't re-alert.

## Data sources (reliability notes)
- **USASpending.gov** — reliable, no auth. Primary award source.
- **SEC EDGAR** — reliable, no auth. `companyfacts` for fundamentals; 8-K full-text
  search for contract-award disclosures. **IC-adjacent contracts appear only here**,
  not on SAM.gov.
- **SAM.gov** — has authentication walls that complicate programmatic access; treat
  as secondary and degrade gracefully when it blocks.

## Stack & configuration
- **Language:** Python 3.11 (`runtime.txt`).
- **Reasoning model:** Anthropic Messages API with `tool_use`. Make the model a
  config value; default to a cost-efficient model (`claude-sonnet-4-6`) since this
  runs across the whole watchlist on a schedule, with the option to bump to
  `claude-opus-4-8` for deeper analysis. `anthropic` must be added to
  `requirements.txt` (not there yet).
- **Deployment:** Render. NOTE: `monitor.py` currently runs an infinite
  `schedule` loop (`Procfile: worker: python monitor.py`) — that's a **Worker**
  service, not a Cron Job. If it's deployed as a Cron Job, either switch the Render
  service type to Worker, or refactor `main()` to run once and exit for cron.
- **Secrets (env vars, set on Render):** `SAM_GOV_API_KEY`, `EMAIL_SENDER`,
  `EMAIL_PASSWORD`, `EMAIL_RECIPIENTS`, plus `ANTHROPIC_API_KEY` (new). `config.json`
  holds non-secret placeholders that these env vars override at load
  (`load_config`, ~line 61).

## Commands
- Install deps: `pip install -r requirements.txt`
- Run once (single ticker, wider window for testing):
  `python monitor.py --ticker KTOS --days 30` (see `main()` arg parsing, ~line 564)
- Run the full monitor: `python monitor.py`

## How to work in this repo
- Before adding a feature, propose a short plan; keep the ingestion, the
  enrichment/scoring (Anthropic calls), and the email I/O modular so each is testable
  alone.
- Build and test the **agentic scorer + EDGAR fundamentals enrichment** first — that
  is the net-new, highest-risk piece. The watchlist already provides tickers, so no
  recipient→ticker resolver is needed for the core loop.
- Do not add any trade-execution capability, and do not add watchlist self-management.

## Key files
- `monitor.py` — pipeline + (to be replaced) `score_award`
- `config.json` — sectors, keyword clusters, scoring weights, NAICS, model, pillar weights
- `data/watchlist.json` — human-curated companies (ticker, search_names, sectors, revenue)
- `data/seen_awards.json` — de-dup state (gitignored)
- `requirements.txt` — add `anthropic`
- `Procfile` / `runtime.txt` — Render worker + Python 3.11
