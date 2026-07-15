# Transport Victoria — GTFS-Realtime Data Platform

Ingests V/Line GTFS-Realtime feeds (Public Transport Victoria open data) into a Databricks
medallion lakehouse for delay/positional analytics.

> Detailed rationale for every choice below lives in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).
> This file is the quick operational reference; keep it current as the pipeline evolves.

## Architecture (end to end)

```
Azure Function (timer)  →  ADLS landing (.pb bytes)  →  Bronze (raw)  →  Silver (typed)  →  Gold
  ingestion/                abfss://lakehouse@         Auto Loader      from_protobuf      (pending)
  function_app.py           transportvicdatalake       binaryFile       explode→typed
```

- **Ingestion** — `ingestion/function_app.py`, Azure Functions (Python). Timer-triggered; downloads
  GTFS-RT protobuf and uploads to ADLS. Auth: `KeyId: TRANSPORT_VIC_API_KEY` header to the API;
  `GTFS_CONTAINER_SAS_URL` + `DefaultAzureCredential` to storage.
  - Trip updates: every 5 min (`"0 */5 * * * *"`) → `landing/vline_trip_updates/date=YYYYMMDD/vline_tu_*.pb`.
- **Storage** — ADLS Gen2 account `transportvicdatalake`, container `lakehouse`.
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
| Bronze | `01_bronze/01_raw_protobuff_to_bronze.ipynb` | `<cat>.01_bronze.bronze_vline_trip_updates` | `bronze_vline_trip_updates.job.yml` | daily `periodic` |
| Silver | `02_silver/02_bronze_to_silver.ipynb` | `<cat>.02_silver.trip_updates` | `silver_trip_updates.job.yml` | `table_update` on bronze |

- **Bronze** = raw/lossless: Auto Loader (`cloudFiles` + `cloudFiles.format=binaryFile`) stores the raw
  `.pb` bytes in a `content` binary column (+ `path`, `modificationTime`, `length`, `_ingest_ts`).
- **Silver** = typed, one row per `stop_time_update`: streaming read of bronze → `from_protobuf` (uses a
  compiled `.desc` descriptor, JVM-side) → explode `entity[] → stop_time_update[]` → typed columns +
  `event_time = coalesce(arrival, departure)`. Keeps every `FULL_DATASET` snapshot (time series).

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
- **Union heterogeneous CSVs by name, not position:** read each with `header=True` then
  `unionByName(..., allowMissingColumns=True)` (null-fills missing cols). A fixed positional schema
  mis-maps files whose columns differ in count/order. Used for the multi-feed GTFS static `stops.txt`.

**GTFS-RT data quirks**
- Protobuf 64-bit ints (timestamps/`time`) are **epoch seconds**; `from_protobuf` yields them as `LONG`
  (cast → timestamp). The JSON/`MessageToJson` path renders them as **strings** instead.
- `start_date` = `yyyyMMdd` string; `start_time` **can exceed `24:00:00`** → keep as string.
- `arrival`, `departure`, and `delay` are **independently optional** (nullable); `delay` is **signed**.
- `stop_sequence` is **sparse/non-contiguous** within a trip. `entity.id` = `"{trip_id}|{start_date}"`.
- `schedule_relationship` (proto2) is null when unset → coalesce to `SCHEDULED`.

## Status

- [x] Bronze pipeline + daily job
- [x] Silver pipeline (`from_protobuf` decode) + table-update-triggered job
- [x] Catalog parametrised for dev/prod
- [ ] **Gold** — "latest predicted arrival/delay per stop" (collapse `FULL_DATASET` snapshots)
- [ ] **Vehicle positions pipeline** (new): ingestion (~30s poll, dedup on `header.timestamp`) → bronze → silver.
      Endpoint (to confirm): `.../gtfs/realtime/v1/vline/vehicle-positions`

## Commands

```bash
databricks bundle validate -t dev                 # from transport_victoria_databricks/
databricks bundle deploy   -t dev
databricks bundle run bronze_vline_trip_updates -t dev
databricks bundle run silver_trip_updates       -t dev
```
