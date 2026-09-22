# Agent data contract — what the agent reads, and what it needs to be true

**Status:** proposed, awaiting the data engineer's review
**Audience:** the data engineer who owns `notebooks/data_ingestion/` and `notebooks/data_modeling/`
**Companion:** `agent-design.md` sections 5.2, 5.3, 8.2 and 19.2 (why each field exists);
`ARCHITECTURE_V2.md` section 9 (the medallion model these tables are the gold layer of)

---

## 0. Why this document exists

`agent-design.md` section 19.2 lists the inputs the agent requires *before* it can be built, and
puts the gold tables first. Those tables do not exist yet: today's ingestion writes one raw table
per series (`finhive.yahoo.<series>`, `finhive.fred.<series>`) with no derived layer above it.

This document specifies **only the reading side** — the shape the agent's tool layer expects. How
those tables are produced (bronze, silver, deduplication, expectations, scheduling) is the data
engineer's design, and deliberately not prescribed here. Section 8 lists what raw material already
exists, as information, not as a proposal.

Nothing in the agent tree will ever read a bronze, silver or per-series table.

---

## 1. The read interface

The agent never issues a Spark query at request time (`agent-design.md` section 5.3). It loads
every table **once** — at model load inside the serving container, or at stage start in a job —
into `PanelData`, a frozen dataclass of pandas frames a few megabytes in total, and every tool
call is then an in-memory lookup.

The only thing the data side has to provide is that these tables exist, are readable, and hold the
columns below. The agent receives a reader as an argument:

```python
def read_gold_table(fully_qualified_name: str) -> pandas.DataFrame: ...
```

**Grants.** Both identities that load `PanelData` need `SELECT` on every table in section 3 and
`USE CATALOG` / `USE SCHEMA` above them:

- the `finhive_deploy_agent` job's service principal, which runs the verification stages;
- **the served model's identity**, which runs `load_context` inside the Model Serving container.

The second is the one that gets forgotten. A missing grant there does not fail at deploy time — it
fails when the new endpoint version first tries to answer, which is exactly the failure
`agent-design.md` section 17 puts a post-deploy smoke test in the way of.

---

## 2. Naming

| | Proposed | Note |
|---|---|---|
| Catalog | `finhive` | What the existing ingestion notebooks already create |
| Schema | `analytics` | New |
| Tables | `finhive.analytics.gold_*` | |

**Open for the data engineer:** `ARCHITECTURE_V2.md` section 9 names the catalog `finhive_free`
(catalog per environment), while the ingestion notebooks in `main` hardcode `finhive`. The two
need reconciling. The agent does not care which wins — catalog and schema are variables in
`notebooks/setup/config.py` (`agent-design.md` section 3.1), so changing them is a one-line edit
on this side. Pick one and say which.

---

## 3. The tables

Conventions that apply to every table below, and matter more than any individual column:

- **A missing value is `NULL`, never `0` and never a sentinel.** `agent-design.md` section 5.1
  renders `NULL` as `n/a`; a zero is a number the model will reason about, and an invented figure
  in an answer is the one failure this whole design exists to prevent. This is the single most
  important line in this document.
- **Every table carries `as_of`** (`timestamp`, UTC), the moment the row's figures were computed.
  Tool output quotes it verbatim — `agent-design.md` section 5.1 makes "as of YYYY-MM-DD" a
  parsed contract, not a nicety, because `graph/opinion.py` regex-scans for exactly that pattern
  to date the evidence.
- **Ratios are decimal fractions, not percentages.** `0.184`, not `18.4`. Stated once here so no
  tool has to guess which convention a column follows.
- Types are Spark SQL types.

### 3.1 `gold_instruments` — the universe

Grain: one row per instrument. Backs `list_instruments`, and `resolve_symbol`'s unique-name-prefix
fallback.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `symbol` | string | no | Primary key. The canonical ticker, exactly as it appears in every other table: `AAPL`, `BTC-USD`, `EURUSD=X`, `^GSPC` |
| `name` | string | no | Full name. `resolve_symbol` does a **unique** prefix match on it |
| `asset_class` | string | no | One of `equity`, `etf`, `crypto`, `fx`, `index` |
| `currency` | string | no | |
| `sector` | string | **yes** | Equities only; `NULL` elsewhere |
| `industry` | string | yes | |
| `exchange` | string | yes | |
| `is_active` | boolean | no | |
| `as_of` | timestamp | no | |

**`asset_class` is load-bearing, not metadata.** `agent-design.md` section 6 forbids the
fundamental analyst for crypto and FX, and the golden set asserts it. A crypto row mislabelled
`equity` convenes an expert with no data for it.

### 3.2 `gold_technical_indicators` — the daily price series and its derived signals

Grain: one row per `(symbol, trade_date)`. **Full history, not just the latest row** —
`get_price_history` and `compare_instruments` read a window of it. The agent loads a bounded
window (a variable in `setup/config.py`, proposed default: 3 years) to keep `PanelData` small.

Backs `get_price_snapshot`, `get_technical_indicators`, `get_trend_signals`, `get_price_history`,
`compare_instruments`.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `symbol`, `trade_date` | string, date | no | Primary key |
| `open`, `high`, `low`, `close` | double | no | |
| `volume` | bigint | yes | `NULL` for FX |
| `sma_20`, `sma_50`, `sma_200` | double | yes | `NULL` until enough history; **never back-filled or zero-filled** |
| `ema_12`, `ema_26` | double | yes | |
| `rsi_14` | double | yes | 0–100 |
| `macd`, `macd_signal`, `macd_hist` | double | yes | |
| `bb_upper`, `bb_mid`, `bb_lower` | double | yes | 20-day, 2 sigma |
| `atr_14` | double | yes | |
| `realized_vol_20d`, `realized_vol_60d` | double | yes | Annualized, decimal fraction. **Both are required:** `get_trend_signals` labels the volatility regime off their ratio (> 1.2 elevated, < 0.8 subdued) |
| `pct_change_1d`, `pct_change_5d`, `pct_change_21d`, `pct_change_252d` | double | yes | Decimal fraction |
| `drawdown_from_peak` | double | yes | Negative decimal fraction, peak over the loaded window |
| `as_of` | timestamp | no | |

**Daily bars only, and a completed session only.** A partial current session must not appear as a
daily bar — that is the `period2 = now` bug class `ARCHITECTURE_V2.md` section 9 names explicitly.
If an intraday row is ever needed it belongs in a separate table with a different name.

### 3.3 `gold_risk_metrics` — risk-adjusted figures per instrument

Grain: one row per `(symbol, as_of_date)`. A short history is welcome; the agent reads the latest
row per symbol. Backs `get_risk_profile`, `compare_risk`.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `symbol`, `as_of_date` | string, date | no | Primary key |
| `window_days` | int | no | The lookback the metrics were computed over (proposed: 252) |
| `observations` | int | no | How many daily returns actually fed the calculation. The tool reports it; a Sharpe on 40 observations is a different statement from one on 252 |
| `ann_return`, `ann_volatility` | double | yes | Decimal fraction |
| `sharpe`, `sortino` | double | yes | **Both are required.** `agent-design.md` section 8.2 makes the Sharpe/Sortino gap information about asymmetric volatility that the expert must report rather than average away |
| `var_95`, `var_99` | double | yes | Historical, daily, as a **negative** decimal fraction |
| `expected_shortfall_95` | double | yes | Same sign convention |
| `max_drawdown` | double | yes | Negative decimal fraction |
| `beta` | double | yes | `NULL` where a beta is meaningless |
| `benchmark_symbol` | string | yes | What `beta` is against. Non-null whenever `beta` is |
| `skewness`, `kurtosis` | double | yes | |
| `risk_free_rate` | double | yes | The rate used for Sharpe/Sortino, decimal fraction |
| `as_of` | timestamp | no | |

### 3.4 `gold_correlations` — the pairwise matrix

Grain: one row per `(symbol_a, symbol_b, window_days, as_of_date)`. Backs `get_correlations`,
`get_diversification`.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `symbol_a`, `symbol_b` | string | no | Stored **once per unordered pair**, with `symbol_a < symbol_b` lexically. The tool mirrors the matrix itself |
| `window_days` | int | no | |
| `as_of_date` | date | no | |
| `correlation` | double | no | `[-1, 1]` |
| `observations` | int | no | Overlapping observations. A correlation on 15 shared days is noise and the tool needs to be able to say so |
| `as_of` | timestamp | no | |

Pairs with too few overlapping observations to be meaningful should be **omitted**, not written
with a placeholder correlation.

### 3.5 `gold_fundamentals` — valuation and quality, equities and ETFs

Grain: one row per `(symbol, as_of_date)`. Backs `get_fundamentals`, `compare_valuation`,
`get_sector_peers`, `get_fundamentals_coverage`.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `symbol`, `as_of_date` | string, date | no | Primary key |
| `sector`, `industry` | string | yes | **Must be populated for equities.** `get_sector_peers` is the tool that makes a multiple mean anything — `agent-design.md` section 8.2 forbids the expert from calling anything cheap or expensive without it |
| `market_cap`, `enterprise_value` | double | yes | Reporting currency |
| `currency` | string | yes | |
| `pe_trailing`, `pe_forward`, `pb`, `ps`, `ev_ebitda` | double | yes | |
| `gross_margin`, `operating_margin`, `net_margin` | double | yes | Decimal fraction |
| `roe`, `roa` | double | yes | Decimal fraction |
| `revenue_growth_yoy`, `eps_growth_yoy` | double | yes | Decimal fraction |
| `debt_to_equity`, `current_ratio` | double | yes | |
| `dividend_yield` | double | yes | Decimal fraction |
| `fiscal_period_end` | date | yes | What period the statement figures cover — distinct from `as_of_date`, which is when we fetched them |
| `as_of` | timestamp | no | |

**This table is where `NULL` discipline matters most.** Coverage from a free fundamentals source
is patchy and uneven across symbols; `get_fundamentals_coverage` exists precisely so the expert
can state what it could not see. A `0.0` written for a missing P/E becomes "trading at 0x
earnings" in an answer.

Crypto and FX rows should simply be **absent**, not present-and-empty.

### 3.6 `gold_macro` + `gold_macro_series` — macroeconomic observations and their metadata

Two tables. The observations are long and thin; the metadata is what makes them interpretable.

**`gold_macro`** — grain: one row per `(series_id, observation_date)`. Full history: the tools
compute the five-year percentile from it.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `series_id`, `observation_date` | string, date | no | Primary key |
| `value` | double | no | Rows with no observation are absent, not zero |
| `as_of` | timestamp | no | |

**`gold_macro_series`** — grain: one row per series.

| Column | Type | Null? | Notes |
|---|---|---|---|
| `series_id` | string | no | Primary key |
| `title` | string | no | Human-readable, quoted in tool output |
| `unit` | string | no | **Load-bearing.** `agent-design.md` section 5.2: when `unit == "percent"` the macro tools report **absolute** changes in points; otherwise percent changes. A wrong unit produces "inflation rose 43%" for a move from 2.3 to 3.3 |
| `frequency` | string | no | `D`, `W`, `M` or `Q` |
| `category` | string | no | One of `rates`, `inflation`, `labour`, `activity`, `risk_appetite` — the five buckets `get_macro_snapshot` reports and the macro analyst's prompt is organised around |
| `curve_tenor_months` | int | yes | Non-null only for Treasury constant-maturity series, and `get_yield_curve` is built entirely from it: `DGS3MO` → 3, `DGS2` → 24, `DGS10` → 120 |
| `as_of` | timestamp | no | |

**Series requested.** The five currently configured (`GDP`, `CPIAUCSL`, `UNRATE`, `FEDFUNDS`,
`DGS10`) leave two tools with nothing to say. The minimum set:

| Tool | Needs | Configured today |
|---|---|---|
| `get_yield_curve` | `DGS3MO`, `DGS2`, `DGS5`, `DGS10`, `DGS30` | `DGS10` only — one point is not a curve |
| `get_inflation_picture` | `CPIAUCSL`, `CPILFESL`, `PCEPI`, `T10YIE` | `CPIAUCSL` only |
| `get_macro_snapshot` (`risk_appetite`) | `VIXCLS` | none — and the macro analyst's prompt uses the VIX percentile as its worked example |
| `get_macro_snapshot` (`labour`, `activity`) | `UNRATE`, `PAYEMS`, `GDPC1`, `INDPRO` | `UNRATE`, `GDP` |

All are free FRED series and cost one config entry each.

---

## 4. Freshness

`tools/panel_data.py`'s `check` verifies each table exists **and is fresh**
(`agent-design.md` section 19.4), and a stale table turns that row red in
`verify_foundations`. Proposed thresholds, to be confirmed:

| Table | Fresh means |
|---|---|
| `gold_technical_indicators` | `max(trade_date)` is the last completed trading day, or the one before it |
| `gold_risk_metrics`, `gold_correlations` | `max(as_of_date)` within 7 days |
| `gold_fundamentals` | `max(as_of_date)` within 30 days |
| `gold_macro` | per series, not per table — each series has its own release cadence; the tools state the observation date and let the expert judge |
| `gold_instruments` | `max(as_of)` within 7 days |

These are thresholds for a red row in a verification stage, not data-quality expectations on
write. Expectations on write are the pipeline's own concern (`ARCHITECTURE_V2.md` section 9).

---

## 5. What the agent does **not** need

Stated so that nothing gets built on its account:

- No bronze or silver table, and no per-series table. The agent reads gold only.
- No Spark at request time, no SQL warehouse, no streaming path.
- No `ingestionLog`. Pipeline health is the data side's to monitor.
- No pre-computed percentiles, ratios or rankings beyond the columns above — every remaining
  derivation is a pure pandas function inside a tool. That is invariant 1 of `agent-design.md`:
  the figures in an answer come from deterministic code, and pushing them into the tool layer is
  what keeps them auditable from the trace.

---

## 6. Deferred: the news index

Not needed until the news analyst is built, and listed here only so it is not rediscovered later.
`agent-design.md` section 13.1 is the full specification; the short version is a Delta Sync index
over a `gold_news` table, `index_subtype: HYBRID`, managed embeddings on a `text` column, columns
`chunk_id` (a stable **content-derived** hash of `(url, chunk_no)`, never positional), `text`,
`ticker`, `published_at`, `title`, `source_domain`, `source_uri`, `ingested_at`, with `ticker` and
`published_at` usable as filters.

The sizing constraint is the part worth reading early: v1 measured 723–1,048 ms per row to embed
against a 10,800 s job timeout, so a full rebuild fits **about 10,000 rows**. Since
`rows = symbols × articles per day × retention days`, the retention window and the symbol subset
have to be chosen against that ceiling rather than discovered at it.

---

## 7. Open questions for the data engineer

1. **Catalog name** — `finhive` or `finhive_free`? (section 2)
2. **Are the freshness thresholds in section 4 achievable** on the current schedule, or should the
   agent's checks be looser?
3. **Fundamentals source.** The agreed default is yfinance, which covers most of the section 3.5
   columns for equities but not all, and nothing for crypto or FX. Is there a column in that table
   you expect to be systematically `NULL`? Knowing in advance is better than the expert
   discovering it per symbol.
4. **Benchmark for `beta`** — `^GSPC` for equities, and what for crypto?
5. **Correlation window and universe** — which `window_days`, and across all instruments or only
   within an asset class?
6. **The eight extra FRED series** in section 3.6 — in scope?

---

## 8. Appendix: what raw material already exists

Information only. The shape of the layers between this and section 3 is the data engineer's
design, and nothing here is a proposal.

| Source | Lands in | Covers |
|---|---|---|
| Yahoo Finance (`notebooks/data_ingestion/yahoo/`) | `finhive.yahoo.<series>`, append-only | OHLCV daily bars for the six configured series. Feeds 3.2, 3.3 and 3.4 in full |
| FRED (`notebooks/data_ingestion/fred/`) | `finhive.fred.<series>`, append-only | Five macro series. Feeds 3.6, short the series in the table above |
| — | — | **Nothing feeds 3.1 or 3.5 today.** The universe and fundamentals have no source in the repo |

Three things about the current ingestion that bear on building a layer above it, offered as
observations rather than requests: it appends without deduplicating, so the same bar can land
twice across runs; it stores the ingestion timestamp as the next run's `start_date`, which is not
the last observation date; and it writes the config file back into the Repos checkout, where the
next `sync_shared_repo.py` overwrites it. Whether any of those matters depends on how the silver
layer is built.
