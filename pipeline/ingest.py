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

# Chunk size for large tables (stop_times can be millions of rows)
CHUNK_SIZE = 50_000


def get_engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    log.info("Database connection established.")
    return engine


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

        if i == 0:
            with engine.connect() as _conn:
                _conn.execute(text(f'TRUNCATE TABLE {schema}."{table}" CASCADE'))
                _conn.commit()

        chunk.to_sql(
            table, engine,
            schema=schema,
            if_exists="append",
            index=False,
            method="multi",
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

    keep_cols = ["ward_id","ward_name","geometry"]
    if "district" in gdf.columns:
        keep_cols.append("district")
    gdf = gdf[keep_cols]

    # Convert ward_id to string
    gdf["ward_id"] = gdf["ward_id"].astype(str).str.strip()

    # Truncate CASCADE to handle ward_metrics FK before reload
    with engine.connect() as _conn:
        _conn.execute(text("TRUNCATE TABLE public.wards CASCADE"))
        _conn.commit()

    gdf.to_postgis(
        "wards", engine,
        schema="public",
        if_exists="append",
        index=False,
    )
    log.info(f"✓ Loaded {len(gdf)} ward polygons into public.wards")


def initialise_ward_metrics(engine):
    """Create a row in derived.ward_metrics for each ward."""
    log.info("Initialising derived.ward_metrics...")
    with engine.connect() as conn:
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
        for schema, table in [("gtfs_bus","stops"),("gtfs_metro","stops"),("public","wards")]:
            try:
                result = conn.execute(
                    text(f"SELECT COUNT(*) FROM {schema}.{table} WHERE geom IS NOT NULL")
                ).scalar()
                log.info(f"  ✓  {schema}.{table} geom populated: {result:,} rows")
            except Exception as e:
                log.error(f"  ✗  {schema}.{table} geom check: {e}")


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
        log.info(f"\n── {feed_name.upper()} FEED ──")
        for filename, cfg in TABLE_MAP.items():
            load_table(engine, feed_name, filename, cfg)

        # Update geometry after stops are loaded
        schema = "gtfs_bus" if feed_name == "bus" else "gtfs_metro"
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