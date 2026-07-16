# V/Line On-Time Performance — Streamlit dashboard

Tells the punctuality story from the gold fact `fct_service_performance`: headline KPIs → least-reliable
lines → delay distribution → peak effect → written insights. Pure Streamlit + Plotly, deploys to Railway.

**🔗 Live:** [dashboard-production-a1ad.up.railway.app](https://dashboard-production-a1ad.up.railway.app)

## Data (static snapshot)

The app reads `data/fct_service_performance.parquet` — a snapshot of the gold table, committed with the
app. No live Databricks connection, no credentials, no running SQL warehouse. Refresh = re-export.

**Export it** (run in a Databricks notebook, after the gold job has populated the fact):

```python
CATALOG = "transport_vic_dev"   # or transport_vic_prod
out = f"/Volumes/{CATALOG}/03_gold/exports/fct_service_performance.parquet"
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.`03_gold`.exports")

# Enrich with human line names from the schedule dim (route_id -> route_long_name).
routes = (spark.table(f"{CATALOG}.`02_silver`.routes")
          .select("route_id", "route_short_name", "route_long_name")
          .dropDuplicates(["route_id"]))
pdf = (spark.table(f"{CATALOG}.`03_gold`.fct_service_performance")
       .join(routes, "route_id", "left")
       .toPandas())
pdf.attrs.clear()   # serverless/Spark Connect attaches a non-serializable PlanMetrics to .attrs;
                    # to_parquet serializes .attrs into the file metadata and chokes on it.
pdf.to_parquet(out, index=False)
print("wrote", out, "|", len(pdf), "rows |",
      f"{pdf['route_long_name'].notna().mean():.0%} matched a line name")
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

Deployed via Railway's **native GitHub integration** — the service watches this repo and **auto-deploys on
every push** touching `dashboard/`. No CI token, no GitHub Action, no secret. Config:

- **Root directory** `dashboard` → Railway builds this subfolder with Nixpacks from `requirements.txt`.
- **Region** `asia-southeast1` (Singapore) — nearest to Melbourne.
- `railway.json` sets the Streamlit start command (binds `$PORT`, headless).
- **No environment variables** — the Parquet ships in the image (no live warehouse / credentials).

## Tuning

- `ON_TIME_SEC` / `SEVERE_SEC` in `app.py` — keep in sync with the gold notebook's thresholds.
- `PEAK_HOURS`, `MIN_TRIPS` — peak window and the minimum-trips cutoff for the line ranking.
