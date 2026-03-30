import ee
import os
import json
import time
import geopandas as gpd
import pandas as pd
from exactextract import exact_extract
from google.cloud import storage
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from pathlib import Path
from tqdm import tqdm

# ── load .env from project root, regardless of working directory ──────────
load_dotenv(Path(__file__).parent.parent / ".env")

# -----------------------------
# 1. INIT GEE
# -----------------------------
def init_gee():
    service_account = os.getenv("GEE_SERVICE_ACCOUNT")
    private_key = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    print("SERVICE ACCOUNT:", service_account)
    print("CREDENTIALS EXISTS:", private_key is not None)
    credentials = ee.ServiceAccountCredentials(service_account, private_key)
    ee.Initialize(credentials)
    print("✓ GEE initialised")


def get_delhi_roi():
    return ee.Geometry.Rectangle([76.8, 28.4, 77.4, 28.9])


# -----------------------------
# 2. GET SENTINEL DATA
# -----------------------------
def get_sentinel_collection():
    print("Filtering Sentinel-2 collection...")
    col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterDate("2024-02-01", "2024-02-28")
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        .filterBounds(get_delhi_roi())
        .limit(10)
    )
    print("✓ Collection ready")
    return col


def get_latest_gcs_file(prefix):
    client = storage.Client()
    bucket = client.bucket(os.getenv("GCS_BUCKET_RS"))
    blobs = list(bucket.list_blobs(prefix=prefix))
    if not blobs:
        raise Exception(f"No files found for prefix: {prefix}")
    blobs.sort(key=lambda x: x.updated, reverse=True)
    latest = blobs[0].name
    print(f"Using file: {latest}")
    return latest


def gcs_prefix_exists(prefix):
    client = storage.Client()
    bucket = client.bucket(os.getenv("GCS_BUCKET_RS"))
    blobs = list(bucket.list_blobs(prefix=prefix, max_results=1))
    return len(blobs) > 0


# -----------------------------
# 3. COMPUTE NDVI & NDBI
# -----------------------------
def compute_indices(collection):
    roi = get_delhi_roi()

    def add_indices(image):
        ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
        ndbi = image.normalizedDifference(['B11', 'B8']).rename('NDBI')
        return image.addBands([ndvi, ndbi])

    print("Computing NDVI and NDBI indices...")
    with tqdm(total=3, desc="Band math", unit="step") as pbar:
        collection = collection.map(add_indices)
        pbar.update(1)
        composite = collection.median().clip(roi)
        pbar.update(1)
        ndvi = composite.select("NDVI")
        ndbi = composite.select("NDBI")
        pbar.update(1)

    print("✓ Indices computed")
    return ndvi, ndbi


# -----------------------------
# 4. EXPORT TO GCS
# -----------------------------
def export_to_gcs(image, name):
    task = ee.batch.Export.image.toCloudStorage(
        image=image,
        description=name,
        bucket=os.getenv("GCS_BUCKET_RS"),
        fileNamePrefix=f"sentinel/{name}",
        scale=10,
        region=get_delhi_roi(),
        crs="EPSG:4326",
        maxPixels=1e13
    )
    task.start()
    print(f"✓ Export task started: sentinel/{name}")
    return task


# -----------------------------
# 5. WAIT FOR GEE TASK
# -----------------------------
def wait_for_task(task):
    desc = task.status().get("description", "GEE export")
    print(f"Waiting for task: {desc}")

    with tqdm(desc=f"  {desc}", unit="s", dynamic_ncols=True) as pbar:
        elapsed = 0
        while task.active():
            time.sleep(30)
            elapsed += 30
            pbar.update(30)
            pbar.set_postfix({"elapsed": f"{elapsed}s", "state": task.status().get("state", "?")})

    status = task.status()
    print(f"TASK STATUS: {status['state']}")

    if status["state"] != "COMPLETED":
        raise Exception(f"GEE export failed: {status}")

    print(f"✓ Task completed: {desc}")


# -----------------------------
# 6. DOWNLOAD FROM GCS
# -----------------------------
def download_from_gcs(blob_name, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    client = storage.Client()
    bucket = client.bucket(os.getenv("GCS_BUCKET_RS"))
    blob = bucket.blob(blob_name)

    file_size = blob.size or 0
    print(f"Downloading {blob_name} ({file_size / 1024 / 1024:.1f} MB)...")

    with tqdm(
        total=file_size, unit="B", unit_scale=True, unit_divisor=1024,
        desc=f"  {os.path.basename(local_path)}",
    ) as pbar:
        blob.download_to_filename(local_path)
        pbar.update(file_size)

    print(f"✓ Downloaded to {local_path}")


# -----------------------------
# 7. ZONAL STATS
# -----------------------------

# Map shapefile column names → ward_id.
# The ingest pipeline uses clean_gtfs.py's rename logic which looks for
# ward_id / ward_no / id etc. Use the same priority order here so the
# ward_ids written to PostGIS match exactly.
_WARD_ID_CANDIDATES = [
    "ward_id", "Ward_ID", "WARD_ID",
    "ward_no", "Ward_No", "WARD_NO",
    "ward_number", "Ward_Number",
    "id", "ID",
]


def _resolve_ward_id_col(gdf: gpd.GeoDataFrame) -> str:
    """Return the first matching ward_id column name, or raise clearly."""
    for col in _WARD_ID_CANDIDATES:
        if col in gdf.columns:
            return col
    raise KeyError(
        f"Cannot find a ward_id column in shapefile. "
        f"Available columns: {list(gdf.columns)}\n"
        f"Add the correct column name to _WARD_ID_CANDIDATES in rs_sentinel.py."
    )


def compute_zonal_stats(raster_path, wards_path, column_name):
    print(f"Computing zonal stats: {column_name} from {os.path.basename(raster_path)}...")

    with tqdm(total=3, desc=f"  {column_name}", unit="step") as pbar:
        wards = gpd.read_file(wards_path)
        pbar.update(1)
        pbar.set_postfix({"wards": len(wards)})

        # ── resolve ward_id column robustly ──────────────────────────────
        ward_id_col = _resolve_ward_id_col(wards)
        if ward_id_col != "ward_id":
            wards = wards.rename(columns={ward_id_col: "ward_id"})

        # Normalise to string, strip whitespace — must match DB ward_ids
        wards["ward_id"] = wards["ward_id"].astype(str).str.strip()
        pbar.update(1)

        # exact_extract returns a list aligned to wards rows
        result_df = exact_extract(raster_path, wards, ["mean"], output="pandas")
        wards[column_name] = result_df["mean"].values
        pbar.update(1)

    print(f"✓ Zonal stats complete: {column_name} for {len(wards)} wards")
    return wards[["ward_id", column_name]]


# -----------------------------
# 8. UPDATE POSTGIS
# -----------------------------
def update_postgis(df):
    """
    Write ndvi_mean and ndbi_mean into derived.ward_metrics.

    Uses DATABASE_URL from .env — same connection string as ingest.py
    and compute.py. Falls back to building from DB_HOST/DB_USER/DB_PASSWORD/
    DB_NAME if DATABASE_URL is not set.
    """
    print(f"Updating PostGIS with {len(df)} ward RS metrics...")

    # ── connection: use DATABASE_URL (set in .env) ────────────────────────
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        # Fallback: build from individual vars — require DB_HOST explicitly
        host = os.getenv("DB_HOST")
        if not host:
            raise EnvironmentError(
                "Neither DATABASE_URL nor DB_HOST is set in .env. "
                "Add DATABASE_URL=postgresql://user:pass@<cloud-sql-ip>:5432/propus"
            )
        database_url = (
            f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{host}:5432/{os.getenv('DB_NAME', 'propus')}"
        )

    engine = create_engine(database_url, pool_pre_ping=True)

    with tqdm(total=2, desc="  PostGIS update", unit="step") as pbar:

        # Stage into a temp table (public schema, dropped after commit)
        df.to_sql("temp_rs_sentinel", engine, if_exists="replace",
                  index=False, schema="public")
        pbar.update(1)
        pbar.set_postfix({"rows": len(df)})

        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE derived.ward_metrics w
                SET
                    ndvi_mean = t.ndvi_mean,
                    ndbi_mean = t.ndbi_mean
                FROM public.temp_rs_sentinel t
                WHERE w.ward_id = t.ward_id
            """))
            rows_updated = result.rowcount

            # Clean up staging table
            conn.execute(text("DROP TABLE IF EXISTS public.temp_rs_sentinel"))
            conn.commit()
        pbar.update(1)

    print(f"✓ PostGIS updated — {rows_updated} wards written")

    # Warn if fewer rows updated than expected
    if rows_updated < len(df) * 0.9:
        print(
            f"  ⚠ Only {rows_updated}/{len(df)} wards matched. "
            f"Check that ward_id values in the shapefile match public.wards.ward_id.\n"
            f"  Sample shapefile IDs: {df['ward_id'].head(5).tolist()}\n"
            f"  Run: SELECT ward_id FROM public.wards LIMIT 5;"
        )


# -----------------------------
# 9. MAIN PIPELINE
# -----------------------------
def run_pipeline():
    print("\n" + "=" * 55)
    print("  Propus — Sentinel-2 RS Pipeline")
    print("=" * 55 + "\n")

    steps = [
        "Init GEE",
        "Get Sentinel collection",
        "Compute NDVI + NDBI",
        "Export NDVI to GCS",
        "Export NDBI to GCS",
        "Download rasters",
        "Zonal stats",
        "Update PostGIS",
    ]

    with tqdm(total=len(steps), desc="Overall pipeline", unit="step",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]") as master:

        master.set_postfix({"step": "Init GEE"})
        init_gee()
        master.update(1)

        master.set_postfix({"step": "Sentinel collection"})
        collection = get_sentinel_collection()
        master.update(1)

        master.set_postfix({"step": "Compute indices"})
        ndvi, ndbi = compute_indices(collection)
        master.update(1)

        master.set_postfix({"step": "Export NDVI"})
        if gcs_prefix_exists("sentinel/ndvi"):
            print("✅ NDVI already exists in GCS → skipping export")
        else:
            ndvi_task = export_to_gcs(ndvi, "ndvi")
            wait_for_task(ndvi_task)
        master.update(1)

        master.set_postfix({"step": "Export NDBI"})
        if gcs_prefix_exists("sentinel/ndbi"):
            print("✅ NDBI already exists in GCS → skipping export")
        else:
            ndbi_task = export_to_gcs(ndbi, "ndbi")
            wait_for_task(ndbi_task)
        master.update(1)

        print("Waiting 20s for GCS sync...")
        for _ in tqdm(range(20), desc="  GCS sync", unit="s", leave=False):
            time.sleep(1)

        master.set_postfix({"step": "Download rasters"})
        ndvi_blob = get_latest_gcs_file("sentinel/ndvi")
        ndbi_blob = get_latest_gcs_file("sentinel/ndbi")
        download_from_gcs(ndvi_blob, "/tmp/ndvi.tif")
        download_from_gcs(ndbi_blob, "/tmp/ndbi.tif")
        master.update(1)

        master.set_postfix({"step": "Zonal stats"})
        ndvi_df = compute_zonal_stats("/tmp/ndvi.tif", "data/raw/delhi_wards.shp", "ndvi_mean")
        ndbi_df = compute_zonal_stats("/tmp/ndbi.tif", "data/raw/delhi_wards.shp", "ndbi_mean")
        df = ndvi_df.merge(ndbi_df, on="ward_id")
        master.update(1)

        master.set_postfix({"step": "Update PostGIS"})
        update_postgis(df)
        master.update(1)

    print("\n" + "=" * 55)
    print("✓ RS pipeline complete.")
    print("  ndvi_mean and ndbi_mean now in derived.ward_metrics")
    print("=" * 55 + "\n")


# -----------------------------
# 10. ENTRY POINT
# -----------------------------
if __name__ == "__main__":
    run_pipeline()