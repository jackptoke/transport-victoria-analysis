# V/Line On-Time Performance — Streamlit dashboard

Tells the punctuality story from the gold fact `fct_service_performance`: headline KPIs → least-reliable
lines → delay distribution → peak effect → written insights. Pure Streamlit + Plotly, deploys to Railway.

## Data (static snapshot)

The app reads `data/fct_service_performance.parquet` — a snapshot of the gold table, committed with the
app. No live Databricks connection, no credentials, no running SQL warehouse. Refresh = re-export.

**Export it** (run in a Databricks notebook, after the gold job has populated the fact):

```python
CATALOG = "transport_vic_dev"   # or transport_vic_prod
out = f"/Volumes/{CATALOG}/03_gold/exports/fct_service_performance.parquet"
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.`03_gold`.exports")
(spark.table(f"{CATALOG}.`03_gold`.fct_service_performance")
      .toPandas()
      .to_parquet(out, index=False))
print("wrote", out)
```

**Download it** to this folder (from the repo root):

```bash
databricks fs cp \
  dbfs:/Volumes/transport_vic_dev/03_gold/exports/fct_service_performance.parquet \
  dashboard/data/fct_service_performance.parquet
```

## Run locally

```bash
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to Railway

`railway.json` sets the Streamlit start command (binds `$PORT`, headless). Point Railway at this
`dashboard/` directory (root directory = `dashboard`) and it builds with Nixpacks from `requirements.txt`.
No environment variables needed — the data ships in the image.

## Tuning

- `ON_TIME_SEC` / `SEVERE_SEC` in `app.py` — keep in sync with the gold notebook's thresholds.
- `PEAK_HOURS`, `MIN_TRIPS` — peak window and the minimum-trips cutoff for the line ranking.
