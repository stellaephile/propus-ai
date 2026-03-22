"""
fetch_osm_routes.py
-------------------
Fetches Delhi bus route geometries from OpenStreetMap via the
Overpass API, matches them to GTFS routes by short name, and
stores the resulting linestrings in derived.route_geometries.

OSM stores bus routes as `relation` objects with type=route,
route=bus. Each relation contains ordered `way` members whose
node coordinates form the full road-snapped route geometry.

Strategy:
  1. Pull all bus route relations inside Delhi bounding box from OSM
  2. For each relation, reconstruct ordered linestring from ways + nodes
  3. Match to GTFS routes by route_short_name (fuzzy where needed)
  4. Store matched geometries in derived.route_geometries
  5. For unmatched GTFS routes — fall back to stop-sequence linestring

Coverage note:
  OSM Delhi bus coverage is partial — major routes (BRT corridor,
  Ring Road routes) are well mapped; smaller routes may be missing.
  The fallback ensures every route gets *some* geometry.

Usage:
    python pipeline/fetch_osm_routes.py

Requires:
    pip install requests geopandas shapely sqlalchemy psycopg2-binary
    DATABASE_URL in .env
"""

import os
import time
import logging
import requests
import json
from collections import defaultdict

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

# ── Overpass config ──────────────────────────────────────────────────────────

# Public Overpass endpoints — try in order if one is slow
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Delhi bounding box: south, west, north, east
DELHI_BBOX = "28.2,76.5,29.2,77.8"


# Overpass request timeout (seconds) — large because Delhi has ~2400 routes
OVERPASS_TIMEOUT = 180

# Pause between retries
RETRY_PAUSE = 10

# Cache raw Overpass response to avoid re-fetching on re-runs
OSM_CACHE_PATH = "data/raw/osm_bus_routes.json"

# Minimum stops for fallback stop-sequence linestring
MIN_STOPS_FOR_FALLBACK = 2


# ── Overpass query ───────────────────────────────────────────────────────────

OVERPASS_QUERY = f"""
[out:json][timeout:{OVERPASS_TIMEOUT}];
(
  relation
    ["type"="route"]
    ["route"="bus"]
    ({DELHI_BBOX});
);
out body;
>;
out skel qt;
"""


def fetch_overpass(use_cache: bool = True) -> dict:
    """Fetch OSM bus route relations from Overpass. Returns raw JSON dict."""
    import pathlib

    if use_cache and pathlib.Path(OSM_CACHE_PATH).exists():
        log.info(f"Loading cached Overpass response from {OSM_CACHE_PATH}")
        with open(OSM_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    log.info("Fetching bus route relations from Overpass API...")
    log.info(f"Query bbox: {DELHI_BBOX} (this may take 1–3 minutes)")

    last_error = None
    for endpoint in OVERPASS_ENDPOINTS:
        log.info(f"  Trying {endpoint}...")
        try:
            resp = requests.post(
                endpoint,
                data={"data": OVERPASS_QUERY},
                timeout=OVERPASS_TIMEOUT + 30,
            )
            resp.raise_for_status()
            data = resp.json()

            # Cache for future runs
            pathlib.Path(OSM_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(OSM_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
            log.info(f"  Response cached to {OSM_CACHE_PATH}")

            elements = data.get("elements", [])
            relations = [e for e in elements if e["type"] == "relation"]
            nodes      = [e for e in elements if e["type"] == "node"]
            ways       = [e for e in elements if e["type"] == "way"]
            log.info(f"  Fetched: {len(relations)} relations, {len(ways)} ways, {len(nodes)} nodes")
            return data

        except requests.RequestException as e:
            log.warning(f"  {endpoint} failed: {e}")
            last_error = e
            time.sleep(RETRY_PAUSE)

    raise RuntimeError(f"All Overpass endpoints failed. Last error: {last_error}")


# ── Geometry reconstruction ──────────────────────────────────────────────────

def build_node_index(elements: list) -> dict:
    """Build {node_id: (lon, lat)} index from Overpass elements."""
    return {
        e["id"]: (e["lon"], e["lat"])
        for e in elements
        if e["type"] == "node" and "lat" in e and "lon" in e
    }


def build_way_index(elements: list) -> dict:
    """Build {way_id: [node_id, ...]} index from Overpass elements."""
    return {
        e["id"]: e.get("nodes", [])
        for e in elements
        if e["type"] == "way"
    }


def reconstruct_route_geometry(
    relation: dict,
    node_idx: dict,
    way_idx: dict,
) -> LineString | MultiLineString | None:
    """
    Reconstruct route geometry from a relation's way members.

    OSM route relations list ways in order (usually), but gaps and
    reversed ways are common. Strategy:
      1. Collect all way geometries (as coordinate lists)
      2. Try linemerge to join touching segments
      3. If linemerge gives a single LineString → perfect
      4. If MultiLineString → keep (still useful for rendering)
      5. Return None if fewer than 2 valid coordinate pairs
    """
    way_coords = []

    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        way_id = member.get("ref")
        node_ids = way_idx.get(way_id, [])
        coords = [node_idx[nid] for nid in node_ids if nid in node_idx]
        if len(coords) >= 2:
            way_coords.append(coords)

    if not way_coords:
        return None

    # Build individual LineStrings
    lines = []
    for coords in way_coords:
        try:
            lines.append(LineString(coords))
        except Exception:
            continue

    if not lines:
        return None

    # Attempt to merge touching segments into a single line
    merged = linemerge(lines)
    return merged  # LineString or MultiLineString


# ── OSM → GTFS matching ──────────────────────────────────────────────────────

def normalise_ref(s: str) -> str:
    """Normalise a route reference for matching: strip whitespace, uppercase."""
    if not s:
        return ""
    return str(s).strip().upper().replace(" ", "").replace("-", "")


def extract_osm_routes(data: dict) -> pd.DataFrame:
    """
    Extract route metadata from Overpass response.
    Returns DataFrame: osm_id, name, ref, from_tag, to_tag, operator
    """
    records = []
    for e in data.get("elements", []):
        if e["type"] != "relation":
            continue
        tags = e.get("tags", {})
        if tags.get("type") != "route" or tags.get("route") != "bus":
            continue
        records.append({
            "osm_id":   e["id"],
            "ref":      tags.get("ref", ""),
            "name":     tags.get("name", ""),
            "from_tag": tags.get("from", ""),
            "to_tag":   tags.get("to", ""),
            "operator": tags.get("operator", ""),
            "ref_norm": normalise_ref(tags.get("ref", "")),
        })
    df = pd.DataFrame(records)
    log.info(f"Extracted {len(df)} OSM bus route relations")
    return df


def load_gtfs_routes(conn) -> pd.DataFrame:
    """Load GTFS bus routes with their short names."""
    rows = conn.execute(text("""
        SELECT route_id, route_short_name, route_long_name
        FROM gtfs_bus.routes
        ORDER BY route_id
    """)).fetchall()
    df = pd.DataFrame(rows, columns=["route_id", "route_short_name", "route_long_name"])
    df["ref_norm"] = df["route_short_name"].apply(normalise_ref)
    log.info(f"Loaded {len(df)} GTFS bus routes")
    return df


def match_routes(gtfs_routes: pd.DataFrame, osm_routes: pd.DataFrame) -> pd.DataFrame:
    """
    Match GTFS routes to OSM relations by normalised ref.

    Returns DataFrame: route_id, osm_id, match_type
    match_type: 'exact' | 'unmatched'
    """
    # Build OSM ref → list of osm_ids (there may be multiple directions)
    osm_by_ref = defaultdict(list)
    for _, row in osm_routes.iterrows():
        if row["ref_norm"]:
            osm_by_ref[row["ref_norm"]].append(row["osm_id"])

    matches = []
    unmatched = []

    for _, row in gtfs_routes.iterrows():
        ref = row["ref_norm"]
        if ref and ref in osm_by_ref:
            # Take all matching osm_ids (both directions)
            for osm_id in osm_by_ref[ref]:
                matches.append({
                    "route_id":   row["route_id"],
                    "osm_id":     osm_id,
                    "match_type": "exact",
                })
        else:
            unmatched.append(row["route_id"])

    log.info(f"Matched {len(set(m['route_id'] for m in matches))} / {len(gtfs_routes)} GTFS routes to OSM")
    log.info(f"Unmatched: {len(unmatched)} routes — will use stop-sequence fallback")

    return pd.DataFrame(matches), unmatched


# ── Fallback: stop-sequence linestring ──────────────────────────────────────

def build_fallback_geometries(conn, unmatched_route_ids: list) -> list:
    """
    For routes not found in OSM, build a straight-line approximation
    by connecting stops in stop_sequence order.
    Returns list of dicts: {route_id, feed, geometry}
    """
    if not unmatched_route_ids:
        return []

    log.info(f"Building stop-sequence fallback for {len(unmatched_route_ids)} routes...")

    # Parameterise the IN clause safely
    placeholders = ", ".join([f":id_{i}" for i in range(len(unmatched_route_ids))])
    params = {f"id_{i}": rid for i, rid in enumerate(unmatched_route_ids)}

    rows = conn.execute(text(f"""
        WITH best_trip AS (
            SELECT DISTINCT ON (t.route_id)
                t.route_id,
                t.trip_id,
                COUNT(st.stop_id) OVER (PARTITION BY t.trip_id) AS stop_count
            FROM gtfs_bus.trips t
            JOIN gtfs_bus.stop_times st ON st.trip_id = t.trip_id
            WHERE t.route_id IN ({placeholders})
            ORDER BY t.route_id, stop_count DESC
        )
        SELECT
            bt.route_id,
            ARRAY_AGG(s.stop_lon::float ORDER BY st.stop_sequence) AS lons,
            ARRAY_AGG(s.stop_lat::float ORDER BY st.stop_sequence) AS lats
        FROM best_trip bt
        JOIN gtfs_bus.stop_times st ON st.trip_id = bt.trip_id
        JOIN gtfs_bus.stops s       ON s.stop_id  = st.stop_id
        WHERE s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
        GROUP BY bt.route_id
        HAVING COUNT(s.stop_id) >= :min_stops
    """), {**params, "min_stops": MIN_STOPS_FOR_FALLBACK}).fetchall()

    records = []
    for row in rows:
        route_id, lons, lats = row
        coords = [(lon, lat) for lon, lat in zip(lons, lats)
                  if lon is not None and lat is not None]
        if len(coords) < 2:
            continue
        try:
            records.append({
                "route_id":    route_id,
                "feed":        "bus",
                "source":      "fallback_stop_sequence",
                "geometry":    LineString(coords),
            })
        except Exception as e:
            log.warning(f"  Fallback failed for route {route_id}: {e}")

    log.info(f"  Built {len(records)} fallback geometries")
    return records


# ── Database write ────────────────────────────────────────────────────────────

def prepare_table(conn):
    """
    Drop the materialised view from schema.sql (if it exists)
    and create a proper table for route_geometries so we can
    INSERT rows incrementally.
    """
    conn.execute(text("""
        DROP MATERIALIZED VIEW IF EXISTS derived.route_geometries CASCADE
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS derived.route_geometries (
            route_id    TEXT NOT NULL,
            feed        TEXT NOT NULL,
            source      TEXT,           -- 'osm' | 'fallback_stop_sequence'
            geom        GEOMETRY(GEOMETRY, 4326),
            PRIMARY KEY (route_id, feed)
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_route_geom_gist
        ON derived.route_geometries USING GIST (geom)
    """))
    conn.commit()
    log.info("derived.route_geometries table ready")


def save_geometries(engine, records: list):
    """Write route geometry records to PostGIS."""
    if not records:
        log.warning("No records to save.")
        return

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")

    # Ensure source column exists
    if "source" not in gdf.columns:
        gdf["source"] = "osm"

    # Keep only needed columns
    gdf = gdf[["route_id", "feed", "source", "geometry"]]

    log.info(f"Writing {len(gdf)} route geometries to derived.route_geometries...")
    gdf.to_postgis(
        "route_geometries",
        engine,
        schema="derived",
        if_exists="append",
        index=False,
    )
    log.info(f"✓ Saved {len(gdf)} geometries")


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary(engine):
    with engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM derived.route_geometries"
        )).scalar()
        osm_count = conn.execute(text(
            "SELECT COUNT(*) FROM derived.route_geometries WHERE source='osm'"
        )).scalar()
        fallback_count = conn.execute(text(
            "SELECT COUNT(*) FROM derived.route_geometries WHERE source='fallback_stop_sequence'"
        )).scalar()
        metro_count = conn.execute(text(
            "SELECT COUNT(*) FROM derived.route_geometries WHERE feed='metro'"
        )).scalar()

    log.info("\n── Route geometry summary ──")
    log.info(f"  Total route geometries:  {total}")
    log.info(f"  From OSM:                {osm_count}")
    log.info(f"  From stop sequence:      {fallback_count}")
    log.info(f"  Metro:                   {metro_count}")

    coverage = osm_count / (total - metro_count) * 100 if (total - metro_count) > 0 else 0
    log.info(f"  OSM coverage (bus):      {coverage:.1f}%")

    if coverage < 30:
        log.warning("  OSM coverage is low — this is normal for less-mapped Delhi routes.")
        log.warning("  All routes still have fallback geometry for map rendering.")


# ── Metro routes (always use stop-sequence — shapes.txt missing for metro too) ─

def build_metro_geometries(conn) -> list:
    """Build stop-sequence linestrings for all metro routes."""
    log.info("Building metro route geometries from stop sequences...")

    rows = conn.execute(text("""
        WITH best_trip AS (
            SELECT DISTINCT ON (t.route_id)
                t.route_id,
                t.trip_id,
                COUNT(st.stop_id) OVER (PARTITION BY t.trip_id) AS stop_count
            FROM gtfs_metro.trips t
            JOIN gtfs_metro.stop_times st ON st.trip_id = t.trip_id
            ORDER BY t.route_id, stop_count DESC
        )
        SELECT
            bt.route_id,
            ARRAY_AGG(s.stop_lon::float ORDER BY st.stop_sequence) AS lons,
            ARRAY_AGG(s.stop_lat::float ORDER BY st.stop_sequence) AS lats
        FROM best_trip bt
        JOIN gtfs_metro.stop_times st ON st.trip_id = bt.trip_id
        JOIN gtfs_metro.stops s       ON s.stop_id  = st.stop_id
        WHERE s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
        GROUP BY bt.route_id
        HAVING COUNT(s.stop_id) >= 2
    """)).fetchall()

    records = []
    for row in rows:
        route_id, lons, lats = row
        coords = [(lon, lat) for lon, lat in zip(lons, lats)
                  if lon is not None and lat is not None]
        if len(coords) < 2:
            continue
        try:
            records.append({
                "route_id": route_id,
                "feed":     "metro",
                "source":   "stop_sequence",
                "geometry": LineString(coords),
            })
        except Exception:
            continue

    log.info(f"  Built {len(records)} metro route geometries")
    return records


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Propus — OSM Route Geometry Fetcher")
    log.info("=" * 60)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with engine.connect() as conn:
        prepare_table(conn)

    # ── Step 1: Fetch OSM data ──
    data = fetch_overpass(use_cache=True)

    node_idx = build_node_index(data["elements"])
    way_idx  = build_way_index(data["elements"])
    osm_routes = extract_osm_routes(data)

    # ── Step 2: Load GTFS routes and match ──
    with engine.connect() as conn:
        gtfs_routes = load_gtfs_routes(conn)

    matched_df, unmatched_ids = match_routes(gtfs_routes, osm_routes)

    # ── Step 3: Build OSM geometries for matched routes ──
    log.info(f"\nBuilding OSM geometries for {len(matched_df)} matched route-direction pairs...")
    osm_records = []

    relations_by_id = {
        e["id"]: e
        for e in data["elements"]
        if e["type"] == "relation"
    }

    for _, match in matched_df.iterrows():
        relation = relations_by_id.get(match["osm_id"])
        if not relation:
            continue
        geom = reconstruct_route_geometry(relation, node_idx, way_idx)
        if geom is None:
            continue
        osm_records.append({
            "route_id": match["route_id"],
            "feed":     "bus",
            "source":   "osm",
            "geometry": geom,
        })

    log.info(f"Successfully built {len(osm_records)} OSM geometries")

    # ── Step 4: Fallback for unmatched bus routes ──
    with engine.connect() as conn:
        fallback_records = build_fallback_geometries(conn, unmatched_ids)

    # ── Step 5: Metro routes (always stop-sequence) ──
    with engine.connect() as conn:
        metro_records = build_metro_geometries(conn)

    # ── Step 6: Deduplicate and save ──
    # OSM wins over fallback when both exist for same route_id
    all_records = osm_records + fallback_records + metro_records

    # Deduplicate: prefer osm source
    seen = {}
    deduped = []
    for r in all_records:
        key = (r["route_id"], r["feed"])
        if key not in seen or r["source"] == "osm":
            seen[key] = r
    deduped = list(seen.values())

    save_geometries(engine, deduped)
    print_summary(engine)

    log.info("\n" + "=" * 60)
    log.info("✓ Route geometries built and stored.")
    log.info("  OSM-matched routes: road-snapped geometry")
    log.info("  Unmatched routes:   stop-sequence straight-line approximation")
    log.info("  Both render correctly in Folium.")
    log.info("\n  Next: python pipeline/compute.py")
    log.info("=" * 60)


if __name__ == "__main__":
    main()