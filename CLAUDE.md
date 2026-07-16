# Transport Victoria — GTFS-Realtime Data Platform

Ingests V/Line GTFS-Realtime feeds **and** the GTFS-Static schedule (Public Transport Victoria open
data) into a Databricks medallion lakehouse for delay/positional analytics.

> Detailed rationale for every choice below lives in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).
> This file is the quick operational reference; keep it current as the pipeline evolves.

## Architecture (end to end)

```
GTFS-Realtime (trip updates + vehicle positions)
  Azure Function (timer) → abfss landing (.pb) → Bronze (binaryFile) → Silver (from_protobuf) → Gold
  ingestion/function_app.py   lakehouse@transportvicdatalake              explode→typed        (pending)

GTFS-Static schedule (weekly)
  Databricks job: download → extract zip → gtfs_data Volume → Bronze (CSV fan-out, 11 files) → Silver → Gold
  00_extractions/*            /Volumes/<cat>/01_bronze/gtfs_data          per-file tables    SCD2 dims (stops)
```

- **Realtime ingestion** — `ingestion/function_app.py`, Azure Functions (Python). Two timer functions
  share one `download_and_persist(url, file_path, file_prefix)` helper. Auth: `KeyId: TRANSPORT_VIC_API_KEY`
  header to the API; `DefaultAzureCredential` (managed identity + RBAC) to storage.
  - Trip updates: every 5 min (`"0 */5 * * * *"`) → `landing/vline_trip_updates/date=YYYYMMDD/vline_tu_*.pb`.
  - Vehicle positions: every 2 min (`"0 */2 * * * *"`) → `landing/vline_vehicle_positions/date=YYYYMMDD/vline_vp_*.pb`.
- **Static ingestion** — a **Databricks** job (not the Azure Function): `00_weekly_schedule_download`
  pulls the weekly GTFS zip → `gtfs_raw` Volume; `01_extract_schedule_zip` unzips per-mode
  `google_transit.zip` → `.txt` under `gtfs_data/export_date=YYYYMMDD/<feed>/`.
- **Storage** — two accounts, deliberately split: data lake `transportvicdatalake` (ADLS Gen2 / HNS,
  container `lakehouse`, reached only via managed identity); Functions runtime on `transportvictoriastorage`
  (plain StorageV2, no HNS — an HNS account is not a supported Functions host store). Set via
  `infra/main.parameters.json` `storageAccountName`; **needs `az deployment` to apply** (see Status).
- **Databricks** — Asset Bundle in `transport_victoria_databricks/`, **serverless** only. Deployed via
  `databricks bundle deploy -t {dev|prod}`.

## Conventions (follow these)

- **Catalogs per environment:** dev `transport_vic_dev`, prod `transport_vic_prod` (set as `var.catalog`
  in `databricks.yml`). **Never hardcode** — notebooks read a `catalog` widget; jobs pass a `catalog`
  parameter defaulting to `${var.catalog}`.
- **Medallion schemas are numbered** for ordering: `01_bronze`, `02_silver`, `03_gold`.
  Leading digit ⇒ **must be backtick-quoted** in SQL / `.toTable()` (e.g. `` catalog.`01_bronze`.tbl ``).
  Backticks are for the SQL parser only — **never** in `/Volumes/...` filesystem paths.
- **Checkpoints & artifacts live in UC Volumes**, one folder per streaming query:
  `/Volumes/<catalog>/<schema>/_checkpoints/<table>` (schemaLocation nested under it).
  The protobuf descriptor lives in `/Volumes/<catalog>/02_silver/artifacts/gtfs_realtime.desc`.
- **Bundle jobs** are `resources/*.job.yml`; keep failure email + `max_concurrent_runs: 1` on streaming jobs.

## Pipelines

| Layer | Notebook | Table | Job | Trigger |
|---|---|---|---|---|
| Bronze (TU) | `01_bronze/01_raw_protobuff_to_bronze.ipynb` | `<cat>.01_bronze.vline_trip_updates` | `bronze_vline_trip_updates.job.yml` | daily `periodic` |
| Silver (TU) | `02_silver/02_bronze_to_silver.ipynb` | `<cat>.02_silver.trip_updates` | `silver_trip_updates.job.yml` | `table_update` on bronze |
| Bronze (VP) | `01_bronze/01_raw_vehicle_positions_to_bronze.ipynb` | `<cat>.01_bronze.vline_vehicle_positions` | `bronze_vline_vehicle_positions.job.yml` | hourly `periodic` |
| Bronze (static) | `01_bronze/01_raw_schedule_to_bronze.ipynb` | `<cat>.01_bronze.<file>` (11: stops, routes, trips, …) | `weekly_schedule_download.job.yml` (`for_each`) | after `extract` task |
| Silver (stops) | `02_silver/02_bronze_stops_to_silver.ipynb` | `<cat>.02_silver.stops` (SCD2) | `silver_stops.job.yml` | `table_update` on `01_bronze.stops` |
| Silver (VP) | `02_silver/02_bronze_vehicle_positions_to_silver.ipynb` | `<cat>.02_silver.vehicle_positions` | `silver_vehicle_positions.job.yml` | `table_update` on `01_bronze.vline_vehicle_positions` |
| Silver (dims) | `02_silver/02_schedule_dims_to_silver.ipynb` | `<cat>.02_silver.{routes,trips,calendar,calendar_dates,stop_times,transfers}` | `silver_schedule_dims.job.yml` | `table_update` on those bronze tables |
| Gold (perf) | `03_gold/02_fct_service_performance.ipynb` | `<cat>.03_gold.fct_service_performance` | `gold_service_performance.job.yml` | `table_update` on `02_silver.trip_updates` |

- **Bronze (protobuf)** = raw/lossless: Auto Loader (`cloudFiles.format=binaryFile`) stores the raw
  `.pb` bytes in a `content` binary column (+ `path`, `modificationTime`, `length`, `_ingest_ts`).
- **Bronze (static CSV)** = one **parameterized** notebook driven by a `filename` widget; `table_name`,
  checkpoint, path glob, and lineage regex all derive from it. The weekly job fans out over the 11
  GTFS `.txt` files with a `for_each` task (one parallel iteration per file). Adds `feed_id`,
  `export_date`, `_source_file`, `_ingest_ts`. Raw strings — cast in silver.
- **Silver (trip updates)** = typed, one row per `stop_time_update`: streaming read of bronze →
  `from_protobuf` (compiled `.desc`, JVM-side) → explode `entity[] → stop_time_update[]` → typed
  columns + `event_time = coalesce(arrival, departure)`. Keeps every `FULL_DATASET` snapshot.
- **Silver (stops)** = **SCD2 dimension** (streaming read of bronze → `foreachBatch` MERGE). Key
  `(feed_id, stop_id)`; change-hash `xxhash64` over the 9 source attributes (name, lat, lon, url,
  location_type, parent_station, wheelchair_boarding, level_id, platform_code); `valid_from/valid_to`
  from `export_date` (half-open, `is_current` true = open). Processes export_dates in order so a
  backfill rebuilds full history. **No delete handling** — a stop dropping from a feed leaves its
  current row open (safe against partial/skipped extracts).

## Gotchas (learned the hard way)

**Databricks Connect (VS Code) vs serverless jobs**
- Under Connect, Python runs **locally in `.venv`**; only `spark`/`dbutils` are remote proxies.
- `%pip` / `dbutils.library.restartPython()` **fail** under Connect (needs `cluster_id`; uv venv has no `pip`).
  Add libs with `uv add` locally, or the **job `environments`** block for serverless.
- **Structured Streaming does NOT run over Spark Connect** → run streaming notebooks as serverless
  **jobs**, not interactively in VS Code. Use a **batch** read for interactive exploration.
- **UDFs execute on executors** → any imported package must be in the serverless environment (this is why
  a `MessageToJson` UDF threw `ModuleNotFoundError: google.transit`). `from_protobuf` avoids it entirely.
- `mode: development` (dev target) **auto-pauses triggers** → jobs won't fire on schedule/table-update in
  dev. Test with `databricks bundle run <job> -t dev`.
- **`input_file_name()` is unsupported in Unity Catalog** → use the `_metadata.file_path` column instead
  (`col("_metadata.file_path")`) for source-file lineage.
- **Heterogeneous CSVs across feeds** (e.g. only feed 2's `stops.txt` has `platform_code`): the static
  bronze reads with `header=True` (map by name) + `cloudFiles.schemaEvolutionMode=addNewColumns` +
  write `mergeSchema=true`. Missing columns null-fill; a feed introducing a new column **fails the
  stream once and self-heals on restart** → the `for_each` bronze task must have **retries**. The
  notebook never names source columns, so it's schema-agnostic across all 11 files.
- **SCD2 change-hash must exclude pipeline columns:** hash only source attributes. `_ingest_ts` /
  `_source_file` change every export, so hashing them opens a spurious new version every run. Use
  `xxhash64` (null-safe) so feeds missing a column (null) don't collide or throw.

**GTFS-RT data quirks**
- Protobuf 64-bit ints (timestamps/`time`) are **epoch seconds**; `from_protobuf` yields them as `LONG`
  (cast → timestamp). The JSON/`MessageToJson` path renders them as **strings** instead.
- `start_date` = `yyyyMMdd` string; `start_time` **can exceed `24:00:00`** → keep as string.
- `arrival`, `departure`, and `delay` are **independently optional** (nullable); `delay` is **signed**.
- `stop_sequence` is **sparse/non-contiguous** within a trip. `entity.id` = `"{trip_id}|{start_date}"`.
- `schedule_relationship` (proto2) is null when unset → coalesce to `SCHEDULED`.

**Azure Functions / infra**
- **Never host the Functions runtime on the ADLS Gen2 (HNS) data lake.** `AzureWebJobsStorage` needs
  blob+queue+table; HNS accounts aren't a supported host store and it drops the data lake's account
  **key** into app settings (undercuts the managed identity). Runtime → `transportvictoriastorage`
  (non-HNS); data → `transportvicdatalake` via managed identity + Storage Blob Data Contributor.
- **Bicep `siteConfig.appSettings` is a full declaration** → a redeploy overwrites/strips app settings
  edited in the portal. Keep `infra/main.parameters.json` the source of truth.
- Vehicle-positions feed caches 30s (rate limit ~20–27/min); poll ≤ that = duplicate `.pb`. Bronze
  lands everything; dedup on `header.timestamp` happens downstream (silver), not in the function.

## Status

Done:
- [x] Trip updates: bronze + silver (`from_protobuf`) + jobs
- [x] Catalog parametrised for dev/prod
- [x] Static schedule: weekly download + extract + **parameterized bronze fan-out** (11 files) + jobs
- [x] **Stops silver SCD2** dimension + `table_update` job
- [x] Vehicle positions: ingestion timer (2 min) added + bronze notebook/job built

Pending (next: **silver + gold tables**):
- [ ] **Deploy actions**: (a) `az deployment` to move Functions runtime → `transportvictoriastorage`
      (params changed, not yet applied); (b) `databricks bundle deploy -t prod` so schedules/triggers
      actually fire (dev auto-pauses). VP bronze has **no data** until the Function redeploys.
- [ ] **`dim_mode`** reference seed + loader (`feed_id` → mode). Known: 1 Regional Train, 2 Metro Train,
      3 Metro Tram, 4 Myki Bus, 5 Regional Coach, 6 Regional Bus, 10 Interstate, 11 SkyBus (7–9 absent).
      Category filled only for feeds being analysed; feed 10 deferred (verify via `agency.txt`).
- [x] **Vehicle positions silver** validated against real data — lat/lon/bearing/`vehicle.trip` all 100%
      populated (per-line join works). V/Line does NOT send `license_plate`/`occupancy_status`/`congestion_level`
      (dropped); `current_status` is always `IN_TRANSIT_TO` so far (kept, may vary over more snapshots).
- [~] **Schedule dims silver** drafted (`02_schedule_dims_to_silver.ipynb` + `silver_schedule_dims.job.yml`):
      routes, trips, calendar, calendar_dates, stop_times, transfers — typed current-snapshot, **pending
      validation against real bronze schemas**. Remaining static files (agency, levels, pathways, shapes) as-needed.
- [x] **Gold** — `fct_service_performance` (collapse `FULL_DATASET` → final delay per served stop +
      on-time/severe/terminus flags). **Plan: [docs/GOLD_PLAN.md](docs/GOLD_PLAN.md)**.
- [x] **Dashboard** — Streamlit reading a Parquet snapshot of the gold fact, live on Railway
      (`asia-southeast1`), auto-deployed from GitHub. Code + refresh flow: [dashboard/](dashboard/README.md).
- [ ] Next: Q2 cancellation anti-join (`calendar`/`trips` scheduled-but-absent), `dim_date` peak/weekday,
      vehicle-position map, dashboard cosmetic polish + line-name join in the export.

## Commands

```bash
databricks bundle validate -t dev                 # from transport_victoria_databricks/
databricks bundle deploy   -t dev
databricks bundle run bronze_vline_trip_updates      -t dev
databricks bundle run silver_trip_updates            -t dev
databricks bundle run weekly_schedule_download       -t dev   # download → extract → for_each bronze (11 files)
databricks bundle run silver_stops                   -t dev   # SCD2 stops (won't auto-fire in dev)
databricks bundle run bronze_vline_vehicle_positions -t dev
```

Azure Function (ingestion), from `infra/`:

```bash
az deployment group create -g transport-victoria-rg \
  --template-file infra/main.bicep --parameters infra/main.parameters.json
```
