"""
compute.py
----------
Computes derived transit metrics per ward from GTFS data in PostGIS.
Updates derived.ward_metrics table.

Metrics computed:
  - bus_stop_count, metro_stop_count per ward  (ST_Within)
  - bus_route_count, metro_route_count per ward
  - peak_freq_mean, offpeak_freq_mean           (trips/hr per stop)
  - has_metro_within_1k                         (boolean per ward)
  - multimodal_gap_count                        (bus stops with no metro within 1km)
  - transit_score                               (0–1 composite, pre-RS version)

Usage:
    python pipeline/compute.py

Requires:
    DATABASE_URL in .env
    schema.sql applied
    ingest.py completed
"""

import os
import logging
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

# Peak hours in seconds from midnight
PEAK_AM_START  = 7  * 3600   # 07:00
PEAK_AM_END    = 10 * 3600   # 10:00
PEAK_PM_START  = 17 * 3600   # 17:00
PEAK_PM_END    = 20 * 3600   # 20:00
PEAK_DURATION  = 3.0         # hours (AM window = 3h, PM window = 3h)

OFFPEAK_START  = 10 * 3600   # 10:00
OFFPEAK_END    = 16 * 3600   # 16:00
OFFPEAK_HOURS  = 6.0

# Distance threshold for multimodal gap analysis (metres)
METRO_GAP_METRES = 1000

# Weekday filter for calendar join
WEEKDAY_FILTER = "(c.monday=1 OR c.tuesday=1 OR c.wednesday=1 OR c.thursday=1 OR c.friday=1)"


def run(engine, label: str, sql: str, params: dict = None):
    """Execute a SQL statement, log rows affected."""
    log.info(f"  {label}...")
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        conn.commit()
        log.info(f"  ✓ {label}: {result.rowcount} rows updated")


def compute_stop_counts(engine):
    """Count bus and metro stops within each ward polygon using ST_Within."""

    run(engine, "Bus stop count per ward", f"""
        UPDATE derived.ward_metrics wm
        SET bus_stop_count = subq.cnt
        FROM (
            SELECT w.ward_id, COUNT(s.stop_id) AS cnt
            FROM public.wards w
            LEFT JOIN gtfs_bus.stops s
                ON ST_Within(s.geom, w.geometry)
                AND s.location_type = 0
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)

    run(engine, "Metro stop count per ward", f"""
        UPDATE derived.ward_metrics wm
        SET metro_stop_count = subq.cnt
        FROM (
            SELECT w.ward_id, COUNT(s.stop_id) AS cnt
            FROM public.wards w
            LEFT JOIN gtfs_metro.stops s
                ON ST_Within(s.geom, w.geometry)
                AND s.location_type = 0
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)

    run(engine, "Total stop count", """
        UPDATE derived.ward_metrics
        SET total_stop_count = bus_stop_count + metro_stop_count
    """)


def compute_route_counts(engine):
    """
    Count distinct routes serving each ward.
    A route 'serves' a ward if it has at least one stop within that ward.
    """

    run(engine, "Bus route count per ward", """
        UPDATE derived.ward_metrics wm
        SET bus_route_count = subq.cnt
        FROM (
            SELECT w.ward_id, COUNT(DISTINCT t.route_id) AS cnt
            FROM public.wards w
            JOIN gtfs_bus.stops s ON ST_Within(s.geom, w.geometry)
            JOIN gtfs_bus.stop_times st ON st.stop_id = s.stop_id
            JOIN gtfs_bus.trips t ON t.trip_id = st.trip_id
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)

    run(engine, "Metro route count per ward", """
        UPDATE derived.ward_metrics wm
        SET metro_route_count = subq.cnt
        FROM (
            SELECT w.ward_id, COUNT(DISTINCT t.route_id) AS cnt
            FROM public.wards w
            JOIN gtfs_metro.stops s ON ST_Within(s.geom, w.geometry)
            JOIN gtfs_metro.stop_times st ON st.stop_id = s.stop_id
            JOIN gtfs_metro.trips t ON t.trip_id = st.trip_id
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)


def compute_service_frequency(engine):
    """
    Compute average weekday service frequency per stop per ward.

    Method:
      1. For each stop in a ward, count distinct trips calling at that stop
         within the peak / off-peak time window on weekday services.
      2. Divide by the window duration in hours → trips/hour per stop.
      3. Average across all stops in the ward.

    Uses departure_seconds (integer) for fast range filtering.
    Joins via calendar to restrict to weekday services.
    """

    # Peak AM frequency (07:00–10:00 weekday)
    run(engine, "Peak AM frequency per ward (bus)", f"""
        UPDATE derived.ward_metrics wm
        SET peak_freq_mean = subq.freq
        FROM (
            SELECT w.ward_id,
                   AVG(stop_trips.trip_count::float / {PEAK_DURATION}) AS freq
            FROM public.wards w
            JOIN gtfs_bus.stops s ON ST_Within(s.geom, w.geometry)
            LEFT JOIN LATERAL (
                SELECT COUNT(DISTINCT st.trip_id) AS trip_count
                FROM gtfs_bus.stop_times st
                JOIN gtfs_bus.trips t ON t.trip_id = st.trip_id
                JOIN gtfs_bus.calendar c ON c.service_id = t.service_id
                WHERE st.stop_id = s.stop_id
                  AND st.departure_seconds BETWEEN {PEAK_AM_START} AND {PEAK_AM_END}
                  AND {WEEKDAY_FILTER}
            ) stop_trips ON TRUE
            WHERE s.location_type = 0
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)

    # Off-peak frequency (10:00–16:00 weekday) — also covers metro
    run(engine, "Off-peak frequency per ward (bus)", f"""
        UPDATE derived.ward_metrics wm
        SET offpeak_freq_mean = subq.freq
        FROM (
            SELECT w.ward_id,
                   AVG(stop_trips.trip_count::float / {OFFPEAK_HOURS}) AS freq
            FROM public.wards w
            JOIN gtfs_bus.stops s ON ST_Within(s.geom, w.geometry)
            LEFT JOIN LATERAL (
                SELECT COUNT(DISTINCT st.trip_id) AS trip_count
                FROM gtfs_bus.stop_times st
                JOIN gtfs_bus.trips t ON t.trip_id = st.trip_id
                JOIN gtfs_bus.calendar c ON c.service_id = t.service_id
                WHERE st.stop_id = s.stop_id
                  AND st.departure_seconds BETWEEN {OFFPEAK_START} AND {OFFPEAK_END}
                  AND {WEEKDAY_FILTER}
            ) stop_trips ON TRUE
            WHERE s.location_type = 0
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)


def compute_metro_proximity(engine):
    """
    Flag wards where at least one metro station is within 1km of any point
    inside the ward polygon (using ST_DWithin on geography for metre accuracy).
    """

    run(engine, f"Metro within {METRO_GAP_METRES}m flag per ward", f"""
        UPDATE derived.ward_metrics wm
        SET has_metro_within_1k = subq.has_metro
        FROM (
            SELECT w.ward_id,
                   EXISTS (
                       SELECT 1
                       FROM gtfs_metro.stops ms
                       WHERE ms.location_type = 0
                         AND ST_DWithin(
                             w.geometry::geography,
                             ms.geom::geography,
                             {METRO_GAP_METRES}
                         )
                   ) AS has_metro
            FROM public.wards w
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)


def compute_multimodal_gaps(engine):
    """
    Count bus stops within each ward that have NO metro stop within 1km.
    These are the 'multimodal gap' stops — served by bus but not within
    walking distance of any metro station.
    High count = ward is bus-dependent with no metro connectivity.
    """

    run(engine, "Multimodal gap count per ward", f"""
        UPDATE derived.ward_metrics wm
        SET multimodal_gap = subq.gap_count > 0
        FROM (
            SELECT w.ward_id,
                   COUNT(bs.stop_id) AS gap_count
            FROM public.wards w
            JOIN gtfs_bus.stops bs ON ST_Within(bs.geom, w.geometry)
            WHERE bs.location_type = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM gtfs_metro.stops ms
                  WHERE ms.location_type = 0
                    AND ST_DWithin(
                        bs.geom::geography,
                        ms.geom::geography,
                        {METRO_GAP_METRES}
                    )
              )
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)

    # Also store the raw count for ranking
    run(engine, "Multimodal gap stop count per ward", f"""
        ALTER TABLE derived.ward_metrics
        ADD COLUMN IF NOT EXISTS multimodal_gap_count INTEGER DEFAULT 0
    """)

    run(engine, "Populate multimodal_gap_count", f"""
        UPDATE derived.ward_metrics wm
        SET multimodal_gap_count = subq.gap_count
        FROM (
            SELECT w.ward_id,
                   COUNT(bs.stop_id) AS gap_count
            FROM public.wards w
            JOIN gtfs_bus.stops bs ON ST_Within(bs.geom, w.geometry)
            WHERE bs.location_type = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM gtfs_metro.stops ms
                  WHERE ms.location_type = 0
                    AND ST_DWithin(
                        bs.geom::geography,
                        ms.geom::geography,
                        {METRO_GAP_METRES}
                    )
              )
            GROUP BY w.ward_id
        ) subq
        WHERE wm.ward_id = subq.ward_id
    """)


def compute_transit_score(engine):
    """
    Compute a composite transit accessibility score per ward (0–1).

    This is the pre-RS version — uses only GTFS metrics.
    It will be revised by rs_merge.py once WorldPop and Sentinel-2 data
    are loaded.

    Formula (equal weights for prototype, tune later):
        score = 0.35 * norm(total_stop_count)
              + 0.25 * norm(peak_freq_mean)
              + 0.20 * norm(bus_route_count + metro_route_count)
              + 0.20 * has_metro_within_1k::int

    Normalisation: min-max per column across all wards.
    Result stored in transit_score.
    """

    # Step 1: compute normalised sub-scores in a CTE, then write
    run(engine, "Transit score (GTFS composite)", """
        WITH bounds AS (
            SELECT
                MIN(total_stop_count)                   AS min_stops,
                MAX(total_stop_count)                   AS max_stops,
                MIN(COALESCE(peak_freq_mean, 0))        AS min_freq,
                MAX(COALESCE(peak_freq_mean, 0))        AS max_freq,
                MIN(bus_route_count + metro_route_count) AS min_routes,
                MAX(bus_route_count + metro_route_count) AS max_routes
            FROM derived.ward_metrics
        ),
        normed AS (
            SELECT
                wm.ward_id,
                CASE WHEN b.max_stops  > b.min_stops
                     THEN (wm.total_stop_count - b.min_stops)::float
                          / (b.max_stops - b.min_stops)
                     ELSE 0 END AS n_stops,

                CASE WHEN b.max_freq   > b.min_freq
                     THEN (COALESCE(wm.peak_freq_mean,0) - b.min_freq)::float
                          / (b.max_freq - b.min_freq)
                     ELSE 0 END AS n_freq,

                CASE WHEN b.max_routes > b.min_routes
                     THEN ((wm.bus_route_count + wm.metro_route_count) - b.min_routes)::float
                          / (b.max_routes - b.min_routes)
                     ELSE 0 END AS n_routes,

                COALESCE(wm.has_metro_within_1k::int, 0)::float AS n_metro

            FROM derived.ward_metrics wm
            CROSS JOIN bounds b
        )
        UPDATE derived.ward_metrics wm
        SET transit_score = ROUND(
            (0.35 * n.n_stops
           + 0.25 * n.n_freq
           + 0.20 * n.n_routes
           + 0.20 * n.n_metro)::numeric,
        4)
        FROM normed n
        WHERE wm.ward_id = n.ward_id
    """)


def log_summary(engine):
    """Print a ranked summary of the 10 worst and 10 best served wards."""
    log.info("\n── Transit score summary ──")

    with engine.connect() as conn:
        # Top 10 worst
        worst = conn.execute(text("""
            SELECT ward_name, total_stop_count, peak_freq_mean,
                   has_metro_within_1k, transit_score
            FROM derived.ward_metrics
            ORDER BY transit_score ASC NULLS LAST
            LIMIT 10
        """)).fetchall()

        log.info("\n  10 worst-served wards:")
        log.info(f"  {'Ward':<30} {'Stops':>6} {'Freq/hr':>8} {'Metro':>6} {'Score':>7}")
        log.info("  " + "-" * 62)
        for row in worst:
            metro = "Yes" if row[3] else "No"
            freq  = f"{row[2]:.2f}" if row[2] is not None else "N/A"
            score = f"{row[4]:.4f}" if row[4] is not None else "N/A"
            log.info(f"  {row[0]:<30} {row[1]:>6} {freq:>8} {metro:>6} {score:>7}")

        # Top 10 best
        best = conn.execute(text("""
            SELECT ward_name, total_stop_count, peak_freq_mean,
                   has_metro_within_1k, transit_score
            FROM derived.ward_metrics
            ORDER BY transit_score DESC NULLS LAST
            LIMIT 10
        """)).fetchall()

        log.info("\n  10 best-served wards:")
        log.info(f"  {'Ward':<30} {'Stops':>6} {'Freq/hr':>8} {'Metro':>6} {'Score':>7}")
        log.info("  " + "-" * 62)
        for row in best:
            metro = "Yes" if row[3] else "No"
            freq  = f"{row[2]:.2f}" if row[2] is not None else "N/A"
            score = f"{row[4]:.4f}" if row[4] is not None else "N/A"
            log.info(f"  {row[0]:<30} {row[1]:>6} {freq:>8} {metro:>6} {score:>7}")


def verify(engine):
    """Quick null-check on derived.ward_metrics."""
    log.info("\n── Verification ──")
    checks = [
        ("bus_stop_count",    "IS NOT NULL"),
        ("transit_score",     "IS NOT NULL"),
        ("has_metro_within_1k","IS NOT NULL"),
        ("peak_freq_mean",    "IS NOT NULL"),
    ]
    with engine.connect() as conn:
        for col, condition in checks:
            populated = conn.execute(
                text(f"SELECT COUNT(*) FROM derived.ward_metrics WHERE {col} {condition}")
            ).scalar()
            total = conn.execute(
                text("SELECT COUNT(*) FROM derived.ward_metrics")
            ).scalar()
            pct = populated / total * 100 if total else 0
            status = "✓" if populated == total else f"⚠ {total - populated} nulls"
            log.info(f"  {status}  {col}: {populated}/{total} ({pct:.0f}%)")


def main():
    log.info("=" * 60)
    log.info("Propus — Derived Metrics Compute Script")
    log.info("=" * 60)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    log.info("\n── Step 1: Stop counts ──")
    compute_stop_counts(engine)

    log.info("\n── Step 2: Route counts ──")
    compute_route_counts(engine)

    log.info("\n── Step 3: Service frequency ──")
    compute_service_frequency(engine)

    log.info("\n── Step 4: Metro proximity ──")
    compute_metro_proximity(engine)

    log.info("\n── Step 5: Multimodal gaps ──")
    compute_multimodal_gaps(engine)

    log.info("\n── Step 6: Transit score ──")
    compute_transit_score(engine)

    log.info("\n── Step 7: Verification ──")
    verify(engine)
    log_summary(engine)

    log.info("\n" + "=" * 60)
    log.info("✓ compute.py complete. GTFS-derived metrics ready.")
    log.info("  Next: python pipeline/rs_worldpop.py")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
