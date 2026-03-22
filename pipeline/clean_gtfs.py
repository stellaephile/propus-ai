"""
clean_gtfs.py
-------------
Cleans and normalises both Delhi bus and metro GTFS feeds.
Outputs clean CSVs to data/processed/ ready for PostGIS ingestion.

Key issues handled:
  - UTF-8 / encoding problems in stop names (common in Delhi GTFS)
  - Stop coordinates outside Delhi bounding box
  - Duplicate stop IDs (prefixed with feed name to avoid conflicts)
  - GTFS times > 24:00:00 (overnight services)
  - Missing / null values in required fields
  - Inconsistent route type codes

Usage:
    python clean_gtfs.py

Outputs:
    data/processed/bus/stops.csv
    data/processed/bus/routes.csv
    data/processed/bus/trips.csv
    data/processed/bus/stop_times.csv
    data/processed/bus/shapes.csv       (if present)
    data/processed/bus/calendar.csv
    data/processed/metro/...            (same structure)
"""

import pandas as pd
import numpy as np
from pathlib import Path
import unicodedata
import logging
import re

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

FEEDS = {
    "bus":   {"in": "data/raw/bus",   "out": "data/processed/bus"},
    "metro": {"in": "data/raw/metro", "out": "data/processed/metro"},
}

DELHI_BBOX = {
    "lat_min": 28.2, "lat_max": 29.2,
    "lon_min": 76.5, "lon_max": 77.8,
}

# Route types per GTFS spec
VALID_ROUTE_TYPES = {0, 1, 2, 3, 4, 5, 6, 7}  # 1=metro, 3=bus


def read_gtfs_file(feed_dir: str, filename: str, required: bool = True) -> pd.DataFrame | None:
    """Read a GTFS txt file with UTF-8 fallback to latin-1."""
    path = Path(feed_dir) / filename
    if not path.exists():
        if required:
            log.error(f"Required file missing: {path}")
        else:
            log.warning(f"Optional file not found: {path}")
        return None

    for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        try:
            df = pd.read_csv(path, dtype=str, encoding=encoding)
            df.columns = df.columns.str.strip()
            log.info(f"  Read {filename} ({len(df):,} rows) with {encoding}")
            return df
        except UnicodeDecodeError:
            continue
        except Exception as e:
            log.error(f"  Failed to read {filename}: {e}")
            return None

    log.error(f"  Could not decode {filename} with any known encoding")
    return None


def clean_string(s) -> str:
    """Normalise unicode, strip whitespace, remove control chars."""
    if pd.isna(s):
        return ""
    s = str(s).strip()
    # Normalise unicode (NFC form handles composed characters)
    s = unicodedata.normalize("NFC", s)
    # Remove non-printable control characters
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s


def prefix_ids(df: pd.DataFrame, id_cols: list, prefix: str) -> pd.DataFrame:
    """Prefix all ID columns to avoid bus/metro conflicts when merged."""
    df = df.copy()
    for col in id_cols:
        if col in df.columns:
            df[col] = prefix + "_" + df[col].astype(str)
    return df


def clean_stops(df: pd.DataFrame, feed_name: str) -> pd.DataFrame:
    log.info(f"  Cleaning stops ({len(df)} rows)...")
    df = df.copy()

    # Clean string fields
    df["stop_name"] = df["stop_name"].apply(clean_string)
    if "stop_desc" in df.columns:
        df["stop_desc"] = df["stop_desc"].apply(clean_string)

    # Numeric coordinates
    df["stop_lat"] = pd.to_numeric(df["stop_lat"], errors="coerce")
    df["stop_lon"] = pd.to_numeric(df["stop_lon"], errors="coerce")

    # Drop stops outside Delhi bbox or with null coords
    before = len(df)
    df = df[
        df["stop_lat"].between(DELHI_BBOX["lat_min"], DELHI_BBOX["lat_max"]) &
        df["stop_lon"].between(DELHI_BBOX["lon_min"], DELHI_BBOX["lon_max"])
    ]
    dropped = before - len(df)
    if dropped:
        log.warning(f"  Dropped {dropped} stops outside Delhi bbox")

    # Drop duplicate stop_ids
    before = len(df)
    df = df.drop_duplicates(subset=["stop_id"], keep="first")
    dupes = before - len(df)
    if dupes:
        log.warning(f"  Dropped {dupes} duplicate stop_ids")

    # Prefix IDs
    df = prefix_ids(df, ["stop_id", "parent_station"], feed_name)

    # Add feed source column — useful for distinguishing bus vs metro later
    df["feed"] = feed_name

    # Ensure location_type is numeric (0=stop, 1=station)
    if "location_type" in df.columns:
        df["location_type"] = pd.to_numeric(df["location_type"], errors="coerce").fillna(0).astype(int)
    else:
        df["location_type"] = 0

    log.info(f"  stops clean: {len(df)} rows")
    return df


def clean_routes(df: pd.DataFrame, feed_name: str) -> pd.DataFrame:
    log.info(f"  Cleaning routes ({len(df)} rows)...")
    df = df.copy()

    df["route_short_name"] = df.get("route_short_name", pd.Series(dtype=str)).apply(clean_string)
    df["route_long_name"]  = df.get("route_long_name",  pd.Series(dtype=str)).apply(clean_string)

    # Ensure route_type is valid
    df["route_type"] = pd.to_numeric(df["route_type"], errors="coerce")
    invalid_types = df[~df["route_type"].isin(VALID_ROUTE_TYPES)]
    if not invalid_types.empty:
        log.warning(f"  {len(invalid_types)} routes with invalid route_type — setting to 3 (bus) for bus feed, 1 (metro) for metro feed")
        default_type = 1 if feed_name == "metro" else 3
        df.loc[~df["route_type"].isin(VALID_ROUTE_TYPES), "route_type"] = default_type

    df["route_type"] = df["route_type"].astype(int)
    df = prefix_ids(df, ["route_id", "agency_id"], feed_name)
    df["feed"] = feed_name

    log.info(f"  routes clean: {len(df)} rows")
    return df


def clean_trips(df: pd.DataFrame, feed_name: str) -> pd.DataFrame:
    log.info(f"  Cleaning trips ({len(df)} rows)...")
    df = df.copy()
    df = prefix_ids(df, ["trip_id", "route_id", "service_id", "shape_id"], feed_name)

    # direction_id should be 0 or 1
    if "direction_id" in df.columns:
        df["direction_id"] = pd.to_numeric(df["direction_id"], errors="coerce").fillna(0).astype(int)

    df["feed"] = feed_name
    log.info(f"  trips clean: {len(df)} rows")
    return df


def parse_gtfs_time(t: str) -> int | None:
    """
    Parse GTFS time string (HH:MM:SS, may exceed 24:00:00) to seconds.
    Returns None for unparseable values.
    GTFS allows times > 24:00:00 for overnight services — we keep them as-is
    in seconds to preserve correct ordering.
    """
    if pd.isna(t) or not isinstance(t, str):
        return None
    t = t.strip()
    match = re.match(r"^(\d+):(\d{2}):(\d{2})$", t)
    if not match:
        return None
    h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return h * 3600 + m * 60 + s


def clean_stop_times(df: pd.DataFrame, feed_name: str) -> pd.DataFrame:
    log.info(f"  Cleaning stop_times ({len(df):,} rows)...")
    df = df.copy()

    # Parse times to seconds (preserves > 24h for overnight services)
    df["arrival_seconds"]   = df["arrival_time"].apply(parse_gtfs_time)
    df["departure_seconds"] = df["departure_time"].apply(parse_gtfs_time)

    # Drop rows where we couldn't parse departure time (required for frequency calc)
    before = len(df)
    df = df.dropna(subset=["departure_seconds"])
    dropped = before - len(df)
    if dropped:
        log.warning(f"  Dropped {dropped} stop_times with unparseable departure_time")

    # stop_sequence must be integer
    df["stop_sequence"] = pd.to_numeric(df["stop_sequence"], errors="coerce")
    df = df.dropna(subset=["stop_sequence"])
    df["stop_sequence"] = df["stop_sequence"].astype(int)

    # Prefix IDs
    df = prefix_ids(df, ["trip_id", "stop_id"], feed_name)
    df["feed"] = feed_name

    # Keep original time strings alongside seconds — useful for display
    log.info(f"  stop_times clean: {len(df):,} rows")
    return df


def clean_shapes(df: pd.DataFrame, feed_name: str) -> pd.DataFrame:
    log.info(f"  Cleaning shapes ({len(df):,} rows)...")
    df = df.copy()

    df["shape_pt_lat"] = pd.to_numeric(df["shape_pt_lat"], errors="coerce")
    df["shape_pt_lon"] = pd.to_numeric(df["shape_pt_lon"], errors="coerce")
    df["shape_pt_sequence"] = pd.to_numeric(df["shape_pt_sequence"], errors="coerce")

    before = len(df)
    df = df.dropna(subset=["shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"])
    dropped = before - len(df)
    if dropped:
        log.warning(f"  Dropped {dropped} shape points with null coords or sequence")

    df = prefix_ids(df, ["shape_id"], feed_name)
    df["feed"] = feed_name
    log.info(f"  shapes clean: {len(df):,} rows")
    return df


def clean_calendar(df: pd.DataFrame, feed_name: str) -> pd.DataFrame:
    log.info(f"  Cleaning calendar ({len(df)} rows)...")
    df = df.copy()

    day_cols = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    for col in day_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # Parse date strings YYYYMMDD → standard date format
    for col in ["start_date", "end_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")

    df = prefix_ids(df, ["service_id"], feed_name)
    df["feed"] = feed_name
    log.info(f"  calendar clean: {len(df)} rows")
    return df


def save(df: pd.DataFrame, out_dir: str, filename: str):
    """Save DataFrame to CSV in output directory."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / filename
    df.to_csv(path, index=False, encoding="utf-8")
    log.info(f"  Saved {filename} → {path} ({len(df):,} rows)")


def process_feed(feed_name: str, in_dir: str, out_dir: str):
    log.info(f"\n{'='*20} {feed_name.upper()} {'='*20}")

    # ── stops ──
    stops_raw = read_gtfs_file(in_dir, "stops.txt", required=True)
    if stops_raw is not None:
        stops = clean_stops(stops_raw, feed_name)
        save(stops, out_dir, "stops.csv")

    # ── routes ──
    routes_raw = read_gtfs_file(in_dir, "routes.txt", required=True)
    if routes_raw is not None:
        routes = clean_routes(routes_raw, feed_name)
        save(routes, out_dir, "routes.csv")

    # ── trips ──
    trips_raw = read_gtfs_file(in_dir, "trips.txt", required=True)
    if trips_raw is not None:
        trips = clean_trips(trips_raw, feed_name)
        save(trips, out_dir, "trips.csv")

    # ── stop_times ──
    stop_times_raw = read_gtfs_file(in_dir, "stop_times.txt", required=True)
    if stop_times_raw is not None:
        stop_times = clean_stop_times(stop_times_raw, feed_name)
        save(stop_times, out_dir, "stop_times.csv")

    # ── shapes (optional) ──
    shapes_raw = read_gtfs_file(in_dir, "shapes.txt", required=False)
    if shapes_raw is not None:
        shapes = clean_shapes(shapes_raw, feed_name)
        save(shapes, out_dir, "shapes.csv")

    # ── calendar ──
    calendar_raw = read_gtfs_file(in_dir, "calendar.txt", required=False)
    if calendar_raw is not None:
        calendar = clean_calendar(calendar_raw, feed_name)
        save(calendar, out_dir, "calendar.csv")

    # ── calendar_dates (pass-through with ID prefix) ──
    cal_dates_raw = read_gtfs_file(in_dir, "calendar_dates.txt", required=False)
    if cal_dates_raw is not None:
        cal_dates = prefix_ids(cal_dates_raw, ["service_id"], feed_name)
        if "date" in cal_dates.columns:
            cal_dates["date"] = pd.to_datetime(cal_dates["date"], format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
        cal_dates["feed"] = feed_name
        save(cal_dates, out_dir, "calendar_dates.csv")


def main():
    log.info("=" * 60)
    log.info("Propus — GTFS Cleaning Script")
    log.info("=" * 60)

    for feed_name, paths in FEEDS.items():
        process_feed(feed_name, paths["in"], paths["out"])

    log.info("\n" + "=" * 60)
    log.info("✓ Cleaning complete. Outputs in data/processed/")
    log.info("  Next: python pipeline/ingest.py")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
