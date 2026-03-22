"""
validate_gtfs.py
----------------
Validates both Delhi bus and metro GTFS feeds using gtfs-kit.
Writes a human-readable validation_report.txt.

Usage:
    python validate_gtfs.py

Requires:
    pip install gtfs-kit pandas
"""

import gtfs_kit as gk
import pandas as pd
from pathlib import Path
from datetime import datetime
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

FEEDS = {
    "bus":   "data/raw/bus",
    "metro": "data/raw/metro",
}

REPORT_PATH = "data/validation_report.txt"

# Delhi bounding box (lat/lon) — used to sanity-check stop coordinates
DELHI_BBOX = {
    "lat_min": 28.40,
    "lat_max": 28.88,
    "lon_min": 76.84,
    "lon_max": 77.35,
}


def load_feed(name: str, path: str):
    """Load a GTFS feed using gtfs-kit. Returns feed or None."""
    log.info(f"[{name}] Loading feed from {path}/")
    try:
        feed = gk.read_feed(path, dist_units="km")
        log.info(f"[{name}] Feed loaded.")
        return feed
    except Exception as e:
        log.error(f"[{name}] Failed to load feed: {e}")
        return None


def run_gtfskit_validation(name: str, feed) -> pd.DataFrame:
    """Run gtfs-kit built-in validation. Returns DataFrame of issues."""
    log.info(f"[{name}] Running gtfs-kit validation...")
    try:
        issues = feed.validate()
        if issues.empty:
            log.info(f"[{name}] No gtfs-kit issues found.")
        else:
            errors   = issues[issues["type"] == "error"]
            warnings = issues[issues["type"] == "warning"]
            log.info(f"[{name}] {len(errors)} errors, {len(warnings)} warnings.")
        return issues
    except Exception as e:
        log.error(f"[{name}] Validation failed: {e}")
        return pd.DataFrame()


def check_stops_bbox(name: str, feed) -> list:
    """Check that all stops are within Delhi bounding box."""
    issues = []
    if feed.stops is None or feed.stops.empty:
        issues.append("stops.txt is empty or missing")
        return issues

    stops = feed.stops.copy()
    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")

    outside = stops[
        (stops["stop_lat"] < DELHI_BBOX["lat_min"]) |
        (stops["stop_lat"] > DELHI_BBOX["lat_max"]) |
        (stops["stop_lon"] < DELHI_BBOX["lon_min"]) |
        (stops["stop_lon"] > DELHI_BBOX["lon_max"]) |
        (stops["stop_lat"].isna()) |
        (stops["stop_lon"].isna())
    ]

    if not outside.empty:
        issues.append(f"{len(outside)} stops outside Delhi bounding box or with null coords")
        log.warning(f"[{name}] {len(outside)} stops outside bbox — sample:")
        log.warning(outside[["stop_id", "stop_name", "stop_lat", "stop_lon"]].head(5).to_string())
    else:
        log.info(f"[{name}] All {len(stops)} stops within Delhi bounding box. ✓")

    return issues


def check_stop_times(name: str, feed) -> list:
    """Check stop_times for common Delhi GTFS quirks."""
    issues = []
    if feed.stop_times is None or feed.stop_times.empty:
        issues.append("stop_times.txt is empty or missing")
        return issues

    st = feed.stop_times.copy()

    # Check for times > 24:00:00 (overnight services — valid GTFS but needs handling)
    def parse_seconds(t):
        if pd.isna(t) or not isinstance(t, str):
            return None
        try:
            h, m, s = t.strip().split(":")
            return int(h) * 3600 + int(m) * 60 + int(s)
        except:
            return None

    st["dep_secs"] = st["departure_time"].apply(parse_seconds)
    over24 = st[st["dep_secs"] > 86400]
    if not over24.empty:
        log.info(f"[{name}] {len(over24)} stop_times with departure > 24:00 (overnight services — valid, will be handled in cleaning)")

    # Check for null times
    null_arr = st["arrival_time"].isna().sum()
    null_dep = st["departure_time"].isna().sum()
    if null_arr > 0:
        issues.append(f"{null_arr} null arrival_times in stop_times.txt")
    if null_dep > 0:
        issues.append(f"{null_dep} null departure_times in stop_times.txt")

    # Check stop_sequence ordering
    out_of_order = (
        st.sort_values(["trip_id", "stop_sequence"])
        .groupby("trip_id")["stop_sequence"]
        .apply(lambda x: (x.diff() <= 0).any())
        .sum()
    )
    if out_of_order > 0:
        issues.append(f"{out_of_order} trips with non-monotonic stop_sequence")

    log.info(f"[{name}] stop_times: {len(st)} rows across {st['trip_id'].nunique()} trips")
    return issues


def check_shapes(name: str, feed) -> list:
    """Check shapes.txt for completeness."""
    issues = []
    if feed.shapes is None or feed.shapes.empty:
        log.warning(f"[{name}] shapes.txt missing or empty — route geometry will not be available")
        issues.append("shapes.txt missing — route polylines will not render on map")
        return issues

    log.info(f"[{name}] shapes.txt: {feed.shapes['shape_id'].nunique()} shape IDs")

    # Check how many trips have shapes
    if feed.trips is not None:
        trips_with_shapes = feed.trips["shape_id"].notna().sum()
        total_trips = len(feed.trips)
        pct = trips_with_shapes / total_trips * 100 if total_trips > 0 else 0
        log.info(f"[{name}] {trips_with_shapes}/{total_trips} trips ({pct:.1f}%) have shape_id assigned")
        if pct < 50:
            issues.append(f"Only {pct:.1f}% of trips have shape_id — route geometry will be incomplete")

    return issues


def check_calendar(name: str, feed) -> list:
    """Check calendar.txt for service coverage."""
    issues = []
    if feed.calendar is None or feed.calendar.empty:
        # calendar_dates.txt only is valid GTFS but less common
        if feed.calendar_dates is not None and not feed.calendar_dates.empty:
            log.info(f"[{name}] No calendar.txt but calendar_dates.txt present — date-based service only")
        else:
            issues.append("Both calendar.txt and calendar_dates.txt are missing or empty")
        return issues

    log.info(f"[{name}] calendar.txt: {len(feed.calendar)} service IDs")

    # Check that at least some services run on weekdays
    weekday_cols = ["monday","tuesday","wednesday","thursday","friday"]
    weekday_services = feed.calendar[
        (feed.calendar[weekday_cols] == 1).any(axis=1)
    ]
    log.info(f"[{name}] {len(weekday_services)} service IDs run on at least one weekday")
    if len(weekday_services) == 0:
        issues.append("No weekday services found in calendar.txt")

    return issues


def print_feed_summary(name: str, feed):
    """Print a concise summary of feed contents."""
    log.info(f"\n[{name}] ── Feed summary ──")
    if feed.stops is not None:
        log.info(f"  stops:       {len(feed.stops):,} rows")
    if feed.routes is not None:
        log.info(f"  routes:      {len(feed.routes):,} rows")
    if feed.trips is not None:
        log.info(f"  trips:       {len(feed.trips):,} rows")
    if feed.stop_times is not None:
        log.info(f"  stop_times:  {len(feed.stop_times):,} rows")
    if feed.shapes is not None:
        log.info(f"  shapes:      {len(feed.shapes):,} rows")
    if feed.calendar is not None:
        log.info(f"  calendar:    {len(feed.calendar):,} rows")


def write_report(results: dict, path: str):
    """Write a human-readable validation report."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "=" * 64,
        "  Propus — GTFS Validation Report",
        f"  Generated: {timestamp}",
        "=" * 64,
        "",
    ]

    overall_ok = True
    for name, r in results.items():
        lines.append(f"── {name.upper()} FEED ──")
        if r.get("load_error"):
            lines.append(f"  ✗ LOAD ERROR: {r['load_error']}")
            overall_ok = False
            lines.append("")
            continue

        # gtfs-kit issues
        issues_df = r.get("gtfskit_issues", pd.DataFrame())
        if not issues_df.empty:
            errors   = issues_df[issues_df["type"] == "error"]
            warnings = issues_df[issues_df["type"] == "warning"]
            if not errors.empty:
                lines.append(f"  ✗ {len(errors)} gtfs-kit ERRORS:")
                for _, row in errors.iterrows():
                    lines.append(f"      - {row.get('message', str(row))}")
                overall_ok = False
            if not warnings.empty:
                lines.append(f"  ⚠  {len(warnings)} gtfs-kit warnings (non-blocking)")
                for _, row in warnings.head(5).iterrows():
                    lines.append(f"      - {row.get('message', str(row))}")
        else:
            lines.append("  ✓ gtfs-kit: no issues")

        # Custom checks
        custom_issues = r.get("custom_issues", [])
        blocking = [i for i in custom_issues if not i.startswith("WARNING:")]
        if blocking:
            lines.append(f"  ✗ Custom check issues:")
            for i in blocking:
                lines.append(f"      - {i}")
            overall_ok = False
        else:
            lines.append("  ✓ Custom checks: passed")

        lines.append(f"  Rows: {r.get('summary', '')}")
        lines.append("")

    lines += [
        "=" * 64,
        f"  Overall status: {'✓ PASS — proceed to clean_gtfs.py' if overall_ok else '✗ FAIL — fix errors before proceeding'}",
        "=" * 64,
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"\nValidation report written to: {path}")
    return overall_ok


def main():
    log.info("=" * 60)
    log.info("Propus — GTFS Validation Script")
    log.info("=" * 60)

    results = {}

    for name, path in FEEDS.items():
        log.info(f"\n{'='*20} {name.upper()} {'='*20}")
        r = {}

        feed = load_feed(name, path)
        if feed is None:
            r["load_error"] = f"Could not load feed from {path}"
            results[name] = r
            continue

        print_feed_summary(name, feed)

        r["gtfskit_issues"] = run_gtfskit_validation(name, feed)

        custom_issues = []
        custom_issues.extend(check_stops_bbox(name, feed))
        custom_issues.extend(check_stop_times(name, feed))
        custom_issues.extend(check_shapes(name, feed))
        custom_issues.extend(check_calendar(name, feed))
        r["custom_issues"] = custom_issues

        r["summary"] = (
            f"stops={len(feed.stops) if feed.stops is not None else 0}, "
            f"routes={len(feed.routes) if feed.routes is not None else 0}, "
            f"trips={len(feed.trips) if feed.trips is not None else 0}, "
            f"stop_times={len(feed.stop_times) if feed.stop_times is not None else 0}"
        )

        results[name] = r

    overall_ok = write_report(results, REPORT_PATH)

    if overall_ok:
        log.info("\n✓ Validation passed. Next: python clean_gtfs.py")
        sys.exit(0)
    else:
        log.error("\n✗ Validation failed. Fix errors before proceeding.")
        sys.exit(1)


if __name__ == "__main__":
    main()
