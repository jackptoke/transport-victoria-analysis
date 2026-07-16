"""V/Line On-Time Performance dashboard.

Reads a static snapshot of the gold fact `fct_service_performance` (Parquet) and tells the punctuality
story: headline KPIs -> worst lines -> delay distribution -> peak effect -> written insights.

Pure Streamlit + Plotly (no HTML). Deploys to Railway as a Python service. Refresh the story by
re-exporting the gold table to data/fct_service_performance.parquet (see README).
"""
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# --- constants (keep ON_TIME_SEC / SEVERE_SEC in sync with the gold notebook) ---------------------
ON_TIME_SEC, SEVERE_SEC = 359, 900
PEAK_HOURS = {7, 8, 16, 17}                 # commuter peaks (local)
MIN_TRIPS = 5                               # hide lines with too few trips to rank fairly

# One primary hue for magnitude; status colours used sparingly, always with a label.
PRIMARY, INK, MUTED, GRID = "#0e7490", "#1f2937", "#6b7280", "#e5e7eb"
GOOD, BAD = "#15803d", "#b91c1c"

DATA = Path(__file__).parent / "data" / "fct_service_performance.parquet"

st.set_page_config(page_title="V/Line On-Time Performance", page_icon="🚆", layout="wide")


@st.cache_data
def load_data(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["service_date"] = pd.to_datetime(df["service_date"])
    df["event_time"] = pd.to_datetime(df["event_time"])
    # Prefer the human line name (route_long_name, if the export joined it); else a cleaned code
    # from the route_id (e.g. "aus:vic:vic-01-BDE:" -> "01-BDE").
    code = (df["route_id"].fillna("unknown")
            .str.replace("aus:vic:vic-", "", regex=False).str.strip(":"))
    if "route_long_name" in df.columns:
        name = df["route_long_name"]
        df["line"] = name.where(name.notna() & (name.str.strip() != ""), code)
    else:
        df["line"] = code
    df["hour"] = df["event_time"].dt.hour
    # Booleans that are never null on the fact.
    for c in ("is_terminus", "served", "cancelled"):
        df[c] = df[c].fillna(False).astype(bool)
    # on_time / severe are null when a stop isn't measurable -> map to 1/0/NaN so mean() = a rate.
    for c in ("on_time", "severe"):
        df[c] = df[c].map({True: 1.0, False: 0.0})
    return df


def style(fig, height=360):
    fig.update_layout(template="simple_white", height=height, showlegend=False,
                      margin=dict(l=10, r=10, t=34, b=10),
                      font=dict(color=INK, size=13), hoverlabel=dict(font_size=13))
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


if not DATA.exists():
    st.error("No data snapshot found at `data/fct_service_performance.parquet`. "
             "Export the gold table there — see the dashboard README.")
    st.stop()

df = load_data(DATA)
dest = df[df["is_terminus"] & df["on_time"].notna()].copy()     # one row per trip (its destination)
served = df[df["served"] & df["delay_sec"].notna()].copy()      # every served stop

# --- header --------------------------------------------------------------------------------------
d0, d1 = df["service_date"].min().date(), df["service_date"].max().date()
st.title("🚆 V/Line On-Time Performance")
st.caption(f"Regional-train punctuality from GTFS-Realtime trip updates &nbsp;·&nbsp; {d0} → {d1} "
           f"&nbsp;·&nbsp; {dest['entity_id'].nunique():,} trips analysed", unsafe_allow_html=True)

# --- headline KPIs (hero numbers, not charts) ----------------------------------------------------
otp = dest["on_time"].mean() * 100
median_delay = dest["delay_sec"].median() / 60
p95_delay = dest["delay_sec"].quantile(0.95) / 60
severe_rate = dest["severe"].mean() * 100

k1, k2, k3, k4 = st.columns(4)
k1.metric("On-time (≤ 6 min)", f"{otp:.1f}%")
k2.metric("Median delay", f"{median_delay:+.1f} min")
k3.metric("95th-pct delay", f"{p95_delay:.0f} min")
k4.metric("Severe (> 15 min)", f"{severe_rate:.1f}%")
st.divider()

# --- OTP by line (magnitude, sorted, single hue, direct-labelled) --------------------------------
st.subheader("Which lines are least reliable?")
by_line = (dest.groupby("line")
           .agg(otp=("on_time", "mean"), trips=("entity_id", "nunique"),
                median_delay_min=("delay_sec", lambda s: s.median() / 60))
           .reset_index())
by_line["otp"] *= 100
by_line = by_line[by_line["trips"] >= MIN_TRIPS].sort_values("otp")

if by_line.empty:
    st.info(f"Not enough data yet — no line has ≥ {MIN_TRIPS} trips in this snapshot.")
else:
    fig = px.bar(by_line, x="otp", y="line", orientation="h",
                 text=by_line["otp"].map("{:.0f}%".format),
                 hover_data={"trips": True, "median_delay_min": ":.1f", "otp": ":.1f"})
    fig.update_traces(marker_color=PRIMARY, textposition="outside", cliponaxis=False)
    fig.add_vline(x=otp, line_dash="dash", line_color=MUTED,
                  annotation_text=f"network {otp:.0f}%", annotation_position="top right")
    fig.update_layout(xaxis_title="On-time %", yaxis_title=None, xaxis_range=[0, 108])
    st.plotly_chart(style(fig, height=max(320, 30 * len(by_line))), use_container_width=True)
    st.caption(f"Destination arrival within {ON_TIME_SEC // 60} min of schedule. "
               f"Lines with fewer than {MIN_TRIPS} trips are hidden.")

# --- delay distribution + peak effect, side by side ----------------------------------------------
left, right = st.columns(2)

with left:
    st.subheader("When trains are late, how late?")
    dmin = dest.assign(delay_min=dest["delay_sec"] / 60)
    fig = px.histogram(dmin, x="delay_min", nbins=40)
    fig.update_traces(marker_color=PRIMARY)
    fig.add_vline(x=median_delay, line_color=GOOD, annotation_text="median")
    fig.add_vline(x=p95_delay, line_color=BAD, annotation_text="p95")
    fig.update_layout(xaxis_title="Delay at destination (min)", yaxis_title="trips")
    st.plotly_chart(style(fig), use_container_width=True)

with right:
    st.subheader("Does reliability sag in the peaks?")
    by_hour = (dest.groupby("hour").agg(otp=("on_time", "mean"),
                                        trips=("entity_id", "nunique")).reset_index())
    by_hour["otp"] *= 100
    fig = px.bar(by_hour, x="hour", y="otp", hover_data={"trips": True, "otp": ":.1f"})
    fig.update_traces(marker_color=PRIMARY)
    for lo in (7, 16):                      # shade the AM/PM peak windows
        fig.add_vrect(x0=lo - 0.5, x1=lo + 1.5, fillcolor=MUTED, opacity=0.10, line_width=0)
    fig.update_layout(xaxis_title="Hour of day", yaxis_title="On-time %",
                      yaxis_range=[0, 105], xaxis=dict(dtick=2))
    st.plotly_chart(style(fig), use_container_width=True)

# --- the story: written insights, computed from the data -----------------------------------------
st.divider()
st.subheader("📌 What the data says")

peak = dest[dest["hour"].isin(PEAK_HOURS)]["on_time"].mean() * 100
offpeak = dest[~dest["hour"].isin(PEAK_HOURS)]["on_time"].mean() * 100
insights = [f"Across **{dest['entity_id'].nunique():,} trips** ({d0} → {d1}), "
            f"**{otp:.0f}% arrived on time** (within {ON_TIME_SEC // 60} min at destination), "
            f"with a **median delay of {median_delay:+.1f} min**."]
if not by_line.empty:
    w, b = by_line.iloc[0], by_line.iloc[-1]
    insights.append(f"**{w['line']}** is the least reliable line at **{w['otp']:.0f}% on time** "
                    f"(median {w['median_delay_min']:+.1f} min over {int(w['trips'])} trips); "
                    f"**{b['line']}** is the best at **{b['otp']:.0f}%**.")
if pd.notna(peak) and pd.notna(offpeak):
    gap = offpeak - peak
    verb = "worse" if gap > 0 else "better"
    insights.append(f"Peak-hour services run **{abs(gap):.0f} pts {verb}** than off-peak "
                    f"({peak:.0f}% vs {offpeak:.0f}%) — {'reliability sags when demand is highest' if gap > 0 else 'the peaks hold up'}.")
insights.append(f"The tail is the real pain: the **worst 5%** of trips arrive **{p95_delay:.0f}+ min late**, "
                f"and **{severe_rate:.0f}%** are severely late (> {SEVERE_SEC // 60} min).")

for line in insights:
    st.markdown(f"- {line}")

st.caption("Source: Public Transport Victoria / V/Line GTFS-Realtime open data. "
           "Delay = actual − scheduled from the feed; final value per stop is the last snapshot before arrival.")
