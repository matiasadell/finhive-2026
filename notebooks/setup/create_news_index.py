# Databricks notebook source
# DBTITLE 1,Create news table and Vector Search index
# MAGIC %md
# MAGIC # Create news table and Vector Search index
# MAGIC
# MAGIC Run by hand, once per environment. Creates `` `finhive-2026`.gold.news `` with the schema from contract1 §5.1, enables Change Data Feed (required for Delta Sync), adds a PRIMARY KEY constraint on `chunk_id`, and creates the Vector Search endpoint + Delta Sync index with managed embeddings on the `text` column.
# MAGIC
# MAGIC **Embedding model:** `databricks-gte-large-en` (1024 dims, 8192 token context). Hybrid search (dense cosine + sparse keyword) is available at query time via `query_type="HYBRID"`.

# COMMAND ----------

# DBTITLE 1,Create table with CDF
CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"
NEWS_TABLE = f"{GOLD}.news"

# --- create table (contract1 §5.1 schema) --------------------------------
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

# --- enable Change Data Feed (required for Delta Sync) -------------------
spark.sql(f"""
    ALTER TABLE {NEWS_TABLE}
    SET TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

print(f"Table {NEWS_TABLE} created with CDF enabled")

# COMMAND ----------

# DBTITLE 1,Add PRIMARY KEY constraint
# --- add PRIMARY KEY constraint on chunk_id (contract1 §5.1, §5.3) ---------
# Required for Delta Sync index reconciliation by key
spark.sql(f"""
    ALTER TABLE {NEWS_TABLE}
    ADD CONSTRAINT chunk_id_pk PRIMARY KEY (chunk_id)
""")

print(f"PRIMARY KEY constraint added on chunk_id")

# COMMAND ----------

# DBTITLE 1,Create Vector Search endpoint
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import EndpointType, EndpointStatusState

w = WorkspaceClient()

ENDPOINT_NAME = "finhive-news-endpoint"
INDEX_NAME = f"{CATALOG}.gold.news_index"

# --- create Vector Search endpoint (Standard) -----------------------------
# Use get_endpoint to check existence — list_endpoints can be paginated
try:
    status = w.vector_search_endpoints.get_endpoint(ENDPOINT_NAME)
    state = status.endpoint_status.state if status.endpoint_status else "UNKNOWN"
    print(f"Endpoint '{ENDPOINT_NAME}' already exists — state: {state}")
except Exception:
    w.vector_search_endpoints.create_endpoint(
        name=ENDPOINT_NAME,
        endpoint_type=EndpointType.STANDARD,
    )
    print(f"Creating Vector Search endpoint '{ENDPOINT_NAME}'...")
    state = "PROVISIONING"

if state != EndpointStatusState.ONLINE:
    print(f"Endpoint is not yet online (state={state}). Wait ~2-5 min and re-run this cell + the next.")
else:
    print(f"Endpoint '{ENDPOINT_NAME}' is online and ready")

# COMMAND ----------

# DBTITLE 1,Create Delta Sync index
# --- create Delta Sync index with managed embeddings --------------------
# contract1 §5.3:
#   - HYBRID: dense cosine + sparse keyword, fused server-side
#   - Embeddings: managed on the `text` column
#   - Filterable: symbol, published_at
#   - Returned: chunk_id, text, symbol, title, source_domain, source_uri,
#               published_at, ingested_at
#   - Sync mode: TRIGGERED
#   - Embedding model: databricks-gte-large-en (1024 dims, 8192 token context)

from databricks.sdk.service.vectorsearch import (
    VectorIndexType, IndexSubtype, PipelineType,
    DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn,
)

try:
    w.vector_search_indexes.get_index(INDEX_NAME)
    print(f"Index '{INDEX_NAME}' already exists")
except Exception:
    w.vector_search_indexes.create_index(
        name=INDEX_NAME,
        endpoint_name=ENDPOINT_NAME,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        index_subtype=IndexSubtype.HYBRID,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=f"{CATALOG}.gold.news",
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name="text",
                    embedding_model_endpoint_name="databricks-gte-large-en",
                )
            ],
            pipeline_type=PipelineType.TRIGGERED,
            columns_to_sync=[
                "chunk_id", "text", "symbol", "title",
                "source_domain", "source_uri",
                "published_at", "ingested_at",
            ],
        ),
    )
    print(f"Creating Delta Sync index '{INDEX_NAME}'...")
    print(f"Embedding model: databricks-gte-large-en")
    print(f"Index subtype: HYBRID (dense cosine + sparse keyword)")
    print(f"Sync mode: TRIGGERED")
    print(f"\nHand off to the agent side:")
    print(f"  - Endpoint name: {ENDPOINT_NAME}")
    print(f"  - Index name:    {INDEX_NAME}")
    print(f"  - Sync mode:    TRIGGERED (call sync_index after ingestion)")

# COMMAND ----------

