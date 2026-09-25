# Databricks notebook source
# DBTITLE 1,News ingestion - worker
# MAGIC %md
# MAGIC # News ingestion - worker
# MAGIC
# MAGIC Fetches company news for one symbol from the **Finnhub** API, chunks each article (headline + summary = 1 chunk), computes a content-derived `chunk_id` (`sha256(symbol # source_uri # chunk_no)`), and **MERGEs** into `` `finhive-2026`.gold.news ``. Never overwrites — contract1 §5.2. The Finnhub API key is read from the `finhive` secret scope.

# COMMAND ----------

# DBTITLE 1,Widgets
dbutils.widgets.text("series", "")
dbutils.widgets.text("from_date", "")
dbutils.widgets.text("to_date", "")
dbutils.widgets.text("job_name", "")

series = dbutils.widgets.get("series")
from_date = dbutils.widgets.get("from_date") or None
to_date = dbutils.widgets.get("to_date") or None
job_name = dbutils.widgets.get("job_name")

if not series:
    raise ValueError("widget 'series' is required")

# COMMAND ----------

# DBTITLE 1,Fetch, chunk, and MERGE into gold.news
import hashlib
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from pyspark.sql import functions as F

SOURCE = "Finnhub"
CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"
NEWS_TABLE = f"{GOLD}.news"

# --- resolve date range -------------------------------------------------
# default: last 3 days (covers weekends + market holidays)
today = datetime.now(timezone.utc).date()
if not to_date:
    to_date = today.isoformat()
if not from_date:
    from_date = (today - timedelta(days=3)).isoformat()

try:
    # --- fetch from Finnhub ------------------------------------------------
    finnhub_key = dbutils.secrets.get(scope="finhive", key="finnhub-api-key")
    url = (
        "https://finnhub.io/api/v1/company-news"
        f"?symbol={series}&from={from_date}&to={to_date}&token={finnhub_key}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    articles = resp.json()

    if not articles:
        # no news for this symbol in the window — not an error
        result = {
            "series": series,
            "source": SOURCE,
            "status": True,
            "updateAt": datetime.now(timezone.utc).isoformat(),
            "lastObservationDate": to_date,
            "item_count": 0,
            "error": None,
            "job_name": job_name,
        }
        dbutils.notebook.exit(json.dumps(result))

    # --- chunk + build rows ------------------------------------------------
    # contract1 §5.4: chunk coarsely — headline + summary = 1 chunk (chunk_no=0)
    rows = []
    for art in articles:
        source_uri = art.get("url", "")
        if not source_uri:
            continue

        domain = urlparse(source_uri).netloc
        if domain.startswith("www."):
            domain = domain[4:]

        headline = art.get("headline", "")
        summary = art.get("summary", "")
        text = f"{headline}\n\n{summary}".strip()
        if not text:
            continue

        chunk_no = 0
        chunk_id = hashlib.sha256(
            f"{series}#{source_uri}#{chunk_no}".encode()
        ).hexdigest()

        published_at = datetime.fromtimestamp(art["datetime"], tz=timezone.utc)
        ingested_at = datetime.now(timezone.utc)

        rows.append((
            chunk_id,
            series,
            text,
            chunk_no,
            headline,
            domain,
            source_uri,
            published_at,
            ingested_at,
            "en",  # Finnhub articles are English; contract1 §5.1 allows NULL
        ))

    if not rows:
        result = {
            "series": series,
            "source": SOURCE,
            "status": True,
            "updateAt": datetime.now(timezone.utc).isoformat(),
            "lastObservationDate": to_date,
            "item_count": 0,
            "error": None,
            "job_name": job_name,
        }
        dbutils.notebook.exit(json.dumps(result))

    # --- ensure table exists (schema from contract1 §5.1) ------------------
    spark.sql(f"CREATE CATALOG IF NOT EXISTS `{CATALOG}`")
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {NEWS_TABLE} (
            chunk_id        STRING    NOT NULL,
            symbol          STRING    NOT NULL,
            text            STRING    NOT NULL,
            chunk_no        INT       NOT NULL,
            title           STRING    NOT NULL,
            source_domain   STRING    NOT NULL,
            source_uri      STRING    NOT NULL,
            published_at    TIMESTAMP NOT NULL,
            ingested_at     TIMESTAMP NOT NULL,
            language        STRING
        )
        USING DELTA
    """)

    # --- MERGE (contract1 §5.2: never overwrite) ----------------------------
    schema = (
        "chunk_id string, symbol string, text string, chunk_no int, "
        "title string, source_domain string, source_uri string, "
        "published_at timestamp, ingested_at timestamp, language string"
    )
    new_df = spark.createDataFrame(rows, schema=schema)
    new_df.createOrReplaceTempView("news_updates")

    spark.sql(f"""
        MERGE INTO {NEWS_TABLE} AS t
        USING news_updates AS s
        ON t.chunk_id = s.chunk_id
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)

    result = {
        "series": series,
        "source": SOURCE,
        "status": True,
        "updateAt": datetime.now(timezone.utc).isoformat(),
        "lastObservationDate": to_date,
        "item_count": len(rows),
        "error": None,
        "job_name": job_name,
    }
except Exception as e:
    result = {
        "series": series,
        "source": SOURCE,
        "status": False,
        "updateAt": datetime.now(timezone.utc).isoformat(),
        "lastObservationDate": None,
        "item_count": 0,
        "error": str(e),
        "job_name": job_name,
    }

dbutils.notebook.exit(json.dumps(result))

# COMMAND ----------

