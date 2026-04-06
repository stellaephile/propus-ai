"""
mcp_server/server.py
--------------------
FastMCP server that exposes Delhi transit PostGIS data as MCP tools.

The ADK agent connects to this server via stdio or SSE transport.
Every tool maps directly to a SQL query against the real DB tables
built by ingest.py and compute.py:

    gtfs_bus.stops / gtfs_bus.routes / gtfs_bus.trips / gtfs_bus.stop_times
    gtfs_metro.stops / gtfs_metro.routes / gtfs_metro.trips / gtfs_metro.stop_times
    public.wards
    derived.ward_metrics
    derived.route_geometries

Usage (stdio — for ADK local dev):
    python mcp_server/server.py

Usage (SSE — for remote / Cloud Run):
    MCP_TRANSPORT=sse python mcp_server/server.py

Requires:
    pip install fastmcp psycopg2-binary sqlalchemy python-dotenv geopandas
"""

import json
import logging
import os
from typing import Any

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from fastmcp import FastMCP
from sqlalchemy import create_engine, text

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)

mcp = FastMCP(
    name="propus-delhi-transit",
    instructions=(
        "Delhi transit intelligence tools. All spatial queries use EPSG:4326. "
        "Stop IDs are prefixed: bus stops = 'bus_<n>', metro stops = 'metro_<n>'. "
        "Ward IDs match public.wards.ward_id. "
        "Distances are in metres unless otherwise stated."
    ),
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _query(sql: str, params: dict | None = None) -> list[dict]:
    """Execute a SQL query and return rows as list of dicts."""
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        cols = list(result.keys())
        return [dict(zip(cols, row)) for row in result.fetchall()]


def _df(sql: str, params: dict | None = None) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


# ---------------------------------------------------------------------------
# Tool 1 — get_nearby_stops
# ---------------------------------------------------------------------------

@mcp.tool()
def get_nearby_stops(lat: float, lon: float, radius_m: int = 500) -> str:
    """
    Return bus and metro stops within radius_m metres of (lat, lon).

    Uses ST_DWithin on geography type for accurate metre-based distance.
    Results are sorted by distance ascending.

    Args:
        lat: Latitude in decimal degrees (WGS84).
        lon: Longitude in decimal degrees (WGS84).
        radius_m: Search radius in metres (default 500).

    Returns:
        JSON list of stops with stop_id, stop_name, feed (bus/metro),
        lat, lon, distance_m.
    """
    sql = """
        SELECT
            stop_id,
            stop_name,
            feed,
            stop_lat  AS lat,
            stop_lon  AS lon,
            ROUND(
                ST_Distance(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                )
            )::int AS distance_m
        FROM (
            SELECT stop_id, stop_name, 'bus' AS feed, stop_lat, stop_lon, geom
            FROM gtfs_bus.stops
            UNION ALL
            SELECT stop_id, stop_name, 'metro' AS feed, stop_lat, stop_lon, geom
            FROM gtfs_metro.stops
        ) all_stops
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
            :radius_m
        )
        ORDER BY distance_m ASC
        LIMIT 50
    """
    rows = _query(sql, {"lat": lat, "lon": lon, "radius_m": radius_m})
    return json.dumps({"count": len(rows), "stops": rows}, default=str)


# ---------------------------------------------------------------------------
# Tool 2 — find_underserved_areas
# ---------------------------------------------------------------------------

@mcp.tool()
def find_underserved_areas(metric: str = "transit_score", threshold: float = 0.15,
                            top_n: int = 15) -> str:
    """
    Find Delhi wards that are underserved by public transit.

    Ranks wards by the chosen metric. Includes WorldPop population data
    (pop_total, stops_per_10k) when available — the WRI equity standard.

    Args:
        metric: Column to rank by. One of:
                'transit_score'  — composite GTFS score (lower = worse)
                'stops_per_10k'  — bus stops per 10,000 residents (lower = worse)
                'bus_stop_count' — raw stop count (lower = worse)
        threshold: Maximum value of metric to include (filters to underserved).
        top_n: Number of wards to return (default 15).

    Returns:
        JSON list of underserved wards with transit and population metrics.
    """
    allowed = {"transit_score", "stops_per_10k", "bus_stop_count"}
    if metric not in allowed:
        return json.dumps({"error": f"metric must be one of {sorted(allowed)}"})

    sql = f"""
        SELECT
            ward_id,
            ward_name,
            bus_stop_count,
            metro_stop_count,
            ROUND(peak_freq_mean::numeric, 2)   AS peak_freq_mean,
            has_metro_within_1k,
            ROUND(transit_score::numeric, 4)     AS transit_score,
            pop_total,
            ROUND(stops_per_10k::numeric, 4)     AS stops_per_10k,
            multimodal_gap_count
        FROM derived.ward_metrics
        WHERE {metric} <= :threshold
           OR {metric} IS NULL
        ORDER BY {metric} ASC NULLS FIRST
        LIMIT :top_n
    """
    rows = _query(sql, {"threshold": threshold, "top_n": top_n})
    return json.dumps({"count": len(rows), "metric": metric,
                       "threshold": threshold, "wards": rows}, default=str)


# ---------------------------------------------------------------------------
# Tool 3 — get_route_coverage
# ---------------------------------------------------------------------------

@mcp.tool()
def get_route_coverage(route_id: str) -> str:
    """
    Return all stops on a route and its shape geometry as GeoJSON.

    Args:
        route_id: GTFS route_id (e.g. 'bus_142' or 'metro_BL').

    Returns:
        JSON with route metadata, ordered stop list, and GeoJSON LineString
        shape for map rendering.
    """
    # Detect feed from prefix
    feed_schema = "gtfs_metro" if route_id.startswith("metro_") else "gtfs_bus"

    # Route metadata
    route_sql = f"""
        SELECT route_id, route_short_name, route_long_name, route_type
        FROM {feed_schema}.routes
        WHERE route_id = :route_id
    """
    routes = _query(route_sql, {"route_id": route_id})
    if not routes:
        return json.dumps({"error": f"Route '{route_id}' not found"})

    # Ordered stops via stop_times
    stops_sql = f"""
        SELECT DISTINCT ON (st.stop_sequence, s.stop_id)
            st.stop_sequence,
            s.stop_id,
            s.stop_name,
            s.stop_lat AS lat,
            s.stop_lon AS lon
        FROM {feed_schema}.trips t
        JOIN {feed_schema}.stop_times st ON st.trip_id = t.trip_id
        JOIN {feed_schema}.stops s ON s.stop_id = st.stop_id
        WHERE t.route_id = :route_id
        ORDER BY st.stop_sequence ASC
        LIMIT 200
    """
    stops = _query(stops_sql, {"route_id": route_id})

    # Shape geometry (may not exist for all routes)
    shape_sql = f"""
        SELECT ST_AsGeoJSON(geom)::json AS geometry
        FROM derived.route_geometries
        WHERE route_id = :route_id
        LIMIT 1
    """
    shapes = _query(shape_sql, {"route_id": route_id})
    geojson = shapes[0]["geometry"] if shapes else None

    return json.dumps({
        "route": routes[0],
        "stop_count": len(stops),
        "stops": stops,
        "shape_geojson": geojson,
    }, default=str)


# ---------------------------------------------------------------------------
# Tool 4 — compare_transit_access
# ---------------------------------------------------------------------------

@mcp.tool()
def compare_transit_access(area_a: str, area_b: str) -> str:
    """
    Side-by-side transit metrics comparison between two Delhi wards.

    Includes GTFS metrics plus RS-derived metrics (pop_total, stops_per_10k,
    ndvi_mean, ndbi_mean, urban_stress_index) when available.

    Args:
        area_a: Ward name or ward_id (e.g. 'KASHMERE GATE' or 'CANT_1').
        area_b: Ward name or ward_id for comparison.

    Returns:
        JSON with side-by-side metrics for both wards.
    """
    sql = """
        SELECT
            ward_id, ward_name,
            bus_stop_count, metro_stop_count,
            ROUND(peak_freq_mean::numeric, 2)    AS peak_freq_mean,
            ROUND(offpeak_freq_mean::numeric, 2) AS offpeak_freq_mean,
            has_metro_within_1k,
            multimodal_gap_count,
            ROUND(transit_score::numeric, 4)     AS transit_score,
            pop_total,
            ROUND(stops_per_10k::numeric, 4)     AS stops_per_10k,
            ROUND(ndvi_mean::numeric, 4)         AS ndvi_mean,
            ROUND(ndbi_mean::numeric, 4)         AS ndbi_mean,
            ROUND(urban_stress_index::numeric, 4) AS urban_stress_index
        FROM derived.ward_metrics
        WHERE UPPER(ward_name) = UPPER(:name)
           OR ward_id = :name
    """
    result_a = _query(sql, {"name": area_a})
    result_b = _query(sql, {"name": area_b})

    return json.dumps({
        area_a: result_a[0] if result_a else {"error": f"Ward '{area_a}' not found"},
        area_b: result_b[0] if result_b else {"error": f"Ward '{area_b}' not found"},
    }, default=str)


# ---------------------------------------------------------------------------
# Tool 5 — get_metro_catchment
# ---------------------------------------------------------------------------

@mcp.tool()
def get_metro_catchment(station_name: str, radius_m: int = 500) -> str:
    """
    Return the catchment zone around a metro station.

    Computes a buffer around the station, finds intersecting wards,
    and counts bus stops within the zone to assess feeder connectivity.

    Args:
        station_name: Metro station name (partial match supported).
        radius_m: Buffer radius in metres (default 500).

    Returns:
        JSON with station location, intersecting wards, bus stop count
        in catchment, and estimated population covered (if WorldPop loaded).
    """
    # Find the metro station
    station_sql = """
        SELECT stop_id, stop_name, stop_lat AS lat, stop_lon AS lon
        FROM gtfs_metro.stops
        WHERE stop_name ILIKE :pattern
        ORDER BY stop_name
        LIMIT 5
    """
    stations = _query(station_sql, {"pattern": f"%{station_name}%"})
    if not stations:
        return json.dumps({"error": f"No metro station matching '{station_name}'"})

    station = stations[0]

    # Intersecting wards
    wards_sql = """
        SELECT
            w.ward_id,
            w.ward_name,
            wm.pop_total,
            wm.transit_score
        FROM public.wards w
        LEFT JOIN derived.ward_metrics wm ON wm.ward_id = w.ward_id
        WHERE ST_DWithin(
            w.geometry::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
            :radius_m
        )
    """
    wards = _query(wards_sql, {
        "lat": station["lat"], "lon": station["lon"], "radius_m": radius_m
    })

    # Bus stops in catchment
    bus_sql = """
        SELECT COUNT(*) AS bus_stop_count
        FROM gtfs_bus.stops
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
            :radius_m
        )
    """
    bus_count = _query(bus_sql, {
        "lat": station["lat"], "lon": station["lon"], "radius_m": radius_m
    })

    total_pop = sum(w.get("pop_total") or 0 for w in wards)

    return json.dumps({
        "station": station,
        "radius_m": radius_m,
        "bus_stops_in_catchment": bus_count[0]["bus_stop_count"],
        "intersecting_wards": wards,
        "estimated_population_covered": total_pop if total_pop > 0 else "WorldPop not yet loaded",
    }, default=str)


# ---------------------------------------------------------------------------
# Tool 6 — get_frequency_at_stop
# ---------------------------------------------------------------------------

@mcp.tool()
def get_frequency_at_stop(stop_id: str, start_time: str = "08:00:00",
                           end_time: str = "10:00:00") -> str:
    """
    Return service frequency at a specific stop during a time window.

    Counts distinct trips calling at the stop and computes trips/hour.

    Args:
        stop_id: GTFS stop_id (e.g. 'bus_1234' or 'metro_45').
        start_time: Window start in HH:MM:SS (24h, may exceed 24:00 for overnight).
        end_time: Window end in HH:MM:SS.

    Returns:
        JSON with trip count, window duration, and trips_per_hour.
    """
    schema = "gtfs_metro" if stop_id.startswith("metro_") else "gtfs_bus"

    # Stop details
    stop_sql = f"""
        SELECT stop_id, stop_name, stop_lat AS lat, stop_lon AS lon
        FROM {schema}.stops WHERE stop_id = :stop_id
    """
    stop_info = _query(stop_sql, {"stop_id": stop_id})
    if not stop_info:
        return json.dumps({"error": f"Stop '{stop_id}' not found"})

    # Trip count in window using precomputed arrival_seconds
    def _to_seconds(t: str) -> int:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)

    freq_sql = f"""
        SELECT
            COUNT(DISTINCT trip_id) AS trip_count,
            MIN(arrival_time)       AS first_arrival,
            MAX(arrival_time)       AS last_arrival
        FROM {schema}.stop_times
        WHERE stop_id = :stop_id
          AND arrival_seconds >= :start_sec
          AND arrival_seconds <  :end_sec
    """
    freq = _query(freq_sql, {
        "stop_id": stop_id,
        "start_sec": _to_seconds(start_time),
        "end_sec": _to_seconds(end_time),
    })

    trip_count = freq[0]["trip_count"] if freq else 0
    window_hours = (_to_seconds(end_time) - _to_seconds(start_time)) / 3600
    trips_per_hour = round(trip_count / window_hours, 2) if window_hours > 0 else 0

    return json.dumps({
        "stop": stop_info[0],
        "window": {"start": start_time, "end": end_time, "hours": window_hours},
        "trip_count": trip_count,
        "trips_per_hour": trips_per_hour,
        "first_arrival": freq[0]["first_arrival"] if freq else None,
        "last_arrival":  freq[0]["last_arrival"]  if freq else None,
    }, default=str)


# ---------------------------------------------------------------------------
# Tool 7 — find_multimodal_gaps
# ---------------------------------------------------------------------------

@mcp.tool()
def find_multimodal_gaps(max_distance_m: int = 1000, top_n: int = 20) -> str:
    """
    Find bus stops with no metro station within max_distance_m metres.

    These are the highest-priority locations for new metro stations or
    feeder bus improvements — core transit equity insight.

    Args:
        max_distance_m: Maximum distance to metro to qualify as a gap
                        (default 1000m = 1 km).
        top_n: Number of gap stops to return.

    Returns:
        JSON list of bus stops that lack metro access, with the distance
        to their nearest metro station and their ward.
    """
    sql = """
        WITH nearest_metro AS (
            SELECT
                b.stop_id AS bus_stop_id,
                b.stop_name AS bus_stop_name,
                b.stop_lat AS lat,
                b.stop_lon AS lon,
                MIN(
                    ST_Distance(b.geom::geography, m.geom::geography)
                ) AS dist_to_nearest_metro_m
            FROM gtfs_bus.stops b
            CROSS JOIN LATERAL (
                SELECT geom
                FROM gtfs_metro.stops
                ORDER BY b.geom::geography <-> geom::geography
                LIMIT 1
            ) m
            GROUP BY b.stop_id, b.stop_name, b.stop_lat, b.stop_lon
        )
        SELECT
            nm.bus_stop_id,
            nm.bus_stop_name,
            nm.lat,
            nm.lon,
            ROUND(nm.dist_to_nearest_metro_m)::int AS dist_to_nearest_metro_m,
            w.ward_name
        FROM nearest_metro nm
        LEFT JOIN public.wards w ON ST_Within(
            ST_SetSRID(ST_MakePoint(nm.lon, nm.lat), 4326),
            w.geometry
        )
        WHERE nm.dist_to_nearest_metro_m > :max_distance_m
        ORDER BY nm.dist_to_nearest_metro_m DESC
        LIMIT :top_n
    """
    rows = _query(sql, {"max_distance_m": max_distance_m, "top_n": top_n})
    return json.dumps({
        "max_distance_m": max_distance_m,
        "gap_stop_count": len(rows),
        "stops": rows,
    }, default=str)


# ---------------------------------------------------------------------------
# Tool 8 — get_ward_rs_profile
# ---------------------------------------------------------------------------

@mcp.tool()
def get_ward_rs_profile(ward_name: str) -> str:
    """
    Return the full remote sensing and transit profile for a Delhi ward.

    Combines GTFS metrics with WorldPop population data and Sentinel-2
    derived indices (NDVI, NDBI, urban stress) when available.

    Args:
        ward_name: Ward name (case-insensitive, partial match supported)
                   or ward_id.

    Returns:
        JSON with complete ward profile: transit score, population,
        stops per 10k residents, green cover (NDVI), built-up intensity
        (NDBI), and urban stress index.
    """
    sql = """
        SELECT
            wm.ward_id,
            wm.ward_name,
            wm.bus_stop_count,
            wm.metro_stop_count,
            ROUND(wm.peak_freq_mean::numeric, 2)     AS peak_freq_mean,
            wm.has_metro_within_1k,
            wm.multimodal_gap_count,
            ROUND(wm.transit_score::numeric, 4)      AS transit_score,
            wm.pop_total,
            ROUND(wm.stops_per_10k::numeric, 4)      AS stops_per_10k,
            ROUND(wm.ndvi_mean::numeric, 4)          AS ndvi_mean,
            ROUND(wm.ndbi_mean::numeric, 4)          AS ndbi_mean,
            ROUND(wm.urban_stress_index::numeric, 4) AS urban_stress_index,
            ST_AsGeoJSON(w.geometry)::json           AS geometry
        FROM derived.ward_metrics wm
        JOIN public.wards w ON w.ward_id = wm.ward_id
        WHERE UPPER(wm.ward_name) LIKE UPPER(:pattern)
           OR wm.ward_id = :raw
        ORDER BY wm.ward_name
        LIMIT 3
    """
    rows = _query(sql, {"pattern": f"%{ward_name}%", "raw": ward_name})
    if not rows:
        return json.dumps({"error": f"No ward found matching '{ward_name}'"})

    return json.dumps({"matches": len(rows), "wards": rows}, default=str)


# ---------------------------------------------------------------------------
# Tool 9 — get_urban_stress_map
# ---------------------------------------------------------------------------

@mcp.tool()
def get_urban_stress_map() -> str:
    """
    Return GeoJSON FeatureCollection of all Delhi wards coloured by
    urban_stress_index for choropleth map rendering.

    Urban stress index = normalised(1−NDVI) + normalised(NDBI) +
    normalised(1−transit_score). Requires rs_sentinel.py to have run.
    Falls back to transit_score if RS metrics are not yet loaded.

    Returns:
        GeoJSON FeatureCollection with ward polygons and all ward metrics
        as properties. Suitable for direct use with folium.GeoJson().
    """
    sql = """
        SELECT
            w.ward_id,
            w.ward_name,
            wm.transit_score,
            wm.bus_stop_count,
            wm.pop_total,
            wm.stops_per_10k,
            wm.ndvi_mean,
            wm.ndbi_mean,
            wm.urban_stress_index,
            wm.has_metro_within_1k,
            ST_AsGeoJSON(w.geometry)::json AS geometry
        FROM public.wards w
        JOIN derived.ward_metrics wm ON wm.ward_id = w.ward_id
        ORDER BY w.ward_name
    """
    rows = _query(sql)

    features = []
    for r in rows:
        geom = r.pop("geometry", None)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {k: v for k, v in r.items()},
        })

    fc = {"type": "FeatureCollection", "features": features}
    return json.dumps(fc, default=str)


# ---------------------------------------------------------------------------
# Tool 10 — semantic_stop_search (text match — pgvector optional)
# ---------------------------------------------------------------------------

@mcp.tool()
def semantic_stop_search(query_text: str, top_n: int = 10) -> str:
    """
    Search for stops by name using fuzzy text matching.

    Uses PostgreSQL pg_trgm trigram similarity for fuzzy matching —
    handles common transliteration variants of Delhi place names
    (e.g. 'Kashmiri Gate' matches 'KASHMERE GATE').

    If pgvector embeddings are loaded (embed.py has run), this tool
    will be upgraded to cosine similarity search automatically.

    Args:
        query_text: Place name or area description to search for.
        top_n: Number of results to return (default 10).

    Returns:
        JSON list of matching stops with similarity score, feed, and location.
    """
    # Try pgvector cosine search first (requires embed.py to have run)
    try:
        vec_sql = """
            SELECT
                s.stop_id, s.stop_name, s.feed,
                e.stop_lat AS lat, e.stop_lon AS lon,
                1 - (e.embedding <=> (
                    SELECT embedding FROM embeddings.stop_embeddings
                    WHERE stop_name = :q LIMIT 1
                )) AS similarity
            FROM embeddings.stop_embeddings e
            JOIN (
                SELECT stop_id, stop_name, 'bus' AS feed, stop_lat, stop_lon
                FROM gtfs_bus.stops
                UNION ALL
                SELECT stop_id, stop_name, 'metro' AS feed, stop_lat, stop_lon
                FROM gtfs_metro.stops
            ) s ON s.stop_id = e.stop_id
            ORDER BY similarity DESC
            LIMIT :top_n
        """
        rows = _query(vec_sql, {"q": query_text, "top_n": top_n})
        if rows:
            return json.dumps({"method": "pgvector", "results": rows}, default=str)
    except Exception:
        pass  # Fall through to trigram

    # Trigram fallback
    trgm_sql = """
        SELECT
            stop_id,
            stop_name,
            feed,
            stop_lat AS lat,
            stop_lon AS lon,
            ROUND(similarity(stop_name, :q)::numeric, 3) AS similarity
        FROM (
            SELECT stop_id, stop_name, 'bus' AS feed, stop_lat, stop_lon
            FROM gtfs_bus.stops
            UNION ALL
            SELECT stop_id, stop_name, 'metro' AS feed, stop_lat, stop_lon
            FROM gtfs_metro.stops
        ) all_stops
        WHERE stop_name ILIKE :pattern
           OR similarity(stop_name, :q) > 0.2
        ORDER BY similarity DESC
        LIMIT :top_n
    """
    rows = _query(trgm_sql, {"q": query_text, "pattern": f"%{query_text}%", "top_n": top_n})
    return json.dumps({"method": "trigram", "results": rows}, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    log.info(f"Starting Propus MCP server (transport={transport})")
    if transport == "sse":
        mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    else:
        mcp.run(transport="stdio")