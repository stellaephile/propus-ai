"""
ingest.py
---------
Loads cleaned GTFS CSVs into PostGIS and ingests Delhi ward boundaries.
Run after clean_gtfs.py and after schema.sql has been applied.

Usage:
    python pipeline/ingest.py

Requires:
    DATABASE_URL in .env  e.g. postgresql://user:pass@host:5432/propus
    pip install geopandas sqlalchemy psycopg2-binary pandas python-dotenv
"""

import os
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine, text
from pathlib import Path
import subprocess
import logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
WARD_SHAPEFILE = "data/raw/delhi_wards.shp"  # from OpenCity India

FEEDS = {
    "bus":   "data/processed/bus",
    "metro": "data/processed/metro",
}

# Column names expected in each processed CSV
# Maps filename → (schema, table, required_cols)
TABLE_MAP = {
    "stops.csv": {
        "bus":   ("gtfs_bus",   "stops"),
        "metro": ("gtfs_metro", "stops"),
        "cols":  ["stop_id","stop_name","stop_lat","stop_lon","location_type","feed"],
    },
    "routes.csv": {
        "bus":   ("gtfs_bus",   "routes"),
        "metro": ("gtfs_metro", "routes"),
        "cols":  ["route_id","route_short_name","route_long_name","route_type","feed"],
    },
    "trips.csv": {
        "bus":   ("gtfs_bus",   "trips"),
        "metro": ("gtfs_metro", "trips"),
        "cols":  ["trip_id","route_id","service_id","direction_id","feed"],
    },
    "stop_times.csv": {
        "bus":   ("gtfs_bus",   "stop_times"),
        "metro": ("gtfs_metro", "stop_times"),
        "cols":  ["trip_id","stop_id","arrival_time","departure_time",
                  "arrival_seconds","departure_seconds","stop_sequence","feed"],
    },
    "shapes.csv": {
        "bus":   ("gtfs_bus",   "shapes"),
        "metro": ("gtfs_metro", "shapes"),
        "cols":  ["shape_id","shape_pt_lat","shape_pt_lon","shape_pt_sequence","feed"],
        "optional": True,
    },
    "calendar.csv": {
        "bus":   ("gtfs_bus",   "calendar"),
        "metro": ("gtfs_metro", "calendar"),
        "cols":  ["service_id","monday","tuesday","wednesday","thursday",
                  "friday","saturday","sunday","start_date","end_date","feed"],
        "optional": True,
    },
    "calendar_dates.csv": {
        "bus":   ("gtfs_bus",   "calendar_dates"),
        "metro": ("gtfs_metro", "calendar_dates"),
        "cols":  ["service_id","date","exception_type","feed"],
        "optional": True,
    },
}

# Chunk size for large tables (stop_times can be millions of rows).
# Kept at 50k rows — safe with COPY writer (no parameter limit).
CHUNK_SIZE = 50_000


# ---------------------------------------------------------------------------
# Fast COPY writer — replaces method="multi" which hits psycopg2's 65,535
# bound-parameter limit at 50k rows × 8+ columns.
# Uses PostgreSQL COPY FROM STDIN which has no parameter limit and is
# 10–20× faster than multi-row INSERT for large tables.
# ---------------------------------------------------------------------------
def _psql_copy_writer(table, conn, keys, data_iter):
    """pandas to_sql writer using COPY FROM STDIN via psycopg2."""
    import io
    import csv
    raw = conn.connection          # unwrap SQLAlchemy → psycopg2 connection
    with raw.cursor() as cur:
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerows(data_iter)
        buf.seek(0)
        cols = ", ".join(f'"{k}"' for k in keys)
        cur.copy_expert(
            f'COPY "{table.schema}"."{table.name}" ({cols}) FROM STDIN WITH (FORMAT CSV)',
            buf,
        )


def get_engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    log.info("Database connection established.")
    return engine




# ---------------------------------------------------------------------------
# FK constraint management
# ---------------------------------------------------------------------------
# Cloud SQL blocks both DISABLE TRIGGER ALL (needs superuser) and
# session_replication_role (restricted). Instead we drop FK constraints
# before bulk load and recreate them after — no special privileges needed.
#
# Constraints discovered from information_schema:
#   gtfs_bus.stop_times  → stop_times_trip_id_fkey  (trip_id  → trips)
#   gtfs_bus.stop_times  → stop_times_stop_id_fkey  (stop_id  → stops)
#   gtfs_bus.trips       → trips_route_id_fkey       (route_id → routes)
#   gtfs_metro.stop_times → stop_times_trip_id_fkey (trip_id  → trips)
#   gtfs_metro.stop_times → stop_times_stop_id_fkey (stop_id  → stops)
#   gtfs_metro.trips      → trips_route_id_fkey      (route_id → routes)
# ---------------------------------------------------------------------------

_FK_CONSTRAINTS = {
    "gtfs_bus": [
        ("stop_times", "stop_times_trip_id_fkey", "trip_id",  "trips",  "trip_id"),
        ("stop_times", "stop_times_stop_id_fkey", "stop_id",  "stops",  "stop_id"),
        ("trips",      "trips_route_id_fkey",      "route_id", "routes", "route_id"),
    ],
    "gtfs_metro": [
        ("stop_times", "stop_times_trip_id_fkey", "trip_id",  "trips",  "trip_id"),
        ("stop_times", "stop_times_stop_id_fkey", "stop_id",  "stops",  "stop_id"),
        ("trips",      "trips_route_id_fkey",      "route_id", "routes", "route_id"),
    ],
}


def drop_gtfs_fk_constraints(engine, schema: str):
    """Drop FK constraints on gtfs schema tables before bulk load."""
    log.info(f"  Dropping FK constraints on {schema}.*...")
    constraints = _FK_CONSTRAINTS.get(schema, [])
    with engine.connect() as conn:
        for table, constraint, *_ in constraints:
            conn.execute(text(
                f'ALTER TABLE {schema}."{table}" DROP CONSTRAINT IF EXISTS "{constraint}"'
            ))
            log.info(f"    dropped {schema}.{table}.{constraint}")
        conn.commit()
    log.info(f"  ✓ FK constraints dropped for {schema}")


def restore_gtfs_fk_constraints(engine, schema: str):
    """Recreate FK constraints after bulk load."""
    log.info(f"  Restoring FK constraints on {schema}.*...")
    constraints = _FK_CONSTRAINTS.get(schema, [])
    with engine.connect() as conn:
        for table, constraint, col, ref_table, ref_col in constraints:
            conn.execute(text(f"""
                ALTER TABLE {schema}."{table}"
                ADD CONSTRAINT "{constraint}"
                FOREIGN KEY ("{col}") REFERENCES {schema}."{ref_table}" ("{ref_col}")
            """))
            log.info(f"    restored {schema}.{table}.{constraint}")
        conn.commit()
    log.info(f"  ✓ FK constraints restored for {schema}")

# ---------------------------------------------------------------------------
# ID normalisation
# ---------------------------------------------------------------------------
# Problem: clean_gtfs.py added 'bus_' / 'metro_' prefixes to IDs in some
# files but not others, breaking every JOIN.
#
# Observed mismatches (bus feed only — metro route_id + trip_id are clean):
#   routes.csv   route_id  = 'bus_142'       ← has prefix
#   trips.csv    route_id  = '142'            ← missing prefix  → ADD 'bus_'
#
#   stop_times.csv trip_id = 'bus_920_14_31' ← has prefix
#   trips.csv      trip_id = '920_14_31'     ← missing prefix  → ADD 'bus_'
#
# Rule applied per (feed, filename, column):
#   If the canonical reference column already has the prefix AND this column
#   does not → prepend the feed prefix.
#
# We detect the mismatch lazily by checking whether values already start with
# the prefix, so re-running ingest is always idempotent.
# ---------------------------------------------------------------------------

# Columns to normalise and the prefix to use per feed.
# Format: { filename: [(col, feed_that_needs_fix), ...] }
_PREFIX_FIX = {
    # bus trips.csv: route_id bare → add 'bus_'
    # bus trips.csv: trip_id  bare → add 'bus_'
    "trips.csv": [
        ("route_id", "bus"),
        ("trip_id",  "bus"),
    ],
    # No fixes needed for metro (already consistent).
}


def _normalise_ids(chunk: pd.DataFrame, filename: str, feed_name: str) -> pd.DataFrame:
    """
    Add the feed prefix to ID columns that are missing it.
    Safe to call even when the prefix is already present (idempotent).
    """
    fixes = _PREFIX_FIX.get(filename, [])
    for col, target_feed in fixes:
        if col not in chunk.columns:
            continue
        if feed_name != target_feed:
            continue

        prefix = f"{feed_name}_"
        # Only touch rows that don't already carry the prefix
        mask = ~chunk[col].astype(str).str.startswith(prefix)
        if mask.any():
            chunk.loc[mask, col] = prefix + chunk.loc[mask, col].astype(str)
            log.debug(f"  [{feed_name}] {filename}: prefixed {mask.sum()} rows in '{col}'")

    return chunk


def load_table(engine, feed_name: str, filename: str, cfg: dict):
    """Load a single CSV into PostGIS."""
    schema, table = cfg[feed_name]
    csv_path = Path(FEEDS[feed_name]) / filename
    optional = cfg.get("optional", False)

    if not csv_path.exists():
        if optional:
            log.warning(f"  [{feed_name}] {filename} not found — skipping (optional)")
            return
        else:
            log.error(f"  [{feed_name}] {filename} not found — required!")
            raise FileNotFoundError(csv_path)

    log.info(f"  [{feed_name}] Loading {filename} → {schema}.{table}")

    # For large files, use chunked loading
    total_rows = 0
    for i, chunk in enumerate(pd.read_csv(csv_path, dtype=str, chunksize=CHUNK_SIZE)):
        # Keep only columns that exist in both the CSV and our schema
        expected = cfg.get("cols", list(chunk.columns))
        available = [c for c in expected if c in chunk.columns]
        chunk = chunk[available]

        # Numeric coercions
        if "stop_lat" in chunk.columns:
            chunk["stop_lat"] = pd.to_numeric(chunk["stop_lat"], errors="coerce")
        if "stop_lon" in chunk.columns:
            chunk["stop_lon"] = pd.to_numeric(chunk["stop_lon"], errors="coerce")
        if "stop_sequence" in chunk.columns:
            chunk["stop_sequence"] = pd.to_numeric(chunk["stop_sequence"], errors="coerce")
        if "arrival_seconds" in chunk.columns:
            chunk["arrival_seconds"] = pd.to_numeric(chunk["arrival_seconds"], errors="coerce")
        if "departure_seconds" in chunk.columns:
            chunk["departure_seconds"] = pd.to_numeric(chunk["departure_seconds"], errors="coerce")
        if "route_type" in chunk.columns:
            chunk["route_type"] = pd.to_numeric(chunk["route_type"], errors="coerce")
        if "location_type" in chunk.columns:
            chunk["location_type"] = pd.to_numeric(chunk["location_type"], errors="coerce").fillna(0)

        for day in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]:
            if day in chunk.columns:
                chunk[day] = pd.to_numeric(chunk[day], errors="coerce").fillna(0)

        # Date columns
        for col in ["start_date","end_date","date"]:
            if col in chunk.columns:
                chunk[col] = pd.to_datetime(chunk[col], errors="coerce").dt.date

        # ── ID normalisation (fixes prefix mismatches without touching CSVs) ──
        chunk = _normalise_ids(chunk, filename, feed_name)

        if i == 0:
            with engine.connect() as _conn:
                _conn.execute(text(f'TRUNCATE TABLE {schema}."{table}" CASCADE'))
                _conn.commit()

        chunk.to_sql(
            table, engine,
            schema=schema,
            if_exists="append",
            index=False,
            method=_psql_copy_writer,
        )
        total_rows += len(chunk)

        if i % 10 == 0 and i > 0:
            log.info(f"    ... {total_rows:,} rows loaded")

    log.info(f"  [{feed_name}] ✓ {filename}: {total_rows:,} rows")


def update_stop_geometries(engine, schema: str):
    """
    Trigger the geom column update for all stops.
    The schema.sql trigger auto-sets geom on INSERT,
    but to_sql bypasses triggers — we update manually.
    """
    log.info(f"  Updating geometry column in {schema}.stops...")
    with engine.connect() as conn:
        conn.execute(text(f"""
            UPDATE {schema}.stops
            SET geom = ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326)
            WHERE stop_lat IS NOT NULL AND stop_lon IS NOT NULL
        """))
        conn.commit()
    log.info(f"  ✓ Geometry updated for {schema}.stops")


def load_ward_boundaries(engine):
    """Load Delhi ward boundaries into public.wards."""
    ward_path = Path(WARD_SHAPEFILE)

    if not ward_path.exists():
        log.warning(f"Ward shapefile not found at {WARD_SHAPEFILE}")
        log.warning("Download from: https://data.opencity.in/dataset/delhi-wards-information")
        log.warning("Place as data/raw/delhi_wards.shp (with associated .dbf, .prj, .shx)")
        return

    log.info(f"Loading ward boundaries from {WARD_SHAPEFILE}...")
    gdf = gpd.read_file(WARD_SHAPEFILE)

    # Normalise CRS to WGS84 (EPSG:4326)
    if gdf.crs is None:
        log.warning("Ward shapefile has no CRS — assuming EPSG:4326")
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        log.info(f"Reprojecting wards from {gdf.crs} to EPSG:4326...")
        gdf = gdf.to_crs(epsg=4326)

    # Ensure MultiPolygon (required by schema)
    from shapely.geometry import MultiPolygon, Polygon
    def to_multipolygon(geom):
        if isinstance(geom, Polygon):
            return MultiPolygon([geom])
        return geom
    gdf["geometry"] = gdf["geometry"].apply(to_multipolygon)

    # Normalise column names — ward shapefiles vary in column naming
    rename_map = {}
    for col in gdf.columns:
        cl = col.lower().strip()
        if cl in ["ward_id", "id", "ward_no", "wardno", "ward_number"]:
            rename_map[col] = "ward_id"
        elif cl in ["ward_name", "name", "wardname", "ward"]:
            rename_map[col] = "ward_name"
        elif cl in ["district", "dist", "dist_name"]:
            rename_map[col] = "district"
    gdf = gdf.rename(columns=rename_map)

    if "ward_id" not in gdf.columns:
        log.warning("No ward_id column found — generating sequential IDs")
        gdf["ward_id"] = ["ward_" + str(i).zfill(3) for i in range(len(gdf))]

    if "ward_name" not in gdf.columns:
        gdf["ward_name"] = gdf["ward_id"]

    # Convert ward_id to string and strip whitespace
    gdf["ward_id"] = gdf["ward_id"].astype(str).str.strip()

    # ── FIX: patch any null/empty ward_ids before writing ──
    null_mask = gdf["ward_id"].isin(["", "None", "nan"]) | gdf["ward_id"].isna()
    if null_mask.any():
        count = null_mask.sum()
        log.warning(f"  {count} ward(s) have null ward_id — generating fallback IDs")
        fallback_ids = ["ward_fallback_" + str(i).zfill(3) for i in range(count)]
        gdf.loc[null_mask, "ward_id"] = fallback_ids

    # Fill ward_name from ward_id where missing
    gdf["ward_name"] = gdf["ward_name"].fillna(gdf["ward_id"])

    # Deduplicate ward_ids (keep first)
    before = len(gdf)
    gdf = gdf.drop_duplicates(subset=["ward_id"], keep="first")
    if len(gdf) < before:
        log.warning(f"  Dropped {before - len(gdf)} duplicate ward_ids")

    keep_cols = ["ward_id", "ward_name", "geometry"]
    if "district" in gdf.columns:
        keep_cols.append("district")
    gdf = gdf[keep_cols]

    # Drop CASCADE first to handle ward_metrics FK, then recreate via to_postgis
    with engine.connect() as _conn:
        _conn.execute(text("DROP TABLE IF EXISTS public.wards CASCADE"))
        _conn.commit()

    gdf.to_postgis(
        "wards", engine,
        schema="public",
        if_exists="replace",
        index=False,
    )

    # Recreate spatial index and PRIMARY KEY
    with engine.connect() as _conn:
        _conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_wards_geom ON public.wards USING GIST (geometry)"
        ))
        _conn.execute(text(
            "ALTER TABLE public.wards ADD PRIMARY KEY (ward_id)"
        ))
        _conn.commit()
    log.info(f"✓ Loaded {len(gdf)} ward polygons into public.wards")

def initialise_ward_metrics(engine):
    """Create a row in derived.ward_metrics for each ward."""
    log.info("Initialising derived.ward_metrics...")
    with engine.connect() as conn:
        # Recreate FK after wards table was dropped and recreated
        conn.execute(text("""
            ALTER TABLE derived.ward_metrics
            DROP CONSTRAINT IF EXISTS ward_metrics_ward_id_fkey
        """))
        conn.execute(text("""
            ALTER TABLE derived.ward_metrics
            ADD CONSTRAINT ward_metrics_ward_id_fkey
            FOREIGN KEY (ward_id) REFERENCES public.wards(ward_id)
        """))
        conn.execute(text("""
            INSERT INTO derived.ward_metrics (ward_id, ward_name)
            SELECT ward_id, ward_name FROM public.wards
            ON CONFLICT (ward_id) DO NOTHING
        """))
        conn.commit()
    log.info("✓ derived.ward_metrics initialised")


def refresh_route_geometries(engine):
    """Refresh the materialised route geometry view."""
    log.info("Refreshing derived.route_geometries materialised view...")
    with engine.connect() as conn:
        conn.execute(text("REFRESH MATERIALIZED VIEW derived.route_geometries"))
        conn.commit()
    log.info("✓ route_geometries refreshed")


def verify_load(engine):
    """Quick row count verification across all key tables."""
    log.info("\n── Load verification ──")
    checks = [
        ("gtfs_bus",   "stops"),
        ("gtfs_bus",   "routes"),
        ("gtfs_bus",   "trips"),
        ("gtfs_bus",   "stop_times"),
        ("gtfs_metro", "stops"),
        ("gtfs_metro", "routes"),
        ("gtfs_metro", "stop_times"),
        ("public",     "wards"),
        ("derived",    "ward_metrics"),
    ]
    with engine.connect() as conn:
        for schema, table in checks:
            try:
                result = conn.execute(
                    text(f"SELECT COUNT(*) FROM {schema}.{table}")
                ).scalar()
                status = "✓" if result > 0 else "✗ EMPTY"
                log.info(f"  {status}  {schema}.{table}: {result:,} rows")
            except Exception as e:
                log.error(f"  ✗  {schema}.{table}: {e}")

        # Check geometry population
        geom_checks = [
            ("gtfs_bus",   "stops", "geom"),
            ("gtfs_metro", "stops", "geom"),
            ("public",     "wards", "geometry"),   # to_postgis uses "geometry" not "geom"
        ]
        for schema, table, geom_col in geom_checks:
            try:
                result = conn.execute(
                    text(f"SELECT COUNT(*) FROM {schema}.{table} WHERE {geom_col} IS NOT NULL")
                ).scalar()
                log.info(f"  ✓  {schema}.{table} geom populated: {result:,} rows")
            except Exception as e:
                log.error(f"  ✗  {schema}.{table} geom check: {e}")
                

    # ── Post-load join sanity check ──
    log.info("\n── Join sanity check ──")
    join_checks = [
        (
            "bus trips ↔ routes",
            """SELECT COUNT(*) FROM gtfs_bus.trips t
               JOIN gtfs_bus.routes r ON r.route_id = t.route_id"""
        ),
        (
            "bus stop_times ↔ trips",
            """SELECT COUNT(*) FROM gtfs_bus.stop_times st
               JOIN gtfs_bus.trips t ON t.trip_id = st.trip_id
               LIMIT 1"""
        ),
        (
            "bus stop_times ↔ stops",
            """SELECT COUNT(*) FROM gtfs_bus.stop_times st
               JOIN gtfs_bus.stops s ON s.stop_id = st.stop_id
               LIMIT 1"""
        ),
        (
            "metro trips ↔ routes",
            """SELECT COUNT(*) FROM gtfs_metro.trips t
               JOIN gtfs_metro.routes r ON r.route_id = t.route_id"""
        ),
    ]
    with engine.connect() as conn:
        for label, sql in join_checks:
            try:
                result = conn.execute(text(sql)).scalar()
                status = "✓" if result and result > 0 else "✗ ZERO ROWS"
                log.info(f"  {status}  {label}: {result:,} rows joined")
            except Exception as e:
                log.error(f"  ✗  {label}: {e}")


def main():
    log.info("=" * 60)
    log.info("Propus — GTFS PostGIS Ingest Script")
    log.info("=" * 60)

    engine = get_engine()

    # ── Load ward boundaries first (needed for derived metrics init) ──
    log.info("\n── Ward boundaries ──")
    load_ward_boundaries(engine)

    # ── Load each GTFS feed ──
    for feed_name in ["bus", "metro"]:
        schema = "gtfs_bus" if feed_name == "bus" else "gtfs_metro"
        log.info(f"\n── {feed_name.upper()} FEED ──")

        # Drop FKs before bulk load, restore after — avoids load-order
        # violations without needing superuser privileges on Cloud SQL
        drop_gtfs_fk_constraints(engine, schema)
        try:
            for filename, cfg in TABLE_MAP.items():
                load_table(engine, feed_name, filename, cfg)
        finally:
            # Always restore, even if a table load fails mid-way
            restore_gtfs_fk_constraints(engine, schema)

        # Update geometry after stops are loaded
        update_stop_geometries(engine, schema)

    # ── Post-load steps ──
    log.info("\n── Post-load steps ──")
    initialise_ward_metrics(engine)
    refresh_route_geometries(engine)

    # ── Verify ──
    verify_load(engine)

    log.info("\n" + "=" * 60)
    log.info("✓ Ingest complete.")
    log.info("  Next: python pipeline/compute.py")
    log.info("=" * 60)


if __name__ == "__main__":
    main()