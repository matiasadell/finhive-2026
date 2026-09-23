<style>
/*
databricks red #FF3621
databricks grey #1B3139
databricks dark grey #122025
databricks logo link
https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/databricks.png
*/

body {
  background: #122025;
  background-attachment: fixed;
  background-size: cover;
  color: #FFFFFF;
  padding: 2em;
  position: relative;
}

body::before,
body::after {
  content: "";
  position: fixed;
  width: 70vmin;
  height: 70vmin;
  background-image: url("https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/databricks.png");
  background-size: contain;
  background-repeat: no-repeat;
  opacity: 0.05;
  pointer-events: none;
  z-index: -1;
}

body > * {
  position: relative;
  z-index: 1;
}

body::before {
  top: -35vmin;
  left: -35vmin;
}

body::after {
  bottom: -35vmin;
  right: -35vmin;
}

th {
  color: #FF3621;
}

table,
pre,
code {
  background-color: #252525;
}

table {
  display: block;
  width: fit-content;
  max-width: 100%;
  margin: 0 auto;
  overflow-x: auto;
  white-space: nowrap;
}

pre {
  display: block;
  width: fit-content;
  max-width: 100%;
  margin: 0 auto;
  overflow-x: auto;
}

pre code {
  white-space: pre;
}

.hljs-keyword,
.hljs-built_in {
  color: #FF3621;
}

h1 {
  color: #FF3621;
  font-size: 3em;
}

h2 {
  color: #FF3621;
}

h3 {
  color: #FFFFFF;
}
</style>

# Contract 1 — the gold tables and news index the agent layer reads

**Status:** draft for review. Nothing here is built yet; nothing here asks you to change what you
already built.
**From:** the agent side (`notebooks/{setup,llm,tools,agents,graph,guardrails,cache,evaluation,serving}`).
**To:** the data engineer who owns `notebooks/data_ingestion/` and `notebooks/data_modeling/`.
**Date:** 2026-09-23.
**Why a contract and not a ticket:** the agent is a panel of LLM experts that is *forbidden from
computing any number itself* — every figure in an answer comes from a deterministic Python function
reading one of the tables below (`agent-design.md` §1, invariant 1). That makes these table shapes the
system's credibility, not a convenience. A column whose unit is ambiguous does not produce a bug, it
produces a confidently wrong sentence in front of a reader.

---

## 0. What is being asked, in one table

Seven tables in a new schema `finhive-2026.gold`, plus one Vector Search index over the last of
them. All names are **proposals** — see §9 if you want to change them; the agent reads them from
`notebooks/setup/config.ipynb`, where they are literals in one cell, so a rename is a one-line edit on
my side and it shows up in a diff.

| # | Deliverable | Grain | Feeds | Slice |
|---|---|---|---|---|
| 1 | `finhive-2026.gold.instruments` | one row per symbol | `list_instruments`, symbol resolution, expert routing | **A** |
| 2 | `finhive-2026.gold.technical_indicators` | one row per (symbol, trade_date) | `technical_analyst` — 6 tools | **A** |
| 3 | `finhive-2026.gold.macro_series` | one row per (series_id, observation_date) | `macro_analyst` — 5 tools | **B** |
| 4 | `finhive-2026.gold.macro_series_catalog` | one row per series_id | the same 5 tools (units, curve order) | **B** |
| 5 | `finhive-2026.gold.risk_metrics` | one row per (symbol, window) | `quant_risk_analyst` — 2 tools | **C** |
| 6 | `finhive-2026.gold.correlations` | one row per (symbol_a, symbol_b, window) | `quant_risk_analyst` — 2 tools | **C** |
| 7 | `finhive-2026.gold.news` + its Delta Sync index | one row per (symbol, url, chunk_no) | `news_analyst` — 1 tool | **D** |
| — | *fundamentals* | — | `fundamental_analyst` | **deferred to contract 2** (§3.7) |

**Please deliver in slices, in this order.** Each slice unblocks one expert end to end, and I can
build and test that expert while you work on the next one. **Slice A alone unblocks me** — it is two
tables over the six symbols already in `config/data_ingestion/yahoo.json`, and I do not need the other
five to start.

---

## 1. The one constraint that shapes every table below

At request time the agent may **not** issue a Spark query. Each of these tables is read **once**, into
a pandas DataFrame, when the model replica starts — five frames, a few megabytes total, held in
memory and captured by the tool closures (`agent-design.md` §5.3). A tool call then costs
microseconds instead of a job.

Three consequences for you:

1. **Long and narrow, not wide and per-symbol.** One table covering every symbol, with `symbol` as a
   column. Not one table per symbol, and not one column per symbol.
2. **Pre-computed.** RSI, Sharpe, VaR, correlations — all of it materialised. I do not recompute
   anything I could have read.
3. **Small enough to hold in memory.** The current universe is trivial. At the design's 35 symbols the
   largest table (`technical_indicators`, ~20 years daily) is roughly 180k rows — still fine. If the
   universe ever reaches hundreds of symbols, tell me and I will change how I load rather than ask you
   to shrink the table.

---

## 2. Conventions that apply to every table

These are the parts most likely to cause a silently wrong answer, so they are stated once here and
assumed everywhere below.

### 2.0 One catalog: `finhive-2026`

There is exactly one Unity Catalog catalog, **`finhive-2026`**, and it holds everything — the model
services (`finhive-2026.default.finhive_router`, `…finhive_embeddings`) and **every medallion layer**.
No second catalog for raw, none for gold, none for models.

| Layer | Where | Who |
|---|---|---|
| bronze, silver | `finhive-2026.<your schemas>` | yours; the agent never reads them and does not care how they are named |
| **gold** | **`finhive-2026.gold`** | yours to produce, the only thing the agent reads (§1) |
| model services | `finhive-2026.default` | already there |

**The hyphen makes it an identifier that must be backtick-quoted in every SQL reference**, because
`finhive-2026` otherwise tokenizes as `finhive` minus `2026`:

```sql
CREATE SCHEMA IF NOT EXISTS `finhive-2026`.gold;     -- works
CREATE SCHEMA IF NOT EXISTS finhive-2026.gold;       -- parse error
```

This applies to `spark.table()` and `spark.sql()` too, since both go through the same parser, and it
is why the agent side stores the quoted form. Written plainly as `finhive-2026.gold.instruments`
throughout this document for readability; quote it in code.

### 2.1 Symbols

- Column `symbol`, **canonical Yahoo-style ticker**, uppercase, exactly as the upstream source names
  it: `AAPL`, `MSFT`, `^GSPC`, and later `BTC-USD`, `EURUSD=X`.
- This matters because the agent resolves what a user typed into a symbol by trying, in order: the
  literal uppercase, an alias table (`BITCOIN`→`BTC-USD`, `S&P 500`→`SPY`), then `{X}-USD`, then
  `{X}=X`, then a **unique name-prefix match** against `instruments.name`. It **never near-matches** —
  answering about the wrong instrument is worse than failing — so a symbol that does not appear
  verbatim in `instruments` is simply unreachable.
- One canonical spelling per instrument. No `BRK.B` in one table and `BRK-B` in another.

### 2.2 Dates and timestamps

| Column kind | Type | Meaning |
|---|---|---|
| `trade_date`, `observation_date`, `as_of_date` | `DATE` | the **data** date. No timezone, no time component. A trading day, or the date FRED attributes an observation to |
| `published_at`, `ingested_at`, `computed_at` | `TIMESTAMP` | **UTC**, always. Wall-clock events |

**Every table carries a pipeline timestamp**, because it is how I detect staleness (§6) and how every
tool states its "as of" date — a contract with the model, not a nicety (§2.5). Which one depends on
what the row is: `computed_at` where the row is *derived* (everything in §3), `ingested_at` where the
row is *fetched* and there is nothing to compute (`news`, §5.1). A table may carry both.

### 2.3 Missing values are NULL, never zero, never forward-filled

**This is the single most important line in this document.** A zero is a number a language model will
happily reason about — "RSI of 0, deeply oversold" — where a NULL renders as `n/a` and the expert says
it could not see that figure.

- Indicator warm-up periods are **NULL**: `rsi_14` is NULL for the first 13 rows of a symbol,
  `sma_200` for the first 199.
- A series with no observation on a date has **no row**, or a row with a NULL `value`. Never a
  forward-filled copy of yesterday. If you forward-fill, the macro analyst will report a stale print
  as today's, with today's date attached.
- No sentinel values: not `0`, not `-1`, not `-999`, not `NaN` written as a string.

### 2.4 Units, spelled out per column

Every numeric column below states its unit explicitly, and the two conventions are:

- **Rates, returns, volatilities and drawdowns: decimal, not percent.** `0.1834` means 18.34%. One
  representation everywhere.
- **Volatility and returns: annualized** where the column name says so (`√252` for daily data), and
  the column name always says so.

Losses carry their sign: `var_95_1d = -0.0312` is a 3.12% loss, and `max_drawdown = -0.2841` is a
28.41% peak-to-trough fall. **Do not** store losses as positive magnitudes — half the industry does it
each way, so the only safe thing is for this document to pick one and for the data to match it.

### 2.5 Adjusted vs raw price

Indicators are computed on **adjusted** close (split- and dividend-adjusted), because a 4:1 split
otherwise prints as a 75% crash and the technical analyst will report it as one. Raw `close` is kept
in the same row so a price snapshot can quote the price a reader would actually see on a screen.

The current Yahoo worker already runs `auto_adjust=False`, so both columns are available upstream.

### 2.6 Idempotency

Every gold table is **rebuilt or MERGEd to a stable state**, never blindly appended. Running the
pipeline twice on the same day must leave the table byte-identical, because these tables are read into
memory and a duplicated `(symbol, trade_date)` row silently doubles a weight in an average.

State the primary key and enforce it. `agent-design.md`'s consumer will assert uniqueness (§8).

---

## 3. The tables

### 3.1 `finhive-2026.gold.instruments`

The universe, and the only place that says what kind of thing each symbol is. Small — one row per
symbol.

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `symbol` | STRING | no | canonical ticker (§2.1). **PK** |
| `name` | STRING | no | human name — "Apple Inc.", "S&P 500". Used for the unique name-prefix match, so it must be the name a person would type |
| `asset_class` | STRING | no | closed vocabulary: `equity`, `etf`, `index`, `crypto`, `fx` |
| `currency` | STRING | no | ISO 4217, e.g. `USD` |
| `exchange` | STRING | yes | e.g. `NASDAQ`. Informational |
| `sector` | STRING | yes | leave NULL for now; populated by contract 2 (§3.7) |
| `is_active` | BOOLEAN | no | false = kept for history, excluded from `list_instruments` |
| `first_date` | DATE | no | earliest `trade_date` available for this symbol |
| `last_date` | DATE | no | latest `trade_date` available |
| `computed_at` | TIMESTAMP | no | UTC |

**Why `asset_class` is not cosmetic.** It is what makes the panel skip experts that have nothing to
say: the planner never convenes the fundamental analyst for `crypto` or `fx` (no issuer, no financial
statements). The vocabulary is closed on purpose — a sixth value would silently fall through that
rule.

`first_date` / `last_date` let `list_instruments` state its own coverage instead of the model guessing
it.

### 3.2 `finhive-2026.gold.technical_indicators`

The largest table, and the one Slice A turns on. **Full available history per symbol** — a tool
returns price history, so this is not an as-of snapshot.

| Column | Type | Null? | Unit / meaning |
|---|---|---|---|
| `symbol` | STRING | no | §2.1. **PK part** |
| `trade_date` | DATE | no | trading day. **PK part** |
| `open`, `high`, `low`, `close` | DOUBLE | no | **raw** price, instrument currency |
| `adj_close` | DOUBLE | no | split/dividend adjusted. Every indicator below derives from this |
| `volume` | BIGINT | yes | shares/contracts. NULL where the source has none (common for indices) |
| `return_1d` | DOUBLE | yes | decimal, `adj_close / prev(adj_close) - 1`. NULL on a symbol's first row |
| `sma_20`, `sma_50`, `sma_200` | DOUBLE | yes | simple moving averages of `adj_close`. NULL during warm-up |
| `ema_12`, `ema_26` | DOUBLE | yes | exponential, the MACD inputs |
| `rsi_14` | DOUBLE | yes | Wilder's RSI, **0–100** (not 0–1) |
| `macd` | DOUBLE | yes | `ema_12 - ema_26` |
| `macd_signal` | DOUBLE | yes | 9-period EMA of `macd` |
| `macd_hist` | DOUBLE | yes | `macd - macd_signal` |
| `bb_mid_20`, `bb_upper_20`, `bb_lower_20` | DOUBLE | yes | Bollinger, 20-period, **2 standard deviations** |
| `atr_14` | DOUBLE | yes | Average True Range, price units (not a percentage) |
| `realized_vol_20d` | DOUBLE | yes | **annualized decimal** — stdev of `return_1d` over 20 days × √252 |
| `realized_vol_60d` | DOUBLE | yes | same, 60 days |
| `drawdown` | DOUBLE | yes | decimal, **≤ 0**: `adj_close / cummax(adj_close) - 1` over the full history |
| `computed_at` | TIMESTAMP | no | UTC |

**PK:** `(symbol, trade_date)`.

**Two derived things I deliberately do *not* ask you for**, because they are interpretation rather
than data, and the rules live in my tools next to the note explaining how they were calibrated:

- overbought/oversold labels (RSI ≥ 70 / ≤ 30), golden and death crosses (`sma_50` crossing
  `sma_200`), and the volatility regime (`realized_vol_20d / realized_vol_60d` > 1.2 elevated, < 0.8
  subdued). I compute all three from the columns above.
- Anything about *what it means*. The tools label, the experts narrate.

Both moving-average crossovers need the *previous* row, which is why full history matters and an
as-of snapshot would not do.

### 3.3 `finhive-2026.gold.macro_series`

Long format, every FRED series in one table. Frequencies differ per series and that is fine — see
§2.3 on not forward-filling.

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `series_id` | STRING | no | FRED identifier, e.g. `DGS10`. **PK part** |
| `observation_date` | DATE | no | the date FRED attributes the observation to. **PK part** |
| `value` | DOUBLE | yes | in the unit declared in the catalog (§3.4). NULL where FRED reports no value |
| `computed_at` | TIMESTAMP | no | UTC |

**PK:** `(series_id, observation_date)`.

I need **at least 5 years** of history per series, because every macro figure the agent reports is
paired with its five-year percentile — "the VIX is 18" is not an answer, "the VIX is 18, the 34th
percentile of its five-year range" is. I compute that percentile myself from this history; no column
needed.

### 3.4 `finhive-2026.gold.macro_series_catalog`

Small, hand-curated, and it carries the three pieces of metadata without which the macro tools cannot
format a sentence correctly.

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `series_id` | STRING | no | **PK**, joins to §3.3 |
| `label` | STRING | no | human name — "10-Year Treasury Constant Maturity Rate" |
| `unit` | STRING | no | display unit: `percent`, `index`, `billions of USD`, `thousands of persons` |
| `change_is_absolute` | BOOLEAN | no | **see below** |
| `frequency` | STRING | no | `daily`, `weekly`, `monthly`, `quarterly` |
| `category` | STRING | no | closed vocabulary: `policy_rate`, `yield_curve`, `inflation`, `labour`, `activity`, `risk_appetite` |
| `maturity_years` | DOUBLE | yes | only for `yield_curve` series: `0.25`, `2`, `5`, `10`, `30`. NULL otherwise |
| `computed_at` | TIMESTAMP | no | UTC |

**`change_is_absolute` is the column that prevents a specific wrong sentence.** When unemployment goes
from 4.0% to 4.2%, the correct statement is "up 0.2 percentage points", not "up 5%" — both are
arithmetically true and only one is how anyone talks about rates. So:

- `true` for anything already measured in percent or index points — every `DGS*`, `UNRATE`,
  `FEDFUNDS`, `VIXCLS`. Changes get reported as absolute point moves.
- `false` for levels — `CPIAUCSL`, `GDP`, `PAYEMS`, `INDPRO`. Changes get reported as percentages.

`category` is what groups the snapshot and drives two tools directly: `get_yield_curve` selects
`category = 'yield_curve'` and **orders by `maturity_years`** (which is why that column exists — a
curve sorted alphabetically by `series_id` is not a curve), and `get_inflation_picture` selects
`category = 'inflation'`.

### 3.5 `finhive-2026.gold.risk_metrics`

**As-of snapshot only** — one row per symbol per window, holding the latest computation. Please do not
accumulate history here; if you want a history table for your own auditing, name it something else and
I will not read it.

| Column | Type | Null? | Unit / meaning |
|---|---|---|---|
| `symbol` | STRING | no | **PK part** |
| `window` | STRING | no | closed vocabulary: `3m`, `1y`, `3y`. **PK part**. `1y` is required; the others are welcome |
| `as_of_date` | DATE | no | last `trade_date` included in the calculation |
| `observations` | INT | no | trading days actually used. Lets a tool say `n/a` instead of quoting a Sharpe built on 11 days |
| `total_return` | DOUBLE | yes | decimal, over the whole window |
| `annualized_return` | DOUBLE | yes | decimal |
| `volatility_annualized` | DOUBLE | yes | decimal, × √252 |
| `sharpe_ratio` | DOUBLE | yes | unitless |
| `sortino_ratio` | DOUBLE | yes | unitless |
| `downside_deviation` | DOUBLE | yes | annualized decimal |
| `risk_free_rate_annualized` | DOUBLE | no | decimal — **the rate you actually used** |
| `risk_free_source` | STRING | no | e.g. `DGS3MO`. Traceability: a Sharpe ratio without its risk-free rate is not a number, it is an opinion |
| `var_95_1d` | DOUBLE | yes | decimal, **negative** (§2.4). Historical (empirical quantile), not parametric |
| `var_99_1d` | DOUBLE | yes | decimal, negative |
| `expected_shortfall_95_1d` | DOUBLE | yes | decimal, negative — mean return below the 95% VaR |
| `max_drawdown` | DOUBLE | yes | decimal, **≤ 0**, within the window |
| `beta` | DOUBLE | yes | vs `benchmark_symbol`, unitless |
| `r_squared` | DOUBLE | yes | 0–1, of the same regression. Without it a beta of 0.9 on noise reads as meaningful |
| `benchmark_symbol` | STRING | yes | e.g. `^GSPC`. NULL when beta is NULL |
| `computed_at` | TIMESTAMP | no | UTC |

**PK:** `(symbol, window)`.

Please state in the pipeline whether VaR is historical or parametric — I have asked for **historical**
above, because the expert's whole reason for being on the panel is to say when tails got worse, and a
normal assumption hides exactly that.

### 3.6 `finhive-2026.gold.correlations`

Pairwise, and stored **once per unordered pair** to halve the row count and remove any chance of the
two directions disagreeing.

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `symbol_a` | STRING | no | **PK part**. Constraint: `symbol_a < symbol_b` lexicographically |
| `symbol_b` | STRING | no | **PK part** |
| `window` | STRING | no | closed vocabulary: `90d`, `1y`. **PK part** |
| `as_of_date` | DATE | no | last `trade_date` included |
| `correlation` | DOUBLE | yes | Pearson on daily `return_1d`, in `[-1, 1]` |
| `observations` | INT | no | **overlapping** days both symbols traded |
| `computed_at` | TIMESTAMP | no | UTC |

**PK:** `(symbol_a, symbol_b, window)`. I symmetrize on read, so do not write both directions.

`observations` matters more here than elsewhere: a crypto/equity pair has genuinely mismatched
calendars, and a correlation over 40 overlapping days should not be presented like one over 250.

### 3.7 Deferred to contract 2 — fundamentals

Not requested now, and nothing above depends on it. Recorded here so it does not arrive as a surprise:
the `fundamental_analyst` needs multiples, margins, ROE/ROA, growth, and **sector** (the
`instruments.sector` column left NULL in §3.1), and no upstream source ingests any of it today. Until
then the panel runs with four experts, which the design already supports — an expert that is not
consulted simply does not appear in the answer.

If you happen to be adding a source anyway, `sector` alone is the highest-value single column, because
it turns "a P/E of 35" into "a P/E of 35, against a software median of 28" — which is the difference
between a number and a judgement.

---

## 4. Upstream series the agent needs that are not ingested yet

Slices B and C need series that are not in `config/data_ingestion/*.json` today. These are additions
to your existing config files — the pattern already works, nothing structural changes.

### 4.1 FRED (`config/data_ingestion/fred.json`)

| series_id | Why the agent needs it | `category` | `change_is_absolute` |
|---|---|---|---|
| `DGS3MO` | the short end of the curve; also the risk-free rate for Sharpe (§3.5) | `yield_curve` | true |
| `DGS2` | the 2s10s inversion is the single most-asked macro question | `yield_curve` | true |
| `DGS5` | curve shape | `yield_curve` | true |
| `DGS30` | the long end | `yield_curve` | true |
| `CPILFESL` | **core** CPI — headline alone cannot answer an inflation question | `inflation` | false |
| `VIXCLS` | risk appetite. The design's own example sentence is about the VIX | `risk_appetite` | true |
| `PAYEMS` | labour, beyond the unemployment rate | `labour` | false |
| `INDPRO` | activity at a monthly frequency; `GDP` alone is quarterly and stale | `activity` | false |

Already ingested and still needed: `DGS10` (`yield_curve`), `CPIAUCSL` (`inflation`), `UNRATE`
(`labour`), `FEDFUNDS` (`policy_rate`), `GDP` (`activity`).

Optional, nice to have: `DFF` (daily effective fed funds, `policy_rate`), `PCEPILFE` (core PCE, the
Fed's actual target, `inflation`).

### 4.2 Yahoo (`config/data_ingestion/yahoo.json`)

The current six symbols are enough for Slice A. `^GSPC` is already there and is the beta benchmark, so
nothing is blocking. Universe expansion is an open parameter — §9.

---

## 5. The news index (Slice D)

News is the one dataset where the agent queries a live service at request time rather than reading a
frame — a Vector Search index, which is a read-only call to a Databricks service, not a table scan.

### 5.1 `finhive-2026.gold.news`

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `chunk_id` | STRING | no | **PK and the index's primary key.** Must be **content-derived and stable**: `sha256(symbol ‖ '#' ‖ source_uri ‖ '#' ‖ chunk_no)`. **Never positional** |
| `symbol` | STRING | no | canonical ticker the article was fetched for. Filterable |
| `text` | STRING | no | the chunk the index embeds. See §5.3 on size |
| `chunk_no` | INT | no | 0-based position within the article |
| `title` | STRING | no | headline |
| `source_domain` | STRING | no | e.g. `reuters.com`. Every claim gets attributed to it |
| `source_uri` | STRING | no | the article URL |
| `published_at` | TIMESTAMP | no | UTC. Filterable. **Must not be in the future** |
| `ingested_at` | TIMESTAMP | no | UTC |
| `language` | STRING | yes | ISO 639-1. `en` only for now, if the provider reports it |

**Why `chunk_id` must be a hash and not a row number:** the index is a Delta Sync index, which
reconciles by key. A positional id reshuffles every key whenever the source changes, and the previous
build of this system had to force a full rebuild — over an hour of embedding — once the drift passed 50
rows.

**Grain is `(symbol, source_uri, chunk_no)`**, matching a per-symbol ingestion loop. An article that
mentions two symbols appears twice, once under each, with different `chunk_id`s. That duplicates a
little embedding cost and buys an exact-match `symbol` filter, which is the right trade at this size.

### 5.2 Write pattern — this one is load-bearing

**MERGE or append-and-prune. Never `overwrite`.** A Delta Sync index does not reconcile a wholesale
replacement of keys; replacing the table wholesale forces a full index rebuild. Concretely:

- new articles: append
- re-fetched articles: MERGE on `chunk_id`
- rows older than the retention window: **DELETE** — Delta Sync propagates deletes, which is what keeps
  recency filters cheap and the index inside its size ceiling

### 5.3 The index itself

| Requirement | Value |
|---|---|
| Type | Delta Sync index over `finhive-2026.gold.news` |
| `index_subtype` | **`HYBRID`** — dense cosine plus sparse keyword, fused server-side |
| Embeddings | **Managed**, on the `text` column. Free Edition has no Direct Vector Access, so the tool sends `query_text` and the index embeds it. Vectors are never uploaded or returned |
| Filterable columns | `symbol`, `published_at` |
| Returned columns | `chunk_id`, `text`, `symbol`, `title`, `source_domain`, `source_uri`, `published_at`, `ingested_at` |
| Sync mode | `TRIGGERED` |
| Embedding model | your choice — please just **write down which**, so a future change in retrieval quality can be attributed |

**Three things I need you to hand me, not just build:**

1. the Vector Search **endpoint name** and the **full index name** (I put both in the secret scope);
2. **how to read the index's last sync time** — a `describe()` field or a table. Every news answer
   states "as of", and its honesty depends on this;
3. whether a **range filter on `published_at`** works on the Free Edition endpoint. If it does not, say
   so and I will over-fetch and filter client-side — but I would rather know than discover it.

### 5.4 Sizing — the ceiling is real, and it is arithmetic

Embedding was measured at **723–1048 ms per row**, against a build job timeout of **10,800 s**. So a
**full rebuild fits about 10,000 rows** and no more:

```
rows = symbols × articles_per_day × retention_days × chunks_per_article

6,000 rows  ≈ 1.2–1.8 h   fits
8,000 rows  ≈ 1.6–2.3 h   fits
16,800 rows ≈ 3.4–4.9 h   does NOT fit
```

Daily increments are small and unaffected — this bounds the *rebuild*, which is what you need after any
schema change. So the retention window and the symbol subset are not details, they are the two knobs
that keep a rebuild possible. Practical starting point: equities and major ETFs only, 14–30 days
retention, and chunk articles coarsely (a headline plus a lead paragraph is often one chunk).

**Provider quota is the other ceiling.** One call per symbol per day over 35 symbols is ~1,000 calls a
month, which is at or above the free tier of most search APIs. Restricting to equities and major ETFs,
or running every other day, keeps it inside — and bounds the index at the same time.

---

## 6. Freshness, and how I detect staleness

`tools/panel_data`'s startup check asserts that every table exists **and is fresh**, and turns a stale
table into a named, red failure rather than a confident answer built on last week's prices. For that I
need an agreed SLA, not a guess.

**Proposed**, matching the existing weekday-after-market-close job trigger:

| Table | Expected pipeline timestamp (§2.2) | Expected data date |
|---|---|---|
| `technical_indicators`, `risk_metrics`, `correlations` | within 24h on a trading day | `max(trade_date)` = last completed trading day |
| `macro_series` | within 24h | varies by series frequency — no assertion on the data date |
| `instruments`, `macro_series_catalog` | within 7 days | — |
| `news` | within 24h on `max(ingested_at)` — `news` has no `computed_at` (§2.2) | `max(published_at)` within the retention window |

A weekend and a market holiday must not read as a failure, so my check compares against the last
trading day, not against "yesterday".

**Grants.** Three identities need read access, and a missing grant fails the agent at model load —
which is the worst place to find out, because the endpoint accepts the deployment and then cannot
answer:

```sql
GRANT USE CATALOG ON CATALOG `finhive-2026` TO `<principal>`;
GRANT USE SCHEMA  ON SCHEMA  `finhive-2026`.gold TO `<principal>`;
GRANT SELECT      ON SCHEMA  `finhive-2026`.gold TO `<principal>`;
-- plus read on the Vector Search index and its endpoint
```

for: (1) my development identity, (2) the service principal that runs the agent job, (3) the identity
the Model Serving endpoint runs as.

---

## 7. Things in the current ingestion that would block this

Reported, not touched — these are in your half of the repo and I have deliberately changed nothing.
Two of them genuinely block Slice A, so they are worth a look before you start on gold.

### 7.1 Yahoo worker: the ticker leaks into the column names — *blocks Slice A*

`notebooks/data_ingestion/yahoo/worker.ipynb` flattens the yfinance MultiIndex like this:

```python
pdf.columns = [
    "_".join(str(p) for p in col if p).lower() if isinstance(col, tuple) else str(col).lower()
    for col in pdf.columns
]
```

For a single-ticker download, yfinance returns `(Price, Ticker)` tuples, so `finhive-2026.yahoo.AAPL` ends
up with columns `open_aapl`, `high_aapl`, `close_aapl`, `adj close_aapl`, `volume_aapl`. Two
consequences:

- **every Yahoo table has a different schema**, so a UNION across the universe cannot be written
  generically;
- `adj close_aapl` **contains a space**, so it needs backticks in every SQL reference.

A one-line fix inside the existing comprehension — drop the ticker level and normalise separators —
makes every table share one schema:

```python
pdf.columns = [
    (col[0] if isinstance(col, tuple) else col).strip().lower().replace(" ", "_")
    for col in pdf.columns
]
# -> date, open, high, low, close, adj_close, volume
```

### 7.2 Append without dedup means re-runs duplicate rows — *blocks Slice A*

Both workers do `sdf.write.mode("append")`. A re-run, a repair run, or a backfill with an earlier
`start_date` writes the same `date` again, so `finhive-2026.yahoo.<series>` can hold several rows per
trading day. Gold must therefore deduplicate on the natural key — `row_number()` over
`(date)` partitioned per symbol, keeping the newest `ingested_at` — before computing anything. An
indicator computed over duplicated days is wrong in a way nothing downstream can detect.

### 7.3 Table names need quoting, and now so does the catalog

``finhive-2026.yahoo.`^GSPC` `` needs backticks in **two** places: the catalog, for its hyphen (§2.0),
and the table, for its caret. Any generated UNION over the universe must quote every identifier, and
the list of tables should come from `config/data_ingestion/yahoo.json` rather than from `SHOW TABLES`,
so the config stays the single source of truth for the universe.

### 7.4 The workers create a second catalog — *needs repointing*

Both workers run `spark.sql("CREATE CATALOG IF NOT EXISTS finhive")` and write to
`finhive.yahoo.<series>` / `finhive.fred.<series>` — a `finhive` catalog, without the year. There is
only one catalog and it is `finhive-2026` (§2.0), so as written the ingestion creates and fills a
catalog nothing else uses, and the gold tables would sit in a different catalog from their own
sources.

The fix is in the two `spark.sql` lines and the `table_name` f-string of each worker, and the
catalog needs its backticks:

```python
spark.sql("CREATE CATALOG IF NOT EXISTS `finhive-2026`")
spark.sql("CREATE SCHEMA IF NOT EXISTS `finhive-2026`.yahoo")
table_name = f"`finhive-2026`.yahoo.`{series}`"
```

Already-ingested data in `finhive` would need moving or re-ingesting; with `start_date: null` on most
series a re-ingest is the cheaper path. The rest of §7 is written against `finhive-2026` on the
assumption this lands first.

---

## 8. Acceptance checklist

These are the assertions the agent's own startup check will make. If these pass, Slice A is done and I
am unblocked — you do not need me to sign anything off.

```sql
-- 1. no duplicate primary keys anywhere
SELECT symbol, trade_date, COUNT(*) c
FROM `finhive-2026`.gold.technical_indicators
GROUP BY 1, 2 HAVING c > 1;                      -- expect 0 rows

-- 2. every instrument has enough history for a 200-day moving average
SELECT i.symbol, COUNT(t.trade_date) n
FROM `finhive-2026`.gold.instruments i
LEFT JOIN `finhive-2026`.gold.technical_indicators t USING (symbol)
WHERE i.is_active
GROUP BY 1 HAVING n < 200;                       -- expect 0 rows

-- 3. indicators are in range, or NULL — never zero-filled
SELECT COUNT(*) FROM `finhive-2026`.gold.technical_indicators
WHERE rsi_14 IS NOT NULL AND (rsi_14 < 0 OR rsi_14 > 100);        -- expect 0
SELECT COUNT(*) FROM `finhive-2026`.gold.technical_indicators
WHERE realized_vol_20d IS NOT NULL AND realized_vol_20d <= 0;     -- expect 0
SELECT COUNT(*) FROM `finhive-2026`.gold.technical_indicators
WHERE drawdown IS NOT NULL AND drawdown > 0;                      -- expect 0

-- 4. warm-up really is NULL and not 0 (this is the check that catches a zero-fill)
SELECT symbol, MIN(trade_date) first_day,
       MIN(CASE WHEN sma_200 IS NOT NULL THEN trade_date END) first_sma200
FROM `finhive-2026`.gold.technical_indicators GROUP BY 1;
-- expect first_sma200 to be ~199 trading days after first_day, never equal to it

-- 5. asset_class vocabulary is closed
SELECT DISTINCT asset_class FROM `finhive-2026`.gold.instruments;
-- expect a subset of: equity, etf, index, crypto, fx

-- 6. correlations: one direction only, and in range
SELECT COUNT(*) FROM `finhive-2026`.gold.correlations WHERE symbol_a >= symbol_b;      -- expect 0
SELECT COUNT(*) FROM `finhive-2026`.gold.correlations
WHERE correlation IS NOT NULL AND (correlation < -1 OR correlation > 1);        -- expect 0

-- 7. losses are negative (§2.4)
SELECT COUNT(*) FROM `finhive-2026`.gold.risk_metrics
WHERE (var_95_1d IS NOT NULL AND var_95_1d > 0)
   OR (max_drawdown IS NOT NULL AND max_drawdown > 0);                          -- expect 0

-- 8. macro: every series has a catalog row, 5y of history, and no future dates
SELECT s.series_id FROM (SELECT DISTINCT series_id FROM `finhive-2026`.gold.macro_series) s
LEFT JOIN `finhive-2026`.gold.macro_series_catalog c USING (series_id)
WHERE c.series_id IS NULL;                                                      -- expect 0 rows
SELECT series_id, MIN(observation_date), MAX(observation_date)
FROM `finhive-2026`.gold.macro_series GROUP BY 1;
-- expect MIN <= today - 5 years for every series
SELECT DISTINCT unit, change_is_absolute FROM `finhive-2026`.gold.macro_series_catalog;
-- eyeball: percent/index-point series must be true, level series false

-- 9. yield curve is orderable
SELECT series_id, maturity_years FROM `finhive-2026`.gold.macro_series_catalog
WHERE category = 'yield_curve' ORDER BY maturity_years;
-- expect every row to have a non-null maturity_years

-- 10. news: stable unique keys, no future publications
SELECT COUNT(*) - COUNT(DISTINCT chunk_id) FROM `finhive-2026`.gold.news;              -- expect 0
SELECT COUNT(*) FROM `finhive-2026`.gold.news WHERE published_at > current_timestamp(); -- expect 0
```

For the index, the check that matters is **two-sided convergence**: the index's row count and
`finhive-2026.gold.news`'s row count must agree after a sync, in both directions. An index with fewer rows
is mid-sync; an index with more rows has orphaned keys from a previous build, and that is the symptom
that a positional `chunk_id` or an `overwrite` slipped in.

---

## 9. Open parameters — please fill these in and hand the document back

Everything above is a shape. These are the numbers, and they are yours to set because they trade off
against cost and quota, not against correctness.

| Parameter | Proposal | Notes |
|---|---|---|
| Universe | keep the current 6 for Slice A | The design targets 35. Expanding costs correlation rows quadratically (35 symbols = 595 pairs per window) |
| Risk windows | `1y` required; `3m` and `3y` if cheap | |
| Correlation windows | `90d` and `1y` | |
| Beta benchmark | `^GSPC` | Already ingested |
| Risk-free rate | `DGS3MO` | Needs §4.1 |
| News retention | 14–30 days | Drives the §5.4 ceiling directly |
| News symbol subset | equities + major ETFs only | Not indices, not FX |
| News provider | your call | Quota is the constraint (§5.4) |
| Freshness SLA | §6's table | |
| Table names | `finhive-2026.gold.*` as above | Rename freely; they are literals in one cell of `setup/config.ipynb` |
| Secret key naming | `snake_case`, matching the existing `fred_api_key` | I will add one key per table name to the `finhive` scope |

---

## 10. What I own, so the boundary is explicit

So there is no ambiguity about who does what:

**Mine.** Everything under `notebooks/{setup,llm,tools,agents,graph,guardrails,cache,evaluation,serving}`
— the model access layer, the 20 tools, the five experts, the planner, consensus arithmetic, both
guardrails, the LangGraph assembly, and the Model Serving deployment. I read every table name from the
`finhive` secret scope. I read each gold table **once**, at model load, and never issue a Spark query at
request time. I treat article text as untrusted data — any instruction inside an article is ignored.

**Yours.** `notebooks/data_ingestion/`, `notebooks/data_modeling/`, the jobs that run them, the
`finhive-2026.gold` tables above, and the Vector Search index over `finhive-2026.gold.news` — including its
lifecycle and rebuild rules.

**Neither of us, yet.** Fundamentals (contract 2), and the semantic cache (agent side, but it depends
on infrastructure that has not been verified).

I have changed nothing in your half of the repo while writing this, including the two blocking issues
in §7 — they are yours to fix or to delegate back to me, whichever you prefer.
