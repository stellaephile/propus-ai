"""
rs_merge.py
-----------
Final RS merge step. Assumes rs_sentinel.py and rs_worldpop.py have run.

What this script does:
    1. Verify ndvi_mean, ndbi_mean, transit_score, pop_total are populated
    2. Compute urban_stress_index per ward:
           normalise(1 − ndvi_mean)      ← low green cover = bad
         + normalise(ndbi_mean)          ← high built-up = bad
         + normalise(1 − transit_score)  ← low transit = bad
       Each component normalised 0–1 across all wards, then averaged.
       Final index is also 0–1. Wards scoring > 0.7 = highest priority.
    3. Compute revised_accessibility_score (RS-weighted transit score):
           transit_score × (1 − 0.3 × normalised_stress)
       Penalises transit scores in high-stress, high-density wards.
    4. Write both columns back to derived.ward_metrics.
    5. Print a verification summary with best/worst wards.

Usage:
    python pipeline/rs_merge.py

    # Recompute without re-running the RS pipelines:
    python pipeline/rs_merge.py --force

Requires:
    pip install sqlalchemy psycopg2-binary pandas python-dotenv
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def _normalise(series: pd.Series) -> pd.Series:
    """Min-max normalise a Series to [0, 1]. NaN stays NaN."""
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(0.5, index=series.index)
    return (series - mn) / (mx - mn)


# ---------------------------------------------------------------------------
# Step 1 — Load ward metrics
# ---------------------------------------------------------------------------

def load_ward_metrics(engine) -> pd.DataFrame:
    df = pd.read_sql("""
        SELECT
            ward_id,
            ward_name,
            transit_score,
            ndvi_mean,
            ndbi_mean,
            pop_total,
            stops_per_10k,
            bus_stop_count
        FROM derived.ward_metrics
        ORDER BY ward_name
    """, engine)
    log.info(f"Loaded {len(df)} wards from derived.ward_metrics")
    return df


def verify_inputs(df: pd.DataFrame) -> None:
    """Log population status and warn about missing columns."""
    log.info("\n── Input verification ──")
    for col in ["transit_score", "ndvi_mean", "ndbi_mean", "pop_total", "stops_per_10k"]:
        n = df[col].notna().sum()
        pct = n / len(df) * 100
        status = "✓" if n > 0 else "✗ EMPTY"
        log.info(f"  {status}  {col:<22}: {n}/{len(df)} ({pct:.0f}%)")

    missing_ndvi = df["ndvi_mean"].isna().sum()
    missing_ndbi = df["ndbi_mean"].isna().sum()
    if missing_ndvi > 0 or missing_ndbi > 0:
        log.warning(
            f"  {missing_ndvi} wards missing ndvi_mean, "
            f"{missing_ndbi} missing ndbi_mean. "
            f"Run rs_sentinel.py first."
        )


# ---------------------------------------------------------------------------
# Step 2 — Compute urban_stress_index
# ---------------------------------------------------------------------------

def compute_urban_stress_index(df: pd.DataFrame) -> pd.DataFrame:
    """
    Urban Stress Index = mean of three normalised components:
        C1 = normalise(1 − ndvi_mean)     low green cover → stress
        C2 = normalise(ndbi_mean)          high built-up  → stress
        C3 = normalise(1 − transit_score)  low transit    → stress

    Wards missing NDVI/NDBI fall back to a transit-only score
    (C3 only, clearly flagged). Once RS data is loaded for all wards,
    re-run this script to get the full three-component index.
    """
    log.info("\n── Step 2: Computing urban_stress_index ──")

    has_rs = df["ndvi_mean"].notna() & df["ndbi_mean"].notna()
    has_transit = df["transit_score"].notna()

    log.info(f"  Wards with full RS + transit data : {(has_rs & has_transit).sum()}")
    log.info(f"  Wards with transit only           : {(~has_rs & has_transit).sum()}")
    log.info(f"  Wards with no data                : {(~has_rs & ~has_transit).sum()}")

    df = df.copy()

    # Compute components on full dataset for consistent normalisation
    # Use fillna(median) for normalisation range calculation,
    # but keep track of which wards had real RS data
    ndvi_filled = df["ndvi_mean"].fillna(df["ndvi_mean"].median())
    ndbi_filled = df["ndbi_mean"].fillna(df["ndbi_mean"].median())
    ts_filled   = df["transit_score"].fillna(df["transit_score"].median())

    c1 = _normalise(1 - ndvi_filled)   # low green  → high stress
    c2 = _normalise(ndbi_filled)       # high built → high stress
    c3 = _normalise(1 - ts_filled)     # low transit→ high stress

    # Full three-component index for wards with RS data
    df["urban_stress_index"] = (c1 + c2 + c3) / 3.0

    # For wards missing RS: use transit-only score (C3),
    # scaled to same range, and flag it
    transit_only_mask = ~has_rs & has_transit
    if transit_only_mask.any():
        df.loc[transit_only_mask, "urban_stress_index"] = c3[transit_only_mask]
        log.warning(
            f"  {transit_only_mask.sum()} wards used transit-only stress "
            f"(no NDVI/NDBI). Re-run after rs_sentinel.py completes."
        )

    # Wards with no data at all get NULL
    df.loc[~has_rs & ~has_transit, "urban_stress_index"] = None

    df["urban_stress_index"] = df["urban_stress_index"].round(6)

    high_stress = (df["urban_stress_index"] > 0.7).sum()
    log.info(f"  ✓ urban_stress_index computed")
    log.info(f"  Wards with index > 0.7 (critical): {high_stress}")
    return df


# ---------------------------------------------------------------------------
# Step 3 — Compute revised_accessibility_score
# ---------------------------------------------------------------------------

def compute_revised_accessibility(df: pd.DataFrame) -> pd.DataFrame:
    """
    Revised accessibility score = transit_score penalised by urban stress
    and boosted by population density.

        revised = transit_score × (1 − 0.3 × urban_stress_index)

    This is the final ranking metric for find_underserved_areas().
    High transit score in a high-stress ward still ranks lower than
    high transit in a low-stress ward — reflecting real equity burden.
    """
    log.info("\n── Step 3: Computing revised_accessibility_score ──")

    df = df.copy()
    has_both = df["transit_score"].notna() & df["urban_stress_index"].notna()

    df["revised_accessibility_score"] = None
    df.loc[has_both, "revised_accessibility_score"] = (
        df.loc[has_both, "transit_score"]
        * (1 - 0.3 * df.loc[has_both, "urban_stress_index"])
    ).round(6)

    log.info(f"  ✓ revised_accessibility_score computed for {has_both.sum()} wards")
    return df


# ---------------------------------------------------------------------------
# Step 4 — Write to PostGIS
# ---------------------------------------------------------------------------

def write_to_db(engine, df: pd.DataFrame) -> None:
    """
    UPDATE derived.ward_metrics with the two new computed columns.
    Uses a temp staging table + single UPDATE for efficiency.
    """
    log.info("\n── Step 4: Writing to derived.ward_metrics ──")

    cols = ["ward_id", "urban_stress_index", "revised_accessibility_score"]
    stage = df[cols].copy()

    # Write staging table
    stage.to_sql(
        "temp_rs_merge", engine,
        schema="public",
        if_exists="replace",
        index=False,
    )

    with engine.connect() as conn:
        # Add revised_accessibility_score column if it doesn't exist yet
        conn.execute(text("""
            ALTER TABLE derived.ward_metrics
            ADD COLUMN IF NOT EXISTS revised_accessibility_score NUMERIC
        """))

        result = conn.execute(text("""
            UPDATE derived.ward_metrics w
            SET
                urban_stress_index          = t.urban_stress_index,
                revised_accessibility_score = t.revised_accessibility_score
            FROM public.temp_rs_merge t
            WHERE w.ward_id = t.ward_id
        """))
        updated = result.rowcount

        conn.execute(text("DROP TABLE IF EXISTS public.temp_rs_merge"))
        conn.commit()

    log.info(f"  ✓ Updated {updated} wards")
    if updated < len(df) * 0.9:
        log.warning(
            f"  ⚠ Only {updated}/{len(df)} wards updated — "
            f"check ward_id alignment between shapefile and ward_metrics"
        )


# ---------------------------------------------------------------------------
# Step 5 — Verify + summary
# ---------------------------------------------------------------------------

def verify(engine) -> None:
    log.info("\n── Verification ──")

    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT
                COUNT(*)                                        AS total,
                COUNT(urban_stress_index)                       AS has_stress,
                COUNT(revised_accessibility_score)              AS has_revised,
                ROUND(AVG(urban_stress_index)::numeric, 4)      AS avg_stress,
                ROUND(MIN(urban_stress_index)::numeric, 4)      AS min_stress,
                ROUND(MAX(urban_stress_index)::numeric, 4)      AS max_stress,
                COUNT(CASE WHEN urban_stress_index > 0.7
                      THEN 1 END)                               AS critical_wards
            FROM derived.ward_metrics
        """)).fetchone()

        log.info(f"  Total wards              : {r[0]}")
        log.info(f"  urban_stress_index       : {r[1]} populated")
        log.info(f"  revised_accessibility    : {r[2]} populated")
        log.info(f"  Stress range             : {r[4]} – {r[5]} (avg {r[3]})")
        log.info(f"  Critical wards (> 0.7)   : {r[6]}")

        log.info("\n  10 highest urban stress wards:")
        rows = conn.execute(text("""
            SELECT ward_name,
                   ROUND(urban_stress_index::numeric, 4)         AS stress,
                   ROUND(transit_score::numeric, 4)              AS transit,
                   ROUND(ndvi_mean::numeric, 4)                  AS ndvi,
                   ROUND(ndbi_mean::numeric, 4)                  AS ndbi,
                   bus_stop_count
            FROM derived.ward_metrics
            WHERE urban_stress_index IS NOT NULL
            ORDER BY urban_stress_index DESC
            LIMIT 10
        """)).fetchall()

        log.info(f"  {'Ward':<30} {'Stress':>7} {'Transit':>8} "
                 f"{'NDVI':>7} {'NDBI':>7} {'Stops':>6}")
        log.info(f"  {'-'*70}")
        for r in rows:
            log.info(f"  {str(r[0]):<30} {str(r[1]):>7} {str(r[2]):>8} "
                     f"{str(r[3]):>7} {str(r[4]):>7} {str(r[5]):>6}")

        log.info("\n  10 lowest urban stress wards (best served):")
        rows2 = conn.execute(text("""
            SELECT ward_name,
                   ROUND(urban_stress_index::numeric, 4)         AS stress,
                   ROUND(transit_score::numeric, 4)              AS transit,
                   bus_stop_count
            FROM derived.ward_metrics
            WHERE urban_stress_index IS NOT NULL
            ORDER BY urban_stress_index ASC
            LIMIT 10
        """)).fetchall()

        log.info(f"  {'Ward':<30} {'Stress':>7} {'Transit':>8} {'Stops':>6}")
        log.info(f"  {'-'*55}")
        for r in rows2:
            log.info(f"  {str(r[0]):<30} {str(r[1]):>7} {str(r[2]):>8} {str(r[3]):>6}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RS merge — compute urban stress index")
    parser.add_argument(
        "--force", action="store_true",
        help="Recompute even if urban_stress_index is already populated",
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Propus — RS Merge Pipeline")
    log.info("=" * 60)

    engine = get_engine()
    log.info("Database connection established.")

    # Check if already populated
    if not args.force:
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(urban_stress_index) FROM derived.ward_metrics"
            )).scalar()
            if n and n > 0:
                log.info(
                    f"  urban_stress_index already populated for {n} wards. "
                    f"Use --force to recompute."
                )
                verify(engine)
                return

    # ── Step 1: Load ─────────────────────────────────────────────────────────
    log.info("\n── Step 1: Load ward metrics ──")
    df = load_ward_metrics(engine)
    verify_inputs(df)

    # ── Step 2: Urban stress index ────────────────────────────────────────────
    df = compute_urban_stress_index(df)

    # ── Step 3: Revised accessibility ─────────────────────────────────────────
    df = compute_revised_accessibility(df)

    # ── Step 4: Write ─────────────────────────────────────────────────────────
    write_to_db(engine, df)

    # ── Step 5: Verify ────────────────────────────────────────────────────────
    verify(engine)

    log.info("\n" + "=" * 60)
    log.info("✓ rs_merge.py complete.")
    log.info("  urban_stress_index and revised_accessibility_score")
    log.info("  are now populated in derived.ward_metrics.")
    log.info("  The Streamlit map will show the choropleth on next load.")
    log.info("=" * 60)


if __name__ == "__main__":
    main() 