"""
agent/agent.py  (v2 — native ADK tools, no MCP subprocess)
-----------------------------------------------------------
Google ADK agent for Delhi Transit Intelligence (Propus / Quiver).

Root cause of TimeoutError: MCPToolset launches server.py as a subprocess
via stdio. SQLAlchemy + geopandas take 2-4s to import, but ADK's MCP
handshake times out before the first byte appears on stdout.

Fix: Define all 10 tools as native Python functions passed directly to
Agent(tools=[...]). ADK wraps them automatically — no subprocess, no timeout.

The MCP server (mcp_server/server.py) is still useful for Cloud Run where
you want the server running as a separate container. For local dev, use this.

Usage:
    # Interactive terminal
    python agent/agent.py

    # Single query
    python agent/agent.py --query "Which wards have the worst transit access?"

    # As module
    from agent.agent import create_runner, run_query

Requires:
    pip install google-adk google-generativeai sqlalchemy psycopg2-binary
                python-dotenv
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MODEL CONFIG
# ---------------------------------------------------------------------------

MODEL_CHAIN = [
    "gemini-2.5-flash",        # primary (fast + best quality)
    "gemini-2.0-flash",        # different quota bucket (important fallback)
    "gemini-2.5-flash-lite",   # lightweight fallback
    "gemini-2.0-flash-lite",   # cheapest / most permissive
    "gemini-2.5-pro",          # last resort (expensive, slower)
]

_current_model_idx = 0
_model_failures = {m: 0 for m in MODEL_CHAIN}

FAILURE_THRESHOLD = 2
BASE_DELAY = 2
MAX_DELAY = 30
TIMEOUT_SECONDS = 25

# ---------------------------------------------------------------------------
# SHARED SESSION SERVICE (CRITICAL FIX)
# ---------------------------------------------------------------------------

from google.adk.sessions import InMemorySessionService
_session_service = InMemorySessionService()


# ---------------------------------------------------------------------------
# DB ENGINE (SAFE POOLING)
# ---------------------------------------------------------------------------

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            os.environ["DATABASE_URL"],
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            pool_recycle=1800,
        )
    return _engine

def _q(sql: str, params: dict | None = None) -> list[dict]:
    from decimal import Decimal
    import datetime

    def _coerce(v):
        if isinstance(v, Decimal): return float(v)
        if isinstance(v, (datetime.date, datetime.datetime)): return v.isoformat()
        return v

    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        cols = list(result.keys())
        return [{c: _coerce(v) for c, v in zip(cols, row)} for row in result.fetchall()]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the Delhi Transit Intelligence Agent for Propus/Quiver,
a WRI India transit equity analysis platform.

REAL DATA IN DATABASE:
- 10,559 bus stops, 2,403 bus routes (DTC network)
- 262 metro stops, 36 metro lines (DMRC)
- 290 Delhi ward polygons with computed transit metrics
- WorldPop 2020 population (loaded after rs_worldpop.py runs)
- Sentinel-2 NDVI/NDBI urban stress indices (loaded after rs_sentinel.py runs)

KEY FACTS:
- Stop IDs: bus stops = 'bus_<n>', metro stops = 'metro_<n>'
- Transit scores: 0–1 (higher = better)
- 23 wards have zero bus stops (Sangam Vihar cluster, Tigri, Raj Nagar etc.)
- urban_stress_index > 0.7 = highest intervention priority

WARD NAMES — CRITICAL:
Ward names in the DB are ALL CAPS and use official MCD names, NOT common
colloquial names. Popular area names map like this:
  "Dwarka"     → search "DWARKA" (will match DWARKA SECTOR 6 etc.)
  "Seemapuri"  → search "SEEMAPURI"
  "Rohini"     → search "ROHINI"
  "Mustafabad" → exact match "MUSTAFABAD" ✓
  "Sangam Vihar" → matches SANGAM VIHAR, SANGAM VIHAR CENTRAL etc.

When a user mentions an area name:
1. ALWAYS call get_ward_rs_profile(area_name) first — it uses LIKE matching
   and will find partial matches. NEVER tell the user the area doesn't exist
   without trying this tool first.
2. If get_ward_rs_profile returns multiple matches (e.g. DWARKA SECTOR 1..20),
   summarise them as a group or pick the most central one.
3. For "how far is X from metro" → call get_ward_rs_profile(X) to get the
   ward centroid, then interpret has_metro_within_1k and multimodal_gap_count.
   DO NOT call semantic_stop_search for area/neighbourhood queries.
4. For "compare X vs Y" → call compare_transit_access(X, Y) which handles
   LIKE matching and aggregates multiple ward matches automatically.

TOOL ROUTING:
- "How far is [area] from metro?" → get_ward_rs_profile(area_name)
- "Tell me about [area]"          → get_ward_rs_profile(area_name)
- "Compare [A] vs [B]"            → compare_transit_access(A, B)
- "Underserved areas"             → find_underserved_areas(metric, threshold)
- "Stops near lat/lon"            → get_nearby_stops(lat, lon, radius_m)
- "Route [id] details"            → get_route_coverage(route_id)
- "Metro [station] catchment"     → get_metro_catchment(station_name)
- "Frequency at stop [id]"        → get_frequency_at_stop(stop_id, start, end)
- "Bus stops far from metro"      → find_multimodal_gaps(max_distance_m)
- "Search for stop named [x]"     → semantic_stop_search(query_text)
- "Show full Delhi map"           → get_urban_stress_map()

EQUITY FRAMING: Always prefer stops_per_10k over raw counts when available.
Note when RS metrics (ndvi_mean, ndbi_mean, pop_total) are NULL — means
the RS pipeline hasn't run yet for that ward.
"""

# ---------------------------------------------------------------------------
# Tools — plain Python functions, ADK wraps them automatically
# ---------------------------------------------------------------------------

def get_nearby_stops(lat: float, lon: float, radius_m: int = 500) -> dict:
    """Return bus and metro stops within radius_m metres of (lat, lon).

    Args:
        lat: Latitude (WGS84 decimal degrees).
        lon: Longitude (WGS84 decimal degrees).
        radius_m: Search radius in metres (default 500).

    Returns:
        count and list of nearby stops sorted by distance.
    """
    rows = _q("""
        SELECT stop_id, stop_name, feed,
               stop_lat AS lat, stop_lon AS lon,
               ROUND(ST_Distance(
                   geom::geography,
                   ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
               ))::int AS distance_m
        FROM (
            SELECT stop_id, stop_name, 'bus'   AS feed, stop_lat, stop_lon, geom
            FROM gtfs_bus.stops
            UNION ALL
            SELECT stop_id, stop_name, 'metro' AS feed, stop_lat, stop_lon, geom
            FROM gtfs_metro.stops
        ) s
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
            :r
        )
        ORDER BY distance_m ASC
        LIMIT 50
    """, {"lat": lat, "lon": lon, "r": radius_m})
    return {"count": len(rows), "stops": rows}


def find_underserved_areas(
    metric: str = "transit_score",
    threshold: float = 0.15,
    top_n: int = 15,
) -> dict:
    """Find Delhi wards underserved by public transit.

    Args:
        metric: 'transit_score', 'stops_per_10k', or 'bus_stop_count'.
        threshold: Max value of metric to count as underserved.
        top_n: Number of wards to return.

    Returns:
        List of underserved wards with transit and population metrics.
    """
    allowed = {"transit_score", "stops_per_10k", "bus_stop_count"}
    if metric not in allowed:
        return {"error": f"metric must be one of {sorted(allowed)}"}
    rows = _q(f"""
        SELECT ward_id, ward_name, bus_stop_count, metro_stop_count,
               ROUND(peak_freq_mean::numeric, 2)   AS peak_freq_mean,
               has_metro_within_1k,
               ROUND(transit_score::numeric, 4)    AS transit_score,
               pop_total,
               ROUND(stops_per_10k::numeric, 4)    AS stops_per_10k,
               multimodal_gap_count
        FROM derived.ward_metrics
        WHERE {metric} <= :t OR {metric} IS NULL
        ORDER BY {metric} ASC NULLS FIRST
        LIMIT :n
    """, {"t": threshold, "n": top_n})
    return {"count": len(rows), "metric": metric, "wards": rows}


def get_route_coverage(route_id: str) -> dict:
    """Return all stops on a bus/metro route with shape GeoJSON.

    Args:
        route_id: GTFS route_id e.g. 'bus_142' or 'metro_BL'.

    Returns:
        Route metadata, ordered stop list, and shape GeoJSON LineString.
    """
    schema = "gtfs_metro" if route_id.startswith("metro_") else "gtfs_bus"
    routes = _q(
        f"SELECT route_id, route_short_name, route_long_name, route_type "
        f"FROM {schema}.routes WHERE route_id = :rid",
        {"rid": route_id},
    )
    if not routes:
        return {"error": f"Route '{route_id}' not found"}

    stops = _q(f"""
        SELECT DISTINCT ON (st.stop_sequence, s.stop_id)
            st.stop_sequence, s.stop_id, s.stop_name,
            s.stop_lat AS lat, s.stop_lon AS lon
        FROM {schema}.trips t
        JOIN {schema}.stop_times st ON st.trip_id = t.trip_id
        JOIN {schema}.stops s       ON s.stop_id  = st.stop_id
        WHERE t.route_id = :rid
        ORDER BY st.stop_sequence ASC
        LIMIT 200
    """, {"rid": route_id})

    shapes = _q(
        "SELECT ST_AsGeoJSON(geom)::json AS geometry "
        "FROM derived.route_geometries WHERE route_id = :rid LIMIT 1",
        {"rid": route_id},
    )
    return {
        "route": routes[0],
        "stop_count": len(stops),
        "stops": stops,
        "shape_geojson": shapes[0]["geometry"] if shapes else None,
    }


def compare_transit_access(area_a: str, area_b: str) -> dict:
    """Side-by-side transit metrics for two Delhi wards.

    Args:
        area_a: Ward name (case-insensitive) or ward_id.
        area_b: Ward name or ward_id to compare against.

    Returns:
        Metrics for both wards including RS indices when available.
    """
    sql = """
        SELECT ward_id, ward_name, bus_stop_count, metro_stop_count,
               peak_freq_mean, offpeak_freq_mean,
               has_metro_within_1k, multimodal_gap_count,
               transit_score, pop_total, stops_per_10k,
               ndvi_mean, ndbi_mean, urban_stress_index
        FROM derived.ward_metrics
        WHERE UPPER(ward_name) = UPPER(:exact)
           OR ward_id = :exact
           OR UPPER(ward_name) LIKE UPPER(:pat)
        ORDER BY ward_name
    """

    def _fetch(name: str) -> dict:
        rows = _q(sql, {"exact": name, "pat": f"%{name}%"})
        if not rows:
            return {"error": f"No wards found matching '{name}'. Try a different spelling."}
        if len(rows) == 1:
            r = rows[0]
            return {k: (round(float(v), 4) if isinstance(v, (float, __import__("decimal").Decimal)) else v)
                    for k, v in r.items()}
        # Multiple wards matched (e.g. DWARKA SECTOR 1..20) — aggregate
        import statistics
        def _avg(k):
            v = [r[k] for r in rows if r[k] is not None]
            return round(statistics.mean(v), 4) if v else None
        def _sum(k):
            v = [r[k] for r in rows if r[k] is not None]
            return sum(v) if v else None
        names = [r["ward_name"] for r in rows]
        return {
            "area_name":            name,
            "wards_matched":        len(rows),
            "matched_wards":        names,
            "bus_stop_count":       _sum("bus_stop_count"),
            "metro_stop_count":     _sum("metro_stop_count"),
            "peak_freq_mean":       _avg("peak_freq_mean"),
            "has_metro_within_1k":  any(r["has_metro_within_1k"] for r in rows),
            "multimodal_gap_count": _sum("multimodal_gap_count"),
            "transit_score":        _avg("transit_score"),
            "pop_total":            _sum("pop_total"),
            "stops_per_10k":        _avg("stops_per_10k"),
            "ndvi_mean":            _avg("ndvi_mean"),
            "ndbi_mean":            _avg("ndbi_mean"),
            "urban_stress_index":   _avg("urban_stress_index"),
            "note": f"Aggregated across {len(rows)} wards: "
                    + ", ".join(names[:5]) + (" ..." if len(names) > 5 else ""),
        }

    return {area_a: _fetch(area_a), area_b: _fetch(area_b)}


def get_metro_catchment(station_name: str, radius_m: int = 500) -> dict:
    """Return the catchment zone of a metro station.

    Args:
        station_name: Station name (partial match supported).
        radius_m: Buffer radius in metres (default 500).

    Returns:
        Station info, bus stops in zone, intersecting wards, estimated pop.
    """
    stations = _q(
        "SELECT stop_id, stop_name, stop_lat AS lat, stop_lon AS lon "
        "FROM gtfs_metro.stops WHERE stop_name ILIKE :pat ORDER BY stop_name LIMIT 5",
        {"pat": f"%{station_name}%"},
    )
    if not stations:
        return {"error": f"No metro station matching '{station_name}'"}
    s = stations[0]

    wards = _q("""
        SELECT w.ward_id, w.ward_name, wm.pop_total, wm.transit_score
        FROM public.wards w
        LEFT JOIN derived.ward_metrics wm ON wm.ward_id = w.ward_id
        WHERE ST_DWithin(
            w.geometry::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r
        )
    """, {"lat": s["lat"], "lon": s["lon"], "r": radius_m})

    bus = _q("""
        SELECT COUNT(*) AS n FROM gtfs_bus.stops
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r
        )
    """, {"lat": s["lat"], "lon": s["lon"], "r": radius_m})

    total_pop = sum(w.get("pop_total") or 0 for w in wards)
    return {
        "station": s,
        "radius_m": radius_m,
        "bus_stops_in_catchment": bus[0]["n"],
        "intersecting_wards": wards,
        "estimated_population_covered": total_pop or "WorldPop not yet loaded",
    }


def get_frequency_at_stop(
    stop_id: str,
    start_time: str = "08:00:00",
    end_time: str = "10:00:00",
) -> dict:
    """Return service frequency at a stop during a time window.

    Args:
        stop_id: GTFS stop_id e.g. 'bus_1234' or 'metro_45'.
        start_time: Window start HH:MM:SS (24h, may exceed 24:00).
        end_time: Window end HH:MM:SS.

    Returns:
        trip_count, trips_per_hour, first and last arrivals in window.
    """
    schema = "gtfs_metro" if stop_id.startswith("metro_") else "gtfs_bus"
    info = _q(
        f"SELECT stop_id, stop_name, stop_lat AS lat, stop_lon AS lon "
        f"FROM {schema}.stops WHERE stop_id = :sid",
        {"sid": stop_id},
    )
    if not info:
        return {"error": f"Stop '{stop_id}' not found"}

    def _s(t: str) -> int:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)

    freq = _q(f"""
        SELECT COUNT(DISTINCT trip_id) AS trip_count,
               MIN(arrival_time) AS first_arrival, MAX(arrival_time) AS last_arrival
        FROM {schema}.stop_times
        WHERE stop_id = :sid AND arrival_seconds >= :s0 AND arrival_seconds < :s1
    """, {"sid": stop_id, "s0": _s(start_time), "s1": _s(end_time)})

    tc = freq[0]["trip_count"] if freq else 0
    wh = (_s(end_time) - _s(start_time)) / 3600
    return {
        "stop": info[0],
        "window": {"start": start_time, "end": end_time, "hours": wh},
        "trip_count": tc,
        "trips_per_hour": round(tc / wh, 2) if wh > 0 else 0,
        "first_arrival": freq[0]["first_arrival"] if freq else None,
        "last_arrival":  freq[0]["last_arrival"]  if freq else None,
    }


def find_multimodal_gaps(max_distance_m: int = 1000, top_n: int = 20) -> dict:
    """Find bus stops with no metro within max_distance_m metres.

    Args:
        max_distance_m: Gap threshold in metres (default 1000).
        top_n: Number of results to return.

    Returns:
        Bus stops that lack metro access, sorted by distance to nearest metro.
    """
    rows = _q("""
        WITH nearest AS (
            SELECT b.stop_id, b.stop_name,
                   b.stop_lat AS lat, b.stop_lon AS lon,
                   (SELECT ROUND(ST_Distance(b.geom::geography, m.geom::geography))::int
                    FROM gtfs_metro.stops m
                    ORDER BY b.geom::geography <-> m.geom::geography
                    LIMIT 1) AS dist_to_metro_m
            FROM gtfs_bus.stops b
        )
        SELECT n.stop_id, n.stop_name, n.lat, n.lon,
               n.dist_to_metro_m, w.ward_name
        FROM nearest n
        LEFT JOIN public.wards w ON ST_Within(
            ST_SetSRID(ST_MakePoint(n.lon, n.lat), 4326), w.geometry
        )
        WHERE n.dist_to_metro_m > :d
        ORDER BY n.dist_to_metro_m DESC
        LIMIT :n
    """, {"d": max_distance_m, "n": top_n})
    return {"max_distance_m": max_distance_m, "count": len(rows), "stops": rows}


def get_ward_rs_profile(ward_name: str) -> dict:
    """Full remote sensing and transit profile for a Delhi ward.

    Args:
        ward_name: Ward name (case-insensitive, partial match) or ward_id.

    Returns:
        Transit score, population, stops/10k, NDVI, NDBI, urban stress index.
    """
    rows = _q("""
        SELECT wm.ward_id, wm.ward_name,
               wm.bus_stop_count, wm.metro_stop_count,
               ROUND(wm.peak_freq_mean::numeric, 2)     AS peak_freq_mean,
               wm.has_metro_within_1k, wm.multimodal_gap_count,
               ROUND(wm.transit_score::numeric, 4)      AS transit_score,
               wm.pop_total,
               ROUND(wm.stops_per_10k::numeric, 4)      AS stops_per_10k,
               ROUND(wm.ndvi_mean::numeric, 4)          AS ndvi_mean,
               ROUND(wm.ndbi_mean::numeric, 4)          AS ndbi_mean,
               ROUND(wm.urban_stress_index::numeric, 4) AS urban_stress_index,
               ST_AsGeoJSON(w.geometry)::json           AS geometry
        FROM derived.ward_metrics wm
        JOIN public.wards w ON w.ward_id = wm.ward_id
        WHERE UPPER(wm.ward_name) LIKE UPPER(:pat) OR wm.ward_id = :raw
        ORDER BY wm.ward_name LIMIT 3
    """, {"pat": f"%{ward_name}%", "raw": ward_name})
    if not rows:
        return {"error": f"No ward found matching '{ward_name}'"}
    return {"matches": len(rows), "wards": rows}


def get_urban_stress_map() -> dict:
    """GeoJSON FeatureCollection of all wards for choropleth rendering.

    Properties include transit_score, urban_stress_index, NDVI, NDBI,
    population, and stops_per_10k for every ward.

    Returns:
        GeoJSON FeatureCollection suitable for folium.GeoJson().
    """
    rows = _q("""
        SELECT w.ward_id, w.ward_name,
               wm.transit_score, wm.bus_stop_count,
               wm.pop_total, wm.stops_per_10k,
               wm.ndvi_mean, wm.ndbi_mean, wm.urban_stress_index,
               wm.has_metro_within_1k,
               ST_AsGeoJSON(w.geometry)::json AS geometry
        FROM public.wards w
        JOIN derived.ward_metrics wm ON wm.ward_id = w.ward_id
        ORDER BY w.ward_name
    """)
    features = [
        {"type": "Feature",
         "geometry": r.pop("geometry", None),
         "properties": dict(r)}
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


def semantic_stop_search(query_text: str, top_n: int = 10) -> dict:
    """Search stops by name using fuzzy matching.

    Tries pgvector cosine similarity if embeddings exist, falls back to
    pg_trgm trigram similarity for common Delhi place-name variants.

    Args:
        query_text: Place name or neighbourhood to search for.
        top_n: Number of results (default 10).

    Returns:
        Matching stops with similarity scores and method used.
    """
    try:
        rows = _q("""
            SELECT stop_id, stop_name, feed,
                   1 - (embedding <=> (
                       SELECT embedding FROM embeddings.stop_embeddings
                       WHERE stop_name ILIKE :q LIMIT 1
                   )) AS similarity
            FROM embeddings.stop_embeddings
            ORDER BY similarity DESC LIMIT :n
        """, {"q": f"%{query_text}%", "n": top_n})
        if rows:
            return {"method": "pgvector", "results": rows}
    except Exception:
        pass

    rows = _q("""
        SELECT stop_id, stop_name, feed,
               stop_lat AS lat, stop_lon AS lon,
               ROUND(similarity(stop_name, :q)::numeric, 3) AS similarity
        FROM (
            SELECT stop_id, stop_name, 'bus'   AS feed, stop_lat, stop_lon FROM gtfs_bus.stops
            UNION ALL
            SELECT stop_id, stop_name, 'metro' AS feed, stop_lat, stop_lon FROM gtfs_metro.stops
        ) all_stops
        WHERE stop_name ILIKE :pat OR similarity(stop_name, :q) > 0.2
        ORDER BY similarity DESC LIMIT :n
    """, {"q": query_text, "pat": f"%{query_text}%", "n": top_n})
    return {"method": "trigram", "results": rows}


# ---------------------------------------------------------------------------
# RATE LIMIT DETECTION
# ---------------------------------------------------------------------------

def _is_rate_limit(exc: Exception) -> bool:
    try:
        from google.api_core.exceptions import ResourceExhausted, TooManyRequests
        if isinstance(exc, (ResourceExhausted, TooManyRequests)):
            return True
    except ImportError:
        pass

    msg = str(exc).lower()
    return any(k in msg for k in ("resource exhausted", "429", "quota", "rate limit"))

def _is_not_found(e: Exception) -> bool:
    return "not found" in str(e).lower()

def _is_quota(e: Exception) -> bool:
    return "quota" in str(e).lower() or "429" in str(e)

# ---------------------------------------------------------------------------
# Agent + Runner
# ---------------------------------------------------------------------------

def create_agent(model: str):
    from google.adk.agents import Agent

    agent = Agent(
        name="delhi_transit_agent",
        model=model,
        description="Delhi transit equity intelligence — real GTFS + PostGIS data",
        instruction=SYSTEM_PROMPT,
        tools=[
            get_nearby_stops,
            find_underserved_areas,
            get_route_coverage,
            compare_transit_access,
            get_metro_catchment,
            get_frequency_at_stop,
            find_multimodal_gaps,
            get_ward_rs_profile,
            get_urban_stress_map,
            semantic_stop_search,
        ],
    )
    log.info("Agent ready — model=%s", model)
    return agent


def create_runner(model: str = None):
    from google.adk.runners import Runner

    if model is None:
        model = MODEL_CHAIN[_current_model_idx]

    return Runner(
        agent=create_agent(model),
        app_name="propus_transit",
        session_service=_session_service,
    )

# ---------------------------------------------------------------------------
# QUERY EXECUTION (WITH TIMEOUT)
# ---------------------------------------------------------------------------

async def _run_query_inner(query: str, session_id: str, user_id: str, runner):
    from google.genai import types

    try:
        await _session_service.create_session(
            app_name="propus_transit",
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        pass

    content = types.Content(role="user", parts=[types.Part(text=query)])

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response():
            if event.content and event.content.parts:
                return event.content.parts[0].text

    return ""

async def run_query(query: str, session_id="default", user_id="user", runner=None):
    if runner is None:
        runner = create_runner(MODEL_CHAIN[0])

    try:
        return await asyncio.wait_for(
            _run_query_inner(query, session_id, user_id, runner),
            timeout=TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise Exception("LLM request timeout")

# ---------------------------------------------------------------------------
# FALLBACK LOGIC
# ---------------------------------------------------------------------------

async def run_with_fallback(query: str, session_id="default", user_id="user"):
    global _current_model_idx

    for model_idx in range(_current_model_idx, len(MODEL_CHAIN)):
        model = MODEL_CHAIN[model_idx]
        runner = create_runner(model)

        for attempt in range(3):
            try:
                result = await run_query(query, session_id, user_id, runner)

                # Reset failure count on success
                _model_failures[model] = 0

                # Reset to best model if recovered
                if _current_model_idx > 0:
                    log.info("Recovered on %s → resetting to primary model", model)
                    _current_model_idx = 0

                return result

            except Exception as exc:
                if _is_rate_limit(exc):
                    _model_failures[model] += 1

                    wait = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                    wait += random.uniform(0, 1)

                    log.warning(
                        "Rate limit on %s (attempt %d) → waiting %.2fs",
                        model, attempt + 1, wait,
                    )

                    await asyncio.sleep(wait)

                    if _model_failures[model] >= FAILURE_THRESHOLD:
                        log.warning("Switching model due to repeated failures: %s", model)
                        _current_model_idx = min(model_idx + 1, len(MODEL_CHAIN) - 1)
                        break

                else:
                    raise
        log.info("Using model: %s", model)
    return "All models are currently rate-limited. Please try again later."

# ---------------------------------------------------------------------------
# Interactive terminal
# ---------------------------------------------------------------------------

async def _interactive_session():
    from google.genai import types

    print("\n" + "=" * 60)
    print("  Propus — Delhi Transit Intelligence Agent")
    print("  Type 'exit' to quit")
    print("=" * 60)
    print("\nTry:")
    print("  Which wards have the worst transit access?")
    print("  Find stops near Connaught Place (lat 28.6315, lon 77.2167)")
    print("  Compare Vasant Vihar and Vasantkunj")
    print("  Which bus stops are farthest from any metro?\n")

    runner = create_runner()
    sid, uid = "terminal", "terminal_user"
    await runner.session_service.create_session(
        app_name="propus_transit", user_id=uid, session_id=sid,
    )

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            print("Goodbye.")
            break

        content = types.Content(role="user", parts=[types.Part(text=user_input)])
        print("Agent: ", end="", flush=True)

        async for event in runner.run_async(user_id=uid, session_id=sid, new_message=content):
            if event.is_final_response():
                if event.content and event.content.parts:
                    print(event.content.parts[0].text)
            elif hasattr(event, "tool_call") and event.tool_call:
                print(f"\n  [→ {getattr(event.tool_call, 'name', 'tool')}]",
                      end="", flush=True)
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", "-q", default=None)
    args = parser.parse_args()

    if args.query:
        async def _single():
            print(f"\nAgent: {await run_query(args.query)}\n")
        asyncio.run(_single())
    else:
        asyncio.run(_interactive_session())