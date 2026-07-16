# Transport Victoria — GTFS Data Platform

A production-style **medallion lakehouse** on Databricks that ingests Public Transport Victoria's
**V/Line GTFS-Realtime feeds** (trip updates + vehicle positions) and the **GTFS static schedule**, to
answer executive questions about how reliable and punctual the regional network is.

Built to demonstrate **end-to-end data engineering**: serverless ingestion, streaming Auto Loader,
protobuf decoding in-engine, an SCD2 dimension, parameterised job fan-out, a Kimball-style gold fact,
Infrastructure-as-Code, and a live analytics dashboard — all parametrised for dev/prod and deployed via
the Databricks Asset Bundle and CI.

**🔗 Live dashboard:** [dashboard-production-a1ad.up.railway.app](https://dashboard-production-a1ad.up.railway.app)
— on-time performance, worst lines, delay distribution, and peak-hour reliability, computed from the gold
fact. (Streamlit, auto-deployed to Railway from this repo.)

> **Deeper docs:** operational quick-reference → [CLAUDE.md](CLAUDE.md) · design rationale →
> [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) · analytics plan →
> [docs/GOLD_PLAN.md](docs/GOLD_PLAN.md) · dashboard → [dashboard/README.md](dashboard/README.md).

---

## What it does

Public Transport Victoria publishes open GTFS data. This platform captures it continuously and models it
for analytics:

- **GTFS-Realtime** (protobuf, polled): *trip updates* (predicted arrival/delay per stop) every 5 min,
  and *vehicle positions* (lat/lon/bearing) every 2 min.
- **GTFS-Static** (CSV, weekly): the timetable — `stops`, `routes`, `trips`, `stop_times`, `calendar`,
  and friends — the schedule baseline that realtime performance is measured against.

The **gold** layer answers the executive question — *what's our on-time performance, how reliable is the
service, where and when does it fail?* — as a single fact (`fct_service_performance`) that a Streamlit
dashboard slices. Framing and metric definitions are in [docs/GOLD_PLAN.md](docs/GOLD_PLAN.md).

## Architecture

Two ingestion paths feed one medallion lakehouse. Bronze is raw/lossless, silver is typed, gold is
analytics-ready.

```
GTFS-Realtime  (trip updates + vehicle positions)
  Azure Function (timer)  ──►  ADLS landing (.pb bytes)  ──►  Bronze (binaryFile)  ──►  Silver (from_protobuf)  ──►  ┐
  ingestion/function_app.py    lakehouse@transportvicdatalake    raw content column     explode → typed            │
                                                                                                                   ├──►  Gold
GTFS-Static  (weekly schedule)                                                                                     │     fct_service_performance
  Databricks job  ──►  extract zip  ──►  gtfs_data Volume  ──►  Bronze (CSV fan-out, 11 files)  ──►  Silver dims  ──►┘     (collapse snapshots →
  00_extractions/*     per-mode .txt      /Volumes/.../gtfs_data   parameterised for_each task      SCD2 + snapshots        on-time / delay facts)

Serving
  Gold fact  ──►  Parquet snapshot  ──►  Streamlit dashboard  ──►  Railway (Singapore), auto-deployed from GitHub on push
              export                     dashboard/app.py
```

**Components**
- **Ingestion (realtime)** — Azure Functions (Python, Flex Consumption). Two timer-triggered functions
  share one `download_and_persist(url, path, prefix)` helper; auth via a `KeyId` API header and
  **managed identity** to storage (no keys in code).
- **Ingestion (static)** — a Databricks job downloads the weekly GTFS zip, unpacks each mode's
  `google_transit.zip` into per-feed `.txt` files under an `export_date=YYYYMMDD/` partition.
- **Storage** — two Azure storage accounts, deliberately split: the **data lake**
  `transportvicdatalake` (ADLS Gen2 / hierarchical namespace, reached only via managed identity), and a
  plain **runtime account** `transportvictoriastorage` for the Functions host (an HNS account is not a
  supported Functions host store — see decisions below).
- **Lakehouse** — Databricks Unity Catalog, **serverless** compute only, deployed as an Asset Bundle.
- **IaC** — the Function App, plan, App Insights, storage roles, and budget are Bicep (`infra/`).
- **Dashboard** — a Streamlit app (`dashboard/`) reading a Parquet snapshot of the gold fact, deployed
  to Railway via native GitHub integration (auto-deploys on push, region `asia-southeast1`). No
  credentials on the public app — the data ships in the image, not a live warehouse connection.

## Data pipelines

Catalog is parametrised per environment (`transport_vic_dev` / `transport_vic_prod`). Schemas are
numbered so they sort in pipeline order: `` `01_bronze` ``, `` `02_silver` ``, `` `03_gold` ``.

| Layer | Table(s) | Source | Job | Trigger |
|---|---|---|---|---|
| Bronze | `vline_trip_updates` | realtime `.pb` | `bronze_vline_trip_updates` | daily |
| Bronze | `vline_vehicle_positions` | realtime `.pb` | `bronze_vline_vehicle_positions` | hourly |
| Bronze | 11 static tables (`stops`, `routes`, `trips`, …) | weekly CSV | `weekly_schedule_download` (`for_each`) | weekly |
| Silver | `trip_updates` (one row / stop_time_update) | bronze | `silver_trip_updates` | `table_update` |
| Silver | `stops` (**SCD2 dimension**) | bronze | `silver_stops` | `table_update` |
| Silver | `vehicle_positions` (typed positions) | bronze | `silver_vehicle_positions` | `table_update` |
| Silver | `routes`, `trips`, `calendar`, `calendar_dates`, `stop_times`, `transfers` (typed snapshots) | bronze | `silver_schedule_dims` | `table_update` |
| Gold | `fct_service_performance` (1 row / served stop, final delay + flags) | silver `trip_updates` | `gold_service_performance` | `table_update` |

## Design decisions

Full rationale in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md); the load-bearing ones:

### Tables

- **Bronze is raw and lossless.** Protobuf feeds land as raw `.pb` bytes (`binaryFile`); CSV feeds land
  as raw strings. Nothing is decoded or cast in bronze — so if silver logic changes or has a bug, we
  replay from untouched bronze without re-fetching from the API.
- **Decode protobuf with `from_protobuf`, not a UDF.** A Python `MessageToJson` UDF runs on executors,
  which on serverless lack `gtfs-realtime-bindings` (`ModuleNotFoundError`). `from_protobuf` decodes in
  the JVM against a compiled `.desc` descriptor — typed output, no executor dependency. One descriptor
  (`transit_realtime.FeedMessage`) serves *both* realtime feeds.
- **Grain is chosen per feed.** Trip updates keep **every** `FULL_DATASET` snapshot (one row per
  `stop_time_update`) — that time series is how a prediction firms up, and collapsing it is a *gold*
  concern. Vehicle positions keep one row per vehicle per snapshot (the position trail).
- **`stops` is the only SCD2 dimension.** Stop attributes (name, platform, coordinates) change rarely
  but matter historically, so silver-stops is Type-2 (key `(feed_id, stop_id)`, a null-safe `xxhash64`
  change-hash over the source columns, `export_date`-based validity, no delete handling so a skipped
  extract can't wrongly retire a stop). The change-hash **excludes pipeline columns** (`_ingest_ts` etc.)
  or every weekly load would spuriously open a new version.
- **Other schedule dims are current-snapshot, not SCD2.** `routes`/`trips`/`calendar`/… are the *latest
  export only* (typed, deduped, overwrite). SCD2 machinery isn't worth it for reference data that's
  joined for names and calendars; bronze still retains every export if point-in-time is needed later.
- **Keys are always `(feed_id, natural_key)`.** GTFS ids (`stop_id`, `route_id`, …) repeat across the
  mode feeds (Regional Train, Metro Train, Coach, …), so `feed_id` is part of every identity.
- **Gold collapses the prediction time-series to a final state.** `fct_service_performance` is one row
  per served stop: a window function keeps the **latest snapshot per `(trip, stop)`** (the firmed-up,
  ~actual delay), and on-time (`≤ 359s`, V/Line's 5:59) / severe (`> 900s`) are classified *after* the
  collapse — **never as a pre-filter**, which would delete the on-time rows and make punctuality
  un-measurable. `SKIPPED`/cancelled stops are flagged out of the metric; the destination (max served
  `stop_sequence`) carries the headline number. Every KPI is then a `GROUP BY` over this one fact.

### Lakeflow jobs

- **Serverless only; Asset Bundle per environment.** `databricks bundle deploy -t {dev|prod}` selects
  the catalog. Dev uses `mode: development`, which **auto-pauses** schedules/triggers — so dev is tested
  with explicit `bundle run`, and only prod fires on its own.
- **One parameterised bronze notebook fans out over 11 static files** via a `for_each` task. The
  notebook never names source columns (reads by header), so it's schema-agnostic; `schemaEvolutionMode`
  + `mergeSchema` + task **retries** absorb a feed introducing a new column (the stream restarts once and
  self-heals). Adding a 12th file is one list entry, not a new notebook.
- **Silver is triggered by `table_update` on its bronze source**, so it runs right after bronze lands —
  no schedules to keep in sync. Multi-table triggers (the schedule dims) require a `condition`; we use
  `ANY_UPDATED` + `wait_after_last_change_seconds` so the parallel fan-out settles into a single run and
  a skipped file can't stall it.
- **Checkpoints live in UC Volumes, one folder per streaming query.** A schema change on a streaming
  table therefore means: clear the checkpoint → drop the table → re-run (a `04_maintenance` reset
  notebook automates this).
- **Streaming jobs carry `max_concurrent_runs: 1` + a failure email.** Overlapping runs would collide on
  the shared checkpoint.
- **The Functions runtime is kept off the data lake.** `AzureWebJobsStorage` needs blob+queue+table and
  isn't supported on an HNS account — and hosting it there would leak the data lake's account key into
  app settings. Runtime → non-HNS `transportvictoriastorage`; data → `transportvicdatalake` via managed
  identity only.

### Dashboard & serving

- **Static Parquet snapshot, not a live warehouse connection.** The dashboard reads a Parquet export of
  the gold fact, shipped inside the image. This puts **no credentials and no running SQL warehouse** on a
  public app, costs nothing to host, and can't break when a warehouse idles or a token expires — the
  right trade for a portfolio. Refresh = re-export + push (a redeploy). A live path (a read-only Databricks
  **service principal** + serverless SQL warehouse) is a one-function swap if ever needed.
- **Railway native GitHub integration over a CI token.** With the Railway GitHub App connected, the
  service deploys straight from the repo on push — no `RAILWAY_TOKEN`, no GitHub Action, no secret. Root
  directory `dashboard/`, watch paths scoped so pipeline commits don't trigger dashboard redeploys.
- **Insights are computed, not hard-coded.** The "what the data says" section is derived from the loaded
  data at render time (worst/best line, peak-vs-offpeak gap, the delay tail), so it stays correct as the
  snapshot grows. Charts follow data-viz basics: median/OTP over the mean, sorted single-hue magnitude
  bars, hero numbers for KPIs.

## Repository layout

```
ingestion/                     Azure Functions app (realtime feed downloaders)
infra/                         Bicep IaC (Function App, storage roles, App Insights, budget)
transport_victoria_databricks/ Databricks Asset Bundle
  databricks.yml               bundle config + dev/prod targets (catalog var)
  resources/*.job.yml          one file per Lakeflow job
  src/.../00_extractions/      weekly GTFS download + unzip
  src/.../01_bronze/           raw ingestion (protobuf + static CSV fan-out)
  src/.../02_silver/           typed models (from_protobuf, SCD2 stops, dims)
  src/.../03_gold/             fct_service_performance (punctuality fact) + change-log
  src/.../04_maintenance/      reset/utility notebooks
dashboard/                     Streamlit app (reads gold Parquet snapshot) + Railway deploy config
docs/                          DESIGN_DECISIONS.md, GOLD_PLAN.md
```

## Deploy & run

From `transport_victoria_databricks/`:

```bash
databricks bundle validate -t dev
databricks bundle deploy   -t dev          # dev pauses triggers — run jobs explicitly:
databricks bundle run weekly_schedule_download -t dev    # static: download → extract → bronze → silver dims
databricks bundle run bronze_vline_trip_updates -t dev
databricks bundle run silver_trip_updates       -t dev
```

Azure Function (from `infra/`, then `ingestion/`):

```bash
az deployment group create -g transport-victoria-rg \
  --template-file infra/main.bicep --parameters infra/main.parameters.json
cd ../ingestion && func azure functionapp publish transport-vic-data-download-app
```

Dashboard: pushes to `main` touching `dashboard/` auto-deploy to Railway (native GitHub integration).
Refresh the numbers by re-exporting the gold Parquet — see [dashboard/README.md](dashboard/README.md).

## Status

- ✅ Realtime bronze (trip updates + vehicle positions) + jobs
- ✅ Static bronze — weekly download/extract + parameterised fan-out (11 files)
- ✅ Silver — trip updates, stops (SCD2), vehicle positions (validated against real data), schedule dims
- ✅ Catalog parametrised dev/prod; Azure Function on managed identity + split storage
- ✅ **Gold** — `fct_service_performance` punctuality fact (snapshot-collapse + on-time/severe/terminus flags)
- ✅ **Dashboard** — Streamlit on Railway, auto-deployed from GitHub · [live](https://dashboard-production-a1ad.up.railway.app)
- 🔲 Next — Q2 cancellation anti-join (scheduled-but-absent), `dim_date` peak/weekday cut, vehicle-position map, cosmetic polish

## Data source & licence

Public Transport Victoria / V/Line open data via the
[Victorian Government open data portal](https://opendata.transport.vic.gov.au). GTFS and GTFS-Realtime
are open specifications. This is a personal portfolio project, not affiliated with PTV or V/Line.
