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

## 9. Vehicle positions: 2-min polling, land-everything
**Decision:** Poll vehicle-positions every 2 min (trip updates stay 5 min); land **every** snapshot to
bronze and defer dedup (on `header.timestamp`) downstream, rather than skip-writing at the source.
**Why:** Positions move, so 5 min is too coarse for a usable trail — but the feed caches ~30s, so polling
faster just re-downloads identical data. A stateless function that always lands is simpler and more
robust than one that must remember the last timestamp. Storage is trivial (<$0.10/mo even at 30s).
**Consequences:** Silver keeps the full position time-series (a "latest per vehicle" collapse is a gold
concern). Validated against real data: V/Line populates lat/lon/bearing/`vehicle.trip` (100%) but not
`license_plate`/`occupancy_status`/`congestion_level` — those were dropped from silver.

## 10. `stops` is the only SCD2 (Type-2) dimension
**Decision:** Silver-stops tracks history via SCD2 (`foreachBatch` MERGE): key `(feed_id, stop_id)`, a
null-safe `xxhash64` change-hash over the source attributes, `export_date`-based validity, no delete.
**Why:** Stop attributes (name, platform, coordinates) change rarely but their history has value. The
hash **excludes pipeline columns** (`_ingest_ts`/`_source_file`) or every weekly load would open a
spurious version. No-delete because the extract can skip files — a partial export must not retire a live
stop.
**Consequences:** The other schedule dims are deliberately *not* SCD2 (see #11) — one dimension carries
the Type-2 complexity, the rest stay simple.

## 11. Schedule dims are current-snapshot, not SCD2
**Decision:** `routes`/`trips`/`calendar`/`calendar_dates`/`stop_times`/`transfers` silver = latest
`export_date` only (typed, deduped, overwrite), built by one parameterised notebook.
**Why:** They're joined for names and calendars; SCD2 machinery isn't worth it for reference data. Bronze
retains every export, so a point-in-time join can be added later if it's ever needed.
**Consequences:** A schedule change mid-capture means an older trip-update row joins the newer schedule —
acceptable while V/Line's timetable is stable (it rarely changes). Judgment call: don't over-engineer.

## 12. One parameterised bronze notebook, `for_each` fan-out
**Decision:** All 11 GTFS static files load through a single notebook driven by a `filename` widget,
fanned out by a `for_each` task. The notebook never names source columns (reads by header).
**Why:** DRY — adding a file is one list entry, not a new notebook. Schema-agnostic ingestion:
`schemaEvolutionMode=addNewColumns` + `mergeSchema` + task **retries** absorb a feed introducing a new
column (the stream fails once and self-heals on restart).
**Consequences:** A multi-table `table_update` trigger needs a `condition` — `ANY_UPDATED` +
`wait_after_last_change_seconds`, so the parallel fan-out settles into one silver run and a skipped file
can't stall the trigger.

## 13. Azure Functions runtime kept off the ADLS Gen2 data lake
**Decision:** The Functions host storage (`AzureWebJobsStorage`) is a separate non-HNS account
(`transportvictoriastorage`); the data lake (`transportvicdatalake`, HNS) is reached only via managed
identity + a scoped RBAC role.
**Why:** `AzureWebJobsStorage` needs blob+queue+table and isn't a supported use of an HNS account; and
hosting it on the data lake drops that account's **key** into app settings, undermining the managed
identity. (Diagnosed from a real symptom — the runtime had homesteaded `azure-webjobs-*` containers into
the data lake.)
**Consequences:** Bicep `siteConfig.appSettings` is declarative, so a redeploy overwrites portal edits —
`infra/main.parameters.json` is the source of truth.

## 14. Gold collapses the prediction time-series; classify *after*, never before
**Decision:** `fct_service_performance` = one row per served stop, taking the **latest snapshot per
`(trip, stop)`** (window function). On-time (`≤ 359s`, V/Line's 5:59) / severe (`> 900s`) are derived
**after** the collapse, never as a `WHERE delay > 0` pre-filter.
**Why:** The last snapshot is the firmed-up, ~actual delay. Pre-filtering on delay deletes the on-time
rows, so punctuality would compute to 0% — a subtle, real bug caught while validating a live trip against
the data. `SKIPPED`/cancelled stops are flagged out; the destination (max served `stop_sequence`) is the
headline metric.
**Consequences:** Every KPI is a `GROUP BY` over one fact — no table-per-question sprawl. Caveat: the
"final" value is the true actual only if the feed published the trip through completion.

## 15. Dashboard: static Parquet snapshot + Railway native GitHub deploy
**Decision:** A Streamlit app reads a Parquet export of the gold fact shipped inside the image; deployed
to Railway via native GitHub integration (auto-deploy on push), not a CI token or a live warehouse query.
**Why:** No credentials and no running SQL warehouse on a public app; free to host; can't break when a
warehouse idles or a token expires. The Railway GitHub App connection makes a `RAILWAY_TOKEN` + a GitHub
Action redundant. A live path (read-only Databricks **service principal** + serverless SQL warehouse) is
a one-function swap if ever wanted.
**Consequences:** Refresh = re-export the Parquet + push (a redeploy). Insights render from the loaded
data so they stay correct as the snapshot grows; region `asia-southeast1` (nearest to Melbourne).
