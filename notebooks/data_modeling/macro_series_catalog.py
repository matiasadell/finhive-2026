# Databricks notebook source
# DBTITLE 1,Macro Series Catalog (gold)
# MAGIC %md
# MAGIC # Macro Series Catalog (gold)
# MAGIC
# MAGIC Builds `finhive-2026.gold.macro_series_catalog` — hand-curated metadata for all 13 FRED series. Per contract1 §3.4.

# COMMAND ----------

# DBTITLE 1,Define catalog data
from datetime import datetime, timezone
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StringType, BooleanType, DoubleType, StructField

CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"

# Hand-curated per contract1 §3.4
# change_is_absolute: True for rates/percent/index (changes reported as point moves)
#                     False for levels (changes reported as percentages)
catalog_data = [
    # series_id,  label,                                                         unit,                   change_is_absolute, frequency,   category,        maturity_years
    ("GDP",      "Gross Domestic Product",                                      "billions of USD",     False,             "quarterly", "activity",      None),
    ("CPIAUCSL", "Consumer Price Index for All Urban Consumers: All Items",    "index",                False,             "monthly",   "inflation",     None),
    ("CPILFESL", "Core CPI: All Items Less Food & Energy",                     "index",                False,             "monthly",   "inflation",     None),
    ("UNRATE",   "Civilian Unemployment Rate",                                  "percent",              True,              "monthly",   "labour",        None),
    ("FEDFUNDS", "Effective Federal Funds Rate",                                "percent",              True,              "monthly",   "policy_rate",   None),
    ("DGS3MO",   "3-Month Treasury Constant Maturity Rate",                     "percent",              True,              "daily",     "yield_curve",   0.25),
    ("DGS2",     "2-Year Treasury Constant Maturity Rate",                      "percent",              True,              "daily",     "yield_curve",   2.0),
    ("DGS5",     "5-Year Treasury Constant Maturity Rate",                      "percent",              True,              "daily",     "yield_curve",   5.0),
    ("DGS10",    "10-Year Treasury Constant Maturity Rate",                    "percent",              True,              "daily",     "yield_curve",   10.0),
    ("DGS30",    "30-Year Treasury Constant Maturity Rate",                    "percent",              True,              "daily",     "yield_curve",   30.0),
    ("VIXCLS",   "CBOE Volatility Index (VIX)",                                 "index",                True,              "daily",     "risk_appetite", None),
    ("PAYEMS",   "All Employees: Total Nonfarm",                               "thousands of persons", False,             "monthly",   "labour",        None),
    ("INDPRO",   "Industrial Production Index",                                "index",                False,             "monthly",   "activity",      None),
]

schema = StructType([
    StructField("series_id", StringType(), False),
    StructField("label", StringType(), False),
    StructField("unit", StringType(), False),
    StructField("change_is_absolute", BooleanType(), False),
    StructField("frequency", StringType(), False),
    StructField("category", StringType(), False),
    StructField("maturity_years", DoubleType(), True),
])

catalog_df = (
    spark.createDataFrame(catalog_data, schema=schema)
    .withColumn("computed_at", F.lit(datetime.now(timezone.utc)))
)

display(catalog_df)

# COMMAND ----------

# DBTITLE 1,Write to gold.macro_series_catalog
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

# Full rebuild — overwrite is idempotent since we recompute from bronze each run
catalog_df.write.mode("overwrite").saveAsTable(f"{GOLD}.macro_series_catalog")

print(f"Wrote {len(catalog_data)} catalog rows into {GOLD}.macro_series_catalog")