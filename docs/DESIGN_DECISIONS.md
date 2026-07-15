# Design Decisions

Rationale behind the major choices. Newest decisions at the bottom. Keep entries short:
**Decision → Why → Consequences**. Operational summary lives in the root [CLAUDE.md](../CLAUDE.md).

---

## 1. Bronze stores raw protobuf bytes (not decoded JSON)
**Decision:** Bronze ingests the `.pb` files as raw `binary` (`content` column) via Auto Loader
`cloudFiles.format=binaryFile`; no decoding in bronze.
**Why:** Lossless and replayable — if the decode logic changes or has a bug, re-run silver against
untouched bronze without re-fetching from the API. Protobuf is not self-describing, so "raw" = bytes.
**Consequences:** Bronze isn't directly queryable; decoding is a silver concern. Each row = one feed
snapshot (one file).

## 2. Decode with `from_protobuf`, not a Python UDF
**Decision:** Silver decodes using Spark's native `from_protobuf` against a compiled descriptor (`.desc`).
**Why:** A `MessageToJson` Python UDF runs on **executors**, which on serverless lack
`gtfs-realtime-bindings` → `ModuleNotFoundError: google.transit`. `from_protobuf` decodes in the JVM,
needs no executor Python package, produces a **typed struct**, and enforces the schema at decode.
**Consequences:** One-time step to compile the descriptor. Types are correct out of the box (timestamps
as `LONG`, delays as `INT`, enums as strings) — no string-casting like the JSON path needs.

## 3. Descriptor generated once from the installed bindings → cached in a Volume
**Decision:** Cell 4 of the silver notebook builds a `FileDescriptorSet` from `gtfs_realtime_pb2` and
writes `gtfs_realtime.desc` to `/Volumes/<catalog>/02_silver/artifacts/`, guarded by `os.path.exists`.
**Why:** Avoids needing `protoc` or the `.proto` file; self-contained. After first run the bindings are
never imported again (the hot path is pure `from_protobuf`).
**Consequences:** The **first** run needs `gtfs-realtime-bindings` in the job environment (wired into
`silver_trip_updates.job.yml`). The descriptor is `transit_realtime.FeedMessage`, single self-contained
proto file (~8 KB).

## 4. Silver grain = one row per `stop_time_update`; keep all snapshots
**Decision:** Explode `entity[] → stop_time_update[]`; retain every feed snapshot (append-only).
**Why:** Finest useful grain (a predicted arrival/departure at a stop). The feed is `FULL_DATASET`, so
each poll re-publishes every trip — retaining snapshots preserves the *time series* of how a prediction
firmed up. Collapsing to "latest" loses that.
**Consequences:** Silver grows with every poll. Natural key `(feed_ts, entity_id, stop_sequence)`.
"Latest per stop" is a **gold** concern. Append is idempotent via the streaming checkpoint (no MERGE).

## 5. Numbered medallion schemas (`01_bronze`, `02_silver`, `03_gold`)
**Decision:** Schema names carry a numeric prefix.
**Why:** Sort in pipeline order in Catalog Explorer instead of alphabetically.
**Consequences:** Leading digit ⇒ identifiers **must be backtick-quoted** in SQL / `.toTable()`.
Backticks are for the SQL parser only — never in `/Volumes/...` paths (they become literal characters).

## 6. Catalog parametrised per environment
**Decision:** dev `transport_vic_dev` / prod `transport_vic_prod` via `var.catalog` in `databricks.yml`.
Notebooks read a `catalog` widget (default `transport_vic_dev` for interactive runs); jobs pass a
`catalog` parameter defaulting to `${var.catalog}`. Schemas, volumes, and ADLS storage are identical
across environments — only the catalog changes.
**Why:** Single source of truth; the prod name is never typed in code. `-t dev`/`-t prod` selects it.
**Consequences:** Widget name must match the job parameter name (`catalog`). Silver's `table_update`
trigger also uses `${var.catalog}` so it watches the right bronze table per target.

## 7. Liquid clustering over hive partitioning; no manual OPTIMIZE (yet)
**Decision:** Silver uses `CLUSTER BY (start_date, route_id)`. No scheduled `OPTIMIZE`.
**Why:** Volume is low (trip-updates ~a few hundred Delta files/year) and there are no joins — hive
partitioning would just create tiny files. At this scale compaction buys little; access is full-scan.
**Consequences:** Revisit if ingestion frequency rises sharply. Prefer Predictive Optimization over
hand-scheduled OPTIMIZE. If a downstream stream reads a table that later gets OPTIMIZE'd, it needs
`skipChangeCommits=true` (already set on the silver→bronze read).

## 8. Loosely-coupled jobs (table-update trigger, not `depends_on`)
**Decision:** Bronze runs on a daily `periodic` trigger; silver runs on a `table_update` trigger on the
bronze table (debounced `min_time_between_triggers_seconds: 60`).
**Why:** Independently deployable/runnable layers; silver reacts to bronze committing new rows.
**Consequences:** In dev the trigger is auto-paused (development mode) → run silver manually while
iterating. Streaming jobs use `max_concurrent_runs: 1` to avoid checkpoint collisions.

## 9. (Planned) Vehicle positions polling cadence
**Decision (proposed):** Poll the vehicle-positions endpoint every ~30s during service hours, with
skip-write dedup keyed on `header.timestamp`.
**Why:** Positions move (unlike slowly-evolving trip-update delays), so 5-min is too coarse; but never
poll faster than the producer refreshes (verify via `header.timestamp` deltas). Skip-write decouples
poll rate from write rate and avoids duplicate identical snapshots.
**Consequences:** Sub-minute Azure Functions timer (`"*/30 * * * * *"`). Confirm DataVic rate limits.
Not yet implemented.
