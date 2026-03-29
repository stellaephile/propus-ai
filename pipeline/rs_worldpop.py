"""
rs_worldpop.py
--------------
WorldPop India 2020 population raster pipeline.

Steps:
    1. Download WorldPop India 2020 constrained 100m GeoTIFF (~800 MB)
       directly from worldpop.org FTP — no API key required.
    2. Verify and reproject to EPSG:4326 (WorldPop is typically already
       WGS84 — this step verifies and warps only if needed).
    3. Compute zonal sum of population pixels per Delhi ward polygon using
       exactextract (preferred) with rasterstats as automatic fallback.
    4. Derive stops_per_10k = (bus_stop_count / pop_total) * 10000 —
       the WRI standard equity metric.
    5. Write pop_total and stops_per_10k into derived.ward_metrics.

Usage:
    python pipeline/rs_worldpop.py

    # Skip download if GeoTIFF already present locally or in GCS:
    python pipeline/rs_worldpop.py --skip-download

    # Use a GeoTIFF already downloaded to a custom path:
    python pipeline/rs_worldpop.py --tif path/to/ind_ppp_2020_1km_Aggregated.tif

Requires (pip install):
    rasterio exactextract rasterstats geopandas sqlalchemy psycopg2-binary
    python-dotenv requests tqdm google-cloud-storage (optional, for GCS cache)

Environment variables (.env):
    DATABASE_URL          postgresql://user:pass@host:5432/propus
    GCS_BUCKET_RAW        (optional) Cloud Storage bucket for caching the tif
    GCP_PROJECT_ID        (optional) required only if GCS_BUCKET_RAW is set
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# WorldPop India 2020 constrained individual country dataset (100m)
# See: https://hub.worldpop.org/geodata/summary?id=49705
WORLDPOP_URL = (
    "https://data.worldpop.org/GIS/Population/Global_2000_2020_Constrained/"
    "2020/BSGM/IND/ind_ppp_2020_constrained.tif"
)

# Fallback URL (unconstrained 100m — slightly different population model)
WORLDPOP_URL_FALLBACK = (
    "https://data.worldpop.org/GIS/Population/Global_2000_2020/"
    "100m/2020/IND/ind_ppp_2020.tif"
)

LOCAL_TIF_DIR = Path("data/worldpop")
LOCAL_TIF_PATH = LOCAL_TIF_DIR / "ind_ppp_2020_constrained.tif"
REPROJECTED_TIF_PATH = LOCAL_TIF_DIR / "ind_ppp_2020_epsg4326.tif"

DATABASE_URL = os.environ["DATABASE_URL"]

# Delhi bounding box — clip raster to this extent before zonal stats
# to avoid loading the full ~800 MB India raster into memory.
DELHI_BBOX = {
    "west":  76.5,
    "south": 28.2,
    "east":  77.8,
    "north": 29.2,
}


# ---------------------------------------------------------------------------
# Step 1 — Download
# ---------------------------------------------------------------------------

def download_worldpop(dest: Path, skip_if_exists: bool = True) -> Path:
    """
    Download WorldPop India 2020 constrained 100m GeoTIFF.

    Falls back to the unconstrained dataset if the constrained URL fails.
    Streams with progress display so the ~800 MB download is visible.
    """
    if skip_if_exists and dest.exists():
        log.info(f"  WorldPop GeoTIFF already present at {dest} — skipping download")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Try GCS cache first (avoids re-downloading across pipeline runs)
    gcs_bucket = os.environ.get("GCS_BUCKET_RAW")
    if gcs_bucket:
        gcs_path = _try_gcs_download(gcs_bucket, "worldpop/ind_ppp_2020_constrained.tif", dest)
        if gcs_path:
            return gcs_path

    for url in [WORLDPOP_URL, WORLDPOP_URL_FALLBACK]:
        log.info(f"  Downloading WorldPop from: {url}")
        try:
            _stream_download(url, dest)
            log.info(f"  ✓ Downloaded to {dest}")
            # Upload to GCS for future runs
            if gcs_bucket:
                _upload_to_gcs(gcs_bucket, "worldpop/ind_ppp_2020_constrained.tif", dest)
            return dest
        except Exception as exc:
            log.warning(f"  Download failed ({exc}), trying fallback URL...")

    raise RuntimeError("Both WorldPop download URLs failed. Check connectivity or supply --tif.")


def _stream_download(url: str, dest: Path) -> None:
    """Stream-download url → dest with a tqdm progress bar."""
    import requests
    try:
        from tqdm import tqdm
        has_tqdm = True
    except ImportError:
        has_tqdm = False

    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        chunk = 1024 * 1024  # 1 MB

        if has_tqdm:
            progress = tqdm(total=total, unit="B", unit_scale=True, desc=dest.name)
        else:
            progress = None
            log.info(f"  (tqdm not installed — no progress bar; total ~{total // 1_000_000} MB)")

        with open(dest, "wb") as f:
            for data in r.iter_content(chunk_size=chunk):
                f.write(data)
                if progress:
                    progress.update(len(data))
        if progress:
            progress.close()


def _try_gcs_download(bucket_name: str, blob_name: str, dest: Path) -> Path | None:
    """Try to download from GCS; return None if blob doesn't exist."""
    try:
        from google.cloud import storage
        client = storage.Client(project=os.environ.get("GCP_PROJECT_ID"))
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if blob.exists():
            log.info(f"  Found in GCS gs://{bucket_name}/{blob_name} — downloading...")
            blob.download_to_filename(str(dest))
            log.info(f"  ✓ Downloaded from GCS to {dest}")
            return dest
        log.info(f"  Not in GCS gs://{bucket_name}/{blob_name} — will download from WorldPop")
    except Exception as exc:
        log.warning(f"  GCS check failed ({exc}) — skipping GCS cache")
    return None


def _upload_to_gcs(bucket_name: str, blob_name: str, src: Path) -> None:
    """Upload src file to GCS for caching."""
    try:
        from google.cloud import storage
        client = storage.Client(project=os.environ.get("GCP_PROJECT_ID"))
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        log.info(f"  Uploading {src.name} to gs://{bucket_name}/{blob_name}...")
        blob.upload_from_filename(str(src))
        log.info("  ✓ Uploaded to GCS")
    except Exception as exc:
        log.warning(f"  GCS upload failed ({exc}) — continuing without cache")


# ---------------------------------------------------------------------------
# Step 2 — Reproject / clip
# ---------------------------------------------------------------------------

def prepare_raster(src: Path, dst: Path) -> Path:
    """
    Verify CRS is EPSG:4326 and clip to Delhi bounding box.

    Clipping before zonal stats reduces memory usage from ~800 MB
    (full India) to ~15 MB (Delhi extent only).

    Returns path to the clipped (and if needed reprojected) GeoTIFF.
    """
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.mask import mask as rasterio_mask
    from shapely.geometry import box
    import json

    target_crs = CRS.from_epsg(4326)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists():
        log.info(f"  Clipped raster already present at {dst} — skipping prepare step")
        return dst

    log.info(f"  Opening {src}...")
    with rasterio.open(src) as rds:
        src_crs = rds.crs
        log.info(f"  Source CRS: {src_crs}")

        if src_crs == target_crs:
            log.info("  CRS is already EPSG:4326 — clipping only")
            # Clip to Delhi bbox
            delhi_geom = box(
                DELHI_BBOX["west"], DELHI_BBOX["south"],
                DELHI_BBOX["east"], DELHI_BBOX["north"],
            )
            clipped, clipped_transform = rasterio_mask(
                rds, [delhi_geom.__geo_interface__], crop=True, nodata=rds.nodata
            )
            out_meta = rds.meta.copy()
            out_meta.update({
                "driver": "GTiff",
                "height": clipped.shape[1],
                "width":  clipped.shape[2],
                "transform": clipped_transform,
                "compress": "lzw",
            })
            with rasterio.open(dst, "w", **out_meta) as out:
                out.write(clipped)
        else:
            log.info(f"  Reprojecting from {src_crs} to EPSG:4326 and clipping...")
            transform, width, height = calculate_default_transform(
                src_crs, target_crs, rds.width, rds.height, *rds.bounds
            )
            out_meta = rds.meta.copy()
            out_meta.update({
                "driver": "GTiff",
                "crs": target_crs,
                "transform": transform,
                "width": width,
                "height": height,
                "compress": "lzw",
            })
            # Write reprojected to a temp path, then clip
            tmp = dst.with_suffix(".tmp.tif")
            with rasterio.open(tmp, "w", **out_meta) as dst_ds:
                reproject(
                    source=rasterio.band(rds, 1),
                    destination=rasterio.band(dst_ds, 1),
                    src_transform=rds.transform,
                    src_crs=src_crs,
                    dst_transform=transform,
                    dst_crs=target_crs,
                    resampling=Resampling.bilinear,
                )
            # Clip to Delhi bbox
            with rasterio.open(tmp) as reprojected:
                delhi_geom = box(
                    DELHI_BBOX["west"], DELHI_BBOX["south"],
                    DELHI_BBOX["east"], DELHI_BBOX["north"],
                )
                clipped, clipped_transform = rasterio_mask(
                    reprojected, [delhi_geom.__geo_interface__], crop=True,
                    nodata=reprojected.nodata
                )
                clip_meta = reprojected.meta.copy()
                clip_meta.update({
                    "height": clipped.shape[1],
                    "width":  clipped.shape[2],
                    "transform": clipped_transform,
                })
                with rasterio.open(dst, "w", **clip_meta) as out:
                    out.write(clipped)
            tmp.unlink(missing_ok=True)

    log.info(f"  ✓ Raster prepared → {dst}")
    return dst


# ---------------------------------------------------------------------------
# Step 3 — Zonal statistics
# ---------------------------------------------------------------------------

def zonal_population(tif: Path, ward_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Compute total WorldPop population per ward polygon.

    Uses exactextract (preferred — fractional pixel coverage, fast on large
    rasters). Falls back to rasterstats automatically if exactextract is not
    installed.

    Returns a DataFrame with columns: ward_id, pop_total.
    """
    log.info("  Computing zonal population statistics...")

    # Try exactextract first
    try:
        return _zonal_exactextract(tif, ward_gdf)
    except ImportError:
        log.warning("  exactextract not installed — falling back to rasterstats")
    except Exception as exc:
        log.warning(f"  exactextract failed ({exc}) — falling back to rasterstats")

    return _zonal_rasterstats(tif, ward_gdf)


def _zonal_exactextract(tif: Path, ward_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """Zonal sum using exactextract (preferred)."""
    from exactextract import exact_extract

    log.info("  Using exactextract for zonal stats...")
    result = exact_extract(
        str(tif),
        ward_gdf,
        ["sum"],
        include_cols=["ward_id"],
        output="pandas",
    )
    # exactextract returns a DataFrame; column naming depends on version
    # Normalise to pop_total
    sum_col = [c for c in result.columns if "sum" in c.lower()][0]
    result = result.rename(columns={sum_col: "pop_total"})
    result["pop_total"] = result["pop_total"].clip(lower=0).fillna(0).round().astype(int)
    log.info(f"  ✓ exactextract: {len(result)} wards, "
             f"total pop = {result['pop_total'].sum():,.0f}")
    return result[["ward_id", "pop_total"]]


def _zonal_rasterstats(tif: Path, ward_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """Zonal sum using rasterstats (fallback)."""
    from rasterstats import zonal_stats

    log.info("  Using rasterstats for zonal stats...")
    stats = zonal_stats(
        ward_gdf,
        str(tif),
        stats=["sum"],
        nodata=-9999,
        all_touched=False,
    )
    pop_totals = [s.get("sum") or 0 for s in stats]
    result = ward_gdf[["ward_id"]].copy()
    result["pop_total"] = [max(0, round(v)) for v in pop_totals]
    log.info(f"  ✓ rasterstats: {len(result)} wards, "
             f"total pop = {result['pop_total'].sum():,.0f}")
    return result[["ward_id", "pop_total"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Step 4 — Derive stops_per_10k
# ---------------------------------------------------------------------------

def derive_stops_per_10k(
    pop_df: pd.DataFrame,
    ward_metrics_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge population totals with existing stop counts and compute:
        stops_per_10k = (bus_stop_count / pop_total) * 10,000

    Handles zero-population wards gracefully (returns NULL / NaN).
    """
    merged = pop_df.merge(
        ward_metrics_df[["ward_id", "bus_stop_count"]],
        on="ward_id",
        how="left",
    )
    merged["stops_per_10k"] = merged.apply(
        lambda r: round((r["bus_stop_count"] / r["pop_total"]) * 10_000, 4)
        if r["pop_total"] > 0 else None,
        axis=1,
    )
    zero_pop = (merged["pop_total"] == 0).sum()
    if zero_pop:
        log.warning(f"  {zero_pop} wards have pop_total = 0 — stops_per_10k will be NULL")
    log.info(f"  ✓ stops_per_10k derived for {len(merged)} wards")
    return merged


# ---------------------------------------------------------------------------
# Step 5 — Write to PostGIS
# ---------------------------------------------------------------------------

def load_ward_metrics(engine) -> pd.DataFrame:
    """Read current ward_metrics from DB."""
    return pd.read_sql(
        "SELECT ward_id, bus_stop_count FROM derived.ward_metrics",
        engine,
    )


def write_to_db(engine, df: pd.DataFrame) -> None:
    """
    Upsert pop_total and stops_per_10k into derived.ward_metrics.

    Uses a per-row UPDATE — safe for small ward counts (~290 rows).
    Relies on ward_id being the primary key of ward_metrics.
    """
    log.info("  Writing population metrics to derived.ward_metrics...")
    updated = 0
    skipped = 0

    with engine.connect() as conn:
        for _, row in df.iterrows():
            result = conn.execute(
                text("""
                    UPDATE derived.ward_metrics
                    SET
                        pop_total      = :pop_total,
                        stops_per_10k  = :stops_per_10k
                    WHERE ward_id = :ward_id
                """),
                {
                    "ward_id":      row["ward_id"],
                    "pop_total":    int(row["pop_total"]) if pd.notna(row["pop_total"]) else None,
                    "stops_per_10k": float(row["stops_per_10k"]) if pd.notna(row.get("stops_per_10k")) else None,
                },
            )
            if result.rowcount > 0:
                updated += 1
            else:
                skipped += 1
        conn.commit()

    log.info(f"  ✓ Updated {updated} wards, {skipped} ward_ids not found in ward_metrics")


def verify(engine) -> None:
    """Quick verification of written metrics."""
    log.info("\n── Verification ──")
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT
                COUNT(*)                                    AS total_wards,
                COUNT(pop_total)                            AS has_pop,
                COUNT(stops_per_10k)                        AS has_ratio,
                ROUND(SUM(pop_total)::numeric / 1e6, 2)     AS total_pop_millions,
                ROUND(AVG(stops_per_10k)::numeric, 2)       AS avg_stops_per_10k,
                ROUND(MIN(stops_per_10k)::numeric, 4)       AS min_stops_per_10k,
                ROUND(MAX(stops_per_10k)::numeric, 2)       AS max_stops_per_10k
            FROM derived.ward_metrics
        """)).fetchone()

    log.info(f"  Total wards          : {r[0]}")
    log.info(f"  Wards with pop_total : {r[1]}")
    log.info(f"  Wards with ratio     : {r[2]}")
    log.info(f"  Total population     : {r[3]}M")
    log.info(f"  Avg stops / 10k pop  : {r[4]}")
    log.info(f"  Min stops / 10k pop  : {r[5]}")
    log.info(f"  Max stops / 10k pop  : {r[6]}")

    # Show worst-served wards by population-weighted metric
    log.info("\n  10 most underserved wards (lowest stops_per_10k, pop > 0):")
    rows = conn.execute(text("""
        SELECT ward_name, pop_total, bus_stop_count, stops_per_10k
        FROM derived.ward_metrics
        WHERE pop_total > 0 AND stops_per_10k IS NOT NULL
        ORDER BY stops_per_10k ASC
        LIMIT 10
    """)).fetchall() if False else []  # conn already closed — re-open

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ward_name, pop_total, bus_stop_count, stops_per_10k
            FROM derived.ward_metrics
            WHERE pop_total > 0 AND stops_per_10k IS NOT NULL
            ORDER BY stops_per_10k ASC
            LIMIT 10
        """)).fetchall()

    log.info(f"  {'Ward':<35} {'Pop':>10} {'Stops':>6} {'Stops/10k':>10}")
    log.info(f"  {'-'*65}")
    for row in rows:
        log.info(f"  {row[0]:<35} {row[1]:>10,} {row[2]:>6} {row[3]:>10.4f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="WorldPop zonal stats pipeline")
    parser.add_argument(
        "--tif",
        type=Path,
        default=None,
        help="Path to an already-downloaded WorldPop GeoTIFF. Skips download.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help=f"Use {LOCAL_TIF_PATH} if it exists; fail if it does not.",
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Propus — WorldPop Population Raster Pipeline")
    log.info("=" * 60)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    log.info("Database connection established.")

    # ── Step 1: Download ──────────────────────────────────────────────────
    log.info("\n── Step 1: Download WorldPop GeoTIFF ──")
    if args.tif:
        raw_tif = args.tif
        if not raw_tif.exists():
            log.error(f"Supplied --tif path does not exist: {raw_tif}")
            sys.exit(1)
        log.info(f"  Using supplied GeoTIFF: {raw_tif}")
    else:
        raw_tif = download_worldpop(LOCAL_TIF_PATH, skip_if_exists=True)

    # ── Step 2: Reproject / clip ──────────────────────────────────────────
    log.info("\n── Step 2: Prepare raster (verify CRS, clip to Delhi) ──")
    clipped_tif = prepare_raster(raw_tif, REPROJECTED_TIF_PATH)

    # ── Step 3: Load ward polygons from DB ───────────────────────────────
    log.info("\n── Step 3: Load ward polygons ──")
    ward_gdf = gpd.read_postgis(
        "SELECT ward_id, ward_name, geometry FROM public.wards",
        engine,
        geom_col="geometry",
    )
    log.info(f"  Loaded {len(ward_gdf)} ward polygons from public.wards")

    # Ensure EPSG:4326 to match raster
    if ward_gdf.crs is None:
        ward_gdf = ward_gdf.set_crs(epsg=4326)
    elif ward_gdf.crs.to_epsg() != 4326:
        ward_gdf = ward_gdf.to_crs(epsg=4326)

    # ── Step 3: Zonal stats ───────────────────────────────────────────────
    log.info("\n── Step 3: Zonal population statistics ──")
    pop_df = zonal_population(clipped_tif, ward_gdf)

    # ── Step 4: Derive stops_per_10k ─────────────────────────────────────
    log.info("\n── Step 4: Derive stops_per_10k ──")
    ward_metrics_df = load_ward_metrics(engine)
    enriched_df = derive_stops_per_10k(pop_df, ward_metrics_df)

    # ── Step 5: Write to DB ───────────────────────────────────────────────
    log.info("\n── Step 5: Write to derived.ward_metrics ──")
    write_to_db(engine, enriched_df)

    # ── Verify ────────────────────────────────────────────────────────────
    verify(engine)

    log.info("\n" + "=" * 60)
    log.info("✓ rs_worldpop.py complete.")
    log.info("  Next: python pipeline/rs_sentinel.py")
    log.info("=" * 60)


if __name__ == "__main__":
    main()