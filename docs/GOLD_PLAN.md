# Gold Layer Plan — V/Line Service Performance

> Working plan for the gold layer. Executive lens: **is V/Line reliable and trustworthy?**
> Scope v1 = V/Line only (extend per line/mode later). Pairs with [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)
> and the pipeline reference in [../CLAUDE.md](../CLAUDE.md).

## Framing

"Reliable and trustworthy" decomposes into measurable dimensions:

| Dimension | Exec question | Metric |
|---|---|---|
| Punctuality | Are we on time? | % arriving within ~6 min of schedule |
| Reliability | Does the promised service run? | % scheduled trips delivered (not cancelled) |
| Severity | When we fail, how badly? | delay median / p95 / % severe (>15 min) |
| Hotspots | Where do we focus? | worst lines / stops / O-D segments |
| Temporal | Do we fail when it matters? | peak vs off-peak, weekday vs weekend |
| Consistency | Can passengers plan around us? | variance/spread of delay per line |

Anchor: V/Line's two official published KPIs are **punctuality** (arrival within 5 min 59 sec) and
**reliability** (% of timetabled services delivered). Mirroring these makes the output credible.

## Business questions

1. **On-time performance** — share of services arriving within ~6 min of schedule, overall and by line. *(headline punctuality)*
2. **Reliability / cancellations** — proportion of scheduled trips that ran vs were cancelled/skipped. *(headline reliability)*
3. **Delay severity** — for late services, median / p95 delay and % "severe" (>15 min).
4. **Hotspots** — which lines, stops, and origin→destination segments drag performance down.
5. **Temporal** — is reliability worse in commuter peaks / on weekdays?
6. **Consistency** — how predictable is each line (delay variance), not just the average.

Stretch (later):
- **Recovery** — when a train starts late, does it recover by the destination or does delay compound? *(uses the pre-collapse prediction snapshots)*
- **Ghost services** — do scheduled services actually have a vehicle running them? *(needs vehicle-positions silver)*

## Data reality & caveats

- **GTFS-RT `delay` is already actual − scheduled** (signed seconds) → basic punctuality (Q1, Q3, Q5, Q6)
  needs **no `stop_times` join**; it's `delay <= 359`. `delay` is **nullable** → filter/handle.
- **Punctuality "at destination"** = terminus stop per trip (max `stop_sequence`), matching V/Line's method.
- **Collapse the FULL_DATASET snapshots first**: silver keeps every prediction; gold wants the **final
  observed** delay per `(service_date, trip_id, stop)`. This collapse is the core gold transform.
  (Recovery is the one question that *keeps* the snapshots.)
- **True cancellations (Q2) need the static schedule**: explicit ones = `schedule_relationship = CANCELED`;
  a service that simply never appears needs `calendar`→scheduled-trips **minus** what appeared in RT.
  This is the only question that genuinely needs `trips`/`calendar`/`routes` joined.
- **Only a few days of data** → cross-sectional (by line/time/stop), not long-term trend.

## Gold design — one star, not one-table-per-question

Avoid baking a table per question (doesn't scale). Build one fact + conformed dims; KPIs are slices.

**Fact — `gold.fct_service_performance`**, grain `(service_date, trip_id, stop_sequence)`:
- `arrival_delay` (final observed, seconds), `departure_delay`
- `on_time` (delay <= 359s), `severe` (delay > 900s)
- `is_terminus`, `cancelled`
- FKs: `feed_id`/line, `route_id`, `stop_id`, `service_date`
- Built by: collapse snapshots → last value per `(service_date, trip_id, stop)` → join static for Q2/names.

**Conformed dimensions:**
- `dim_mode` — `feed_id` → line/mode (the pending seed; see CLAUDE.md Status).
- `dim_stop` — the SCD2 `02_silver.stops`.
- `dim_route` — from `routes`.
- `dim_date` / day-type — peak flag, weekday/weekend.

**KPIs** (Power BI measures or thin `gold.kpi_*` views over the fact): OTP%, reliability%, delay
percentiles, all group-by slices. Q1/Q3/Q4/Q5/Q6 fall out of the fact directly; Q2 adds the
scheduled-but-absent anti-join; recovery reads pre-collapse snapshots.

## Open decisions / next steps

- [ ] Confirm the question shortlist (all 6, or a tighter 3 to start).
- [ ] Lock definitions: on-time threshold (359s?), severe threshold (900s?), destination-only vs all-stops.
- [ ] Build `dim_mode` seed (prerequisite for per-line grouping).
- [ ] Detail the snapshot-collapse logic for `fct_service_performance` (the foundation).
- [ ] Decide Q2 cancellation approach (explicit CANCELED only, or full calendar anti-join).
- [ ] Prereqs from CLAUDE.md Status: vehicle-positions silver (for ghost services), prod deploy so data flows.
