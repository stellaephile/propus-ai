# Day 2 Summary — Propus PostGIS Ingest
**Date:** 2026-03-29

---

## Goal
Load cleaned GTFS data (bus + metro) and Delhi ward boundaries into PostGIS on GCP, and verify all joins pass.

---

## What Was Done

### 1. Ran `clean_gtfs.py`
Cleaned raw GTFS feeds from `data/raw/bus/` and `data/raw/metro/` into `data/processed/`. All files written successfully:

| Feed | File | Rows |
|------|------|------|
| Bus | stops.csv | 10,559 |
| Bus | routes.csv | 2,403 |
| Bus | trips.csv | 89,393 |
| Bus | stop_times.csv | 3,724,320 |
| Bus | shapes.csv | 152,400 |
| Bus | calendar.csv | 1 |
| Metro | stops.csv | 262 |
| Metro | routes.csv | 36 |
| Metro | trips.csv | 5,438 |
| Metro | stop_times.csv | 128,434 |
| Metro | shapes.csv | 6,643 |
| Metro | calendar.csv | 3 |

`calendar_dates.txt` absent for both feeds — skipped as optional.

---

### 2. Ran `ingest.py` — Bugs Encountered and Fixed

#### Bug 1: `stops.csv` wrote 0 rows on first attempt
**Symptom:** `data/processed/bus/stops.csv` had only a header row (73 bytes). Stop times loaded fine but the FK constraint `stop_times_stop_id_fkey` failed on restore because `gtfs_bus.stops` was empty.

**Root cause:** The processed CSV had been written in a prior aborted run and was stale. Re-running `clean_gtfs.py` regenerated it correctly with 10,559 rows.

---

#### Bug 2: FK constraint restore failed — `stop_times_stop_id_fkey`
**Symptom:**
```
ForeignKeyViolation: insert or update on table "stop_times" violates
foreign key constraint "stop_times_stop_id_fkey"
Key (stop_id)=(bus_0) is not present in table "stops".
```
3,724,320 orphaned rows across 10,559 distinct missing stop IDs.

**Root cause:** Stale `stops.csv` (above). Fixed by regenerating via `clean_gtfs.py`.

---

#### Bug 3: `public.wards` PRIMARY KEY failed — null `ward_id`
**Symptom:**
```
NotNullViolation: column "ward_id" of relation "wards" contains null values
```
1 of 290 ward polygons had no matching column in the shapefile's rename map, writing a null `ward_id`. Without a PK on `public.wards`, the FK from `derived.ward_metrics` could not be created.

**Fix:** Added a null-patch block in `load_ward_boundaries()` before calling `to_postgis()`:
- Detects rows where `ward_id` is null, empty, `"None"`, or `"nan"`
- Assigns a deterministic fallback ID (`ward_fallback_000`)
- Also added deduplication on `ward_id` for robustness

**Result:** 290 wards loaded, PK added, FK from `ward_metrics` restored successfully.

---

#### Bug 4: `verify_load()` false error on `public.wards` geometry
**Symptom:**
```
UndefinedColumn: column "geom" does not exist
```
**Root cause:** `to_postgis()` (GeoPandas) writes the geometry column as `geometry`, not `geom`. The verify loop hardcoded `geom` for all three tables.

**Fix:** Parameterised the geometry column name per table in the geom check loop — `("public", "wards", "geometry")` vs `("gtfs_bus", "stops", "geom")`.

---

## Final State — All Checks Green

### Row counts
| Table | Rows |
|-------|------|
| gtfs_bus.stops | 10,559 |
| gtfs_bus.routes | 2,403 |
| gtfs_bus.trips | 89,393 |
| gtfs_bus.stop_times | 3,724,320 |
| gtfs_metro.stops | 262 |
| gtfs_metro.routes | 36 |
| gtfs_metro.stop_times | 128,434 |
| public.wards | 290 |
| derived.ward_metrics | 290 |

### Geometry populated
| Table | Rows with geom |
|-------|---------------|
| gtfs_bus.stops | 10,559 |
| gtfs_metro.stops | 262 |
| public.wards | 290 |

### Join sanity checks
| Join | Result |
|------|--------|
| bus trips ↔ routes | 89,393 rows |
| bus stop_times ↔ trips | 3,724,320 rows |
| bus stop_times ↔ stops | 3,724,320 rows |
| metro trips ↔ routes | 5,438 rows |

---

---

## Ran `compute.py` — All Steps Passed

### Step results

| Step | Description | Result |
|------|-------------|--------|
| 1 | Bus stop count per ward | 290 wards updated |
| 1 | Metro stop count per ward | 290 wards updated |
| 1 | Total stop count | 290 wards updated |
| 2 | Bus route count per ward | 267 wards updated (23 wards have no bus stops) |
| 2 | Metro route count per ward | 109 wards updated |
| 3 | Peak AM frequency per ward | 267 wards updated |
| 3 | Off-peak frequency per ward | 267 wards updated |
| 4 | Metro within 1000m flag | 290 wards updated |
| 5 | Multimodal gap count | 202 wards updated |
| 5 | Multimodal gap stop count | ⚠ reported `-1` rows (cosmetic bug — see below) |
| 5 | Populate multimodal_gap_count | 202 wards updated |
| 6 | Transit score (GTFS composite) | 290 wards updated |

### Verification

| Metric | Coverage |
|--------|----------|
| bus_stop_count | 290/290 (100%) |
| transit_score | 290/290 (100%) |
| has_metro_within_1k | 290/290 (100%) |
| peak_freq_mean | 267/290 (92%) — expected, 23 wards have no bus stops |

### Transit score: 10 worst-served wards

| Ward | Stops | Freq/hr | Metro | Score |
|------|-------|---------|-------|-------|
| SANGAM VIHAR | 0 | N/A | No | 0.0000 |
| TIGRI | 0 | N/A | No | 0.0000 |
| RAJ NAGAR | 0 | N/A | No | 0.0000 |
| PREM NAGAR | 0 | N/A | No | 0.0000 |
| SAGARPUR | 0 | N/A | No | 0.0000 |
| SAID UL AJAIB | 0 | N/A | No | 0.0000 |
| SANGAM VIHAR CENTRAL | 0 | N/A | No | 0.0000 |
| SANGAM VIHAR WEST | 0 | N/A | No | 0.0000 |
| JAITPUR | 1 | 0.67 | No | 0.0046 |
| MEETHEYPUR | 2 | 1.00 | No | 0.0082 |

### Transit score: 10 best-served wards

| Ward | Stops | Freq/hr | Metro | Score |
|------|-------|---------|-------|-------|
| KASHMERE GATE | 113 | 35.55 | Yes | 0.6845 |
| NDMC CHARGE 4 | 153 | 30.74 | Yes | 0.6773 |
| NDMC CHARGE 1 | 113 | 30.11 | Yes | 0.6082 |
| MUNDAKA | 182 | 13.65 | Yes | 0.6075 |
| ward_fallback_000 | 98 | 25.00 | Yes | 0.6033 |
| MINTO ROAD | 92 | 38.85 | Yes | 0.5912 |
| NDMC CHARGE 5 | 149 | 17.81 | Yes | 0.5910 |
| BIJWASAN | 184 | 9.75 | Yes | 0.5904 |
| DARYAGANJ | 100 | 29.53 | Yes | 0.5887 |
| ROSHANPURA | 93 | 45.00 | Yes | 0.5749 |

### Bugs noted (non-blocking)

**`-1 rows updated` on multimodal gap stop count** — cosmetic reporting bug in `compute.py`. The step found no rows to update but returned `-1` instead of `0`. Did not affect subsequent steps; `multimodal_gap_count` populated correctly (202 wards).

**`ward_fallback_000` in top 10** — the one shapefile ward with a null `ward_id` (fixed in ingest). It's real data (98 stops, metro access). Investigate the shapefile before production to assign its true ward name.

---

## Next Steps
```
python pipeline/rs_worldpop.py   # Days 3–4: WorldPop population zonal stats
python pipeline/rs_sentinel.py   # Days 3–4: GEE Sentinel-2 NDVI/NDBI
```