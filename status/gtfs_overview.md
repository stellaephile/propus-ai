# Propus — GTFS Data Pipeline
## Days 1–2: Download, Validate, Clean, and Shape Preparation

---

## Overview

Days 1–2 cover getting raw Delhi GTFS data into a clean, validated state ready for PostGIS ingestion. This includes downloading both the bus (DTC) and metro (DMRC) feeds, validating them, cleaning known data quality issues, and building route geometry (shapes.txt) from OpenStreetMap since the Delhi bus feed does not include a shapes.txt file.

---

## Data Sources

| Feed | Source | License | URL |
|---|---|---|---|
| Delhi Bus (DTC) | Open Transit Data, Delhi | OGD India | https://otd.delhi.gov.in/data/static/ |
| Delhi Metro (DMRC) | Open Transit Data, Delhi | OGD India | https://otd.delhi.gov.in/data/staticDMRC/ |

---

## Scripts — Execution Order

```
python download_gtfs.py              # Day 1 — fetch feeds
python validate_gtfs.py              # Day 1 — validate + report
python clean_gtfs.py                 # Day 1 — clean + normalise

# In QGIS: QuickOSM → Extract Vertices → Export CSV
python convert_to_shapes.py          # Day 1–2 — build shapes.txt from OSM
python link_shapes_to_trips.py       # Day 2 — link shapes to trips

python pipeline/ingest.py            # Day 2 — load into PostGIS
python pipeline/fetch_osm_routes_quick.py  # Day 2 — store route geometries
python pipeline/compute.py           # Day 2 — derive ward metrics
python pipeline/verify.py            # Day 2 — verify load
```

---

## Day 1 — Download and Validate

### Step 1: `download_gtfs.py`

Downloads both GTFS feeds and unzips them into `data/raw/bus/` and `data/raw/metro/`.

**Outputs:**
- `data/raw/bus/` — DTC bus GTFS files
- `data/raw/metro/` — DMRC metro GTFS files
- `data/raw/bus.zip`, `data/raw/metro.zip` — kept for audit trail

**Key behaviour:**
- Tries multiple encodings on download failure
- Skips download if zip already exists (safe to re-run)
- Checks for all required GTFS files after extraction and logs missing optional files

---

### Step 2: `validate_gtfs.py`

Runs two layers of validation on both feeds:

**Layer 1 — gtfs-kit built-in validation**
Standard GTFS spec compliance check. Catches missing required fields, invalid foreign key references, malformed dates.

**Layer 2 — Custom checks**
- Stop coordinates within Delhi bounding box (28.40–28.88°N, 76.84–77.35°E)
- `stop_times.txt` time format — flags times > 24:00:00 (overnight services, valid but needs handling in cleaning)
- `shapes.txt` presence and trip coverage percentage
- `calendar.txt` weekday service coverage

**Output:** `data/validation_report.txt`

**What we found on the Delhi bus feed:**

| Check | Result | Action taken |
|---|---|---|
| gtfs-kit validation | No errors | Proceed |
| Stop coordinates bbox | Initially flagged all 10,559 stops | False positive — dtype=str caused float comparison to fail. All coordinates are valid WGS84. |
| `shapes.txt` | Missing entirely | Built from OSM (see below) |
| Stop times > 24:00:00 | Present | Handled in clean_gtfs.py |

> **Note on the bbox false positive:** The validation script read stop_lat as string dtype. Comparison `"28.85" < 28.40` silently failed in pandas. All coordinates are valid — confirmed by manual inspection. Coordinates range from 28.40–28.88°N, 76.84–77.35°E, fully within Delhi.

---

### Step 3: `clean_gtfs.py`

Cleans and normalises both feeds. Outputs to `data/processed/bus/` and `data/processed/metro/`.

**What it cleans:**

| Issue | Fix applied |
|---|---|
| Encoding inconsistencies in stop names | Tries utf-8, utf-8-sig, latin-1, cp1252 in order |
| Unicode control characters in stop names | Strip non-printable chars, NFC normalise |
| Stop coordinates outside Delhi bbox | Drop with warning |
| Duplicate stop_ids | Deduplicate, keep first |
| Stop ID collisions between bus and metro | Prefix all IDs: `bus_<id>` and `metro_<id>` |
| GTFS times > 24:00:00 | Parse to integer seconds — preserves overnight ordering |
| Unparseable departure times | Drop rows, log count |
| Non-monotonic stop_sequence | Flagged in validation report |
| route_type inconsistencies | Defaulted to 3 (bus) for bus feed, 1 (metro) for metro feed |
| calendar date format YYYYMMDD | Converted to ISO 8601 (YYYY-MM-DD) |

**Key design decision — ID prefixing:**
All stop_ids, route_ids, trip_ids, service_ids are prefixed with feed name (`bus_` / `metro_`). This is essential because Delhi bus and metro feeds share numeric ID ranges. Without prefixing, loading both feeds into the same PostGIS database would cause primary key collisions.

---

## Day 1–2 — Route Geometry (shapes.txt)

### The problem

The Delhi DTC bus GTFS feed does not include `shapes.txt`. Without route geometry, the Folium map cannot draw route lines — only stop markers are visible. For a transit intelligence tool this is a significant gap.

### Solution — OpenStreetMap via QuickOSM

OpenStreetMap has Delhi bus routes mapped as `type=route, route=bus` relations. These relations contain ordered way members whose node coordinates form road-snapped route geometry.

**Process:**

**In QGIS:**
1. QuickOSM plugin → query `route=bus` for Delhi bounding box
2. Result: `route_bus` LineString layer (651 features, 411 unique refs) and a MultiLineString layer (discarded — contained highway relations, not bus routes)
3. Vector → Geometry Tools → Extract Vertices on LineString layer
4. Export vertices as CSV with Geometry = AS_XY → `data/processed/qgis_vertices.csv` (154,209 rows)

**In Python (`convert_to_shapes.py`):**
- Maps QGIS export columns to GTFS schema: `ref` → `shape_id`, `x` → `shape_pt_lon`, `y` → `shape_pt_lat`, `vertex_index` → `shape_pt_sequence`
- Handles WKT geometry column when AS_XY export is missing (parses `POINT (lon lat)` strings)
- Resets sequence to start at 1 per shape
- Drops points outside Delhi bbox
- Rounds coordinates to 6 decimal places

**Output:** `data/raw/bus/shapes.txt` — 410 unique shapes, ~154,000 coordinate rows, ~375 points per route average

---

### Step 4: `link_shapes_to_trips.py`

Links shapes.txt back to trips.txt by matching shape_id to route.

**The matching challenge:**

`route_short_name` is entirely empty in the Delhi bus feed. Route identifiers are encoded in `route_long_name` with direction and service-type suffixes appended:

```
828AUP        →  route 828A, direction UP
971DOWN       →  route 971, direction DOWN
824STLDOWN2   →  route 824, STL service, direction DOWN, variant 2
260PMSLSTLUP  →  route 260, PMSL+STL service, direction UP
0114(NS)      →  route 114, night service
0OMS(+)(NS)   →  Outer Mudrika Service (+) direction, night service
```

**Suffix stripping regex (applied twice for stacked suffixes):**
```
PMSLSTLDOWN\d* | PMSLSTLUP\d* | LNKSTLDOWN\d* | LNKSTLUP\d* |
LNKSTLDWN\d*  | LNKSTL\d*    | STLDOWN\d*    | STLUP\d*    |
STLDWN\d*     | STL\d*       | DOWN\d*       | DWN\d*      |
UP\d+         | UP$          | EXT\d*        | (NS)
```

**Normalisation pipeline per route:**
1. Strip `(NS)` night service marker
2. Strip direction/service suffixes (two passes)
3. Strip leading zeros (`0114` → `114`)
4. Strip trailing `+` or `-` (`CBD1+` → `CBD1`)
5. Remove hyphens (`GL-91` → `GL91`, `AIR-05` → `AIR05`)
6. Strip `LNK` suffix (`136LNK` → `136`)
7. Trim trailing letters only — never digits (`828A` → `828`, stop there — never go to `82` or `8`)
8. Short route whitelist guard — prevents `828A` falsely matching `shape_8`

**Known short routes (single/double digit) that are genuine:**
`1, 8, 33, 34, 39, 47, 48, 66, 73, 85, 88, 8A, 94, 99, MS`

**Special name mappings (OSM ref → shape_id fragment):**

| Route code | Shape ID |
|---|---|
| `OMS(+)` | `(+) OMS` |
| `OMS(-)` | `(-) OMS` |
| `TMS(+)` | `(+) TMS` |
| `TMS(-)` | `(-) TMS` |
| `(+)OUTERMUDRIKA` | `(+) OMS` |

---

## Final Data Status — End of Day 2

| File | Rows | Status | Notes |
|---|---|---|---|
| `stops.txt` | 10,559 | ✅ Clean | All coords valid WGS84, within Delhi bbox |
| `routes.txt` | 2,403 | ✅ Clean | route_short_name empty — use route_long_name |
| `trips.csv` | 89,393 | ✅ Clean | 49.6% linked to shapes (1,090 routes covered) |
| `stop_times.txt` | 3,724,320 | ✅ Clean | departure_seconds column added for fast queries |
| `shapes.txt` | 154,209 pts | ✅ Built | 410 shapes from OSM, ~375 pts/route |
| `calendar.txt` | present | ✅ Clean | Weekday/weekend service flags |

**Metro feed:** All files present and clean. Shapes built from stop-sequence linestrings (metro lines are simple enough that OSM shapes are unnecessary).

---

## Shape Coverage Analysis

| Metric | Value |
|---|---|
| Total routes in feed | 2,403 |
| Routes with OSM shape | 1,090 (45.4%) |
| Routes without shape | 1,313 (54.6%) |
| Trips with shape linked | 44,353 (49.6%) |
| Trips without shape | 45,040 (50.4%) |

**Why 54.6% of routes have no shape:**
OpenStreetMap Delhi bus coverage is partial. Major corridors (Ring Road, BRT, Airport Express feeders) are well mapped. Smaller residential and feeder routes are absent. Routes without shapes still have full stop data — they appear as stop markers on the map but without a route line. All spatial queries (`ST_Within`, `ST_DWithin`, frequency analysis) work correctly regardless of shape coverage.

**Composite routes** (e.g. `876+790DOWN`) are merged services that don't correspond to a single OSM route and will never have shapes — these are left unlinked by design.

---

## Known Issues and Future Work

| Issue | Impact | Fix planned |
|---|---|---|
| 54.6% routes missing shapes | Route lines missing on map for those routes | Manual QGIS tracing for top-50 routes by frequency |
| `route_short_name` empty | Matching complexity | Add short name column derived from long name during cleaning |
| No `shapes.txt` for bus in source feed | Ongoing — upstream data gap | Monitor OTD Delhi for future shapes.txt inclusion |
| Night service `(NS)` routes | Stripped during matching — may affect night bus analysis | Add `is_night_service` boolean column to routes table |

---

## Output Files

```
data/
├── raw/
│   ├── bus/                  ← Original downloaded GTFS files
│   │   ├── stops.txt
│   │   ├── routes.txt
│   │   ├── trips.txt
│   │   ├── stop_times.txt
│   │   ├── calendar.txt
│   │   └── shapes.txt        ← Built from OSM (not in original feed)
│   └── metro/                ← Original downloaded GTFS files
│       ├── stops.txt
│       ├── routes.txt
│       ├── trips.txt
│       ├── stop_times.txt
│       └── calendar.txt
├── processed/
│   ├── bus/
│   │   ├── stops.csv          ← Cleaned, ID-prefixed, bbox-validated
│   │   ├── routes.csv         ← Cleaned
│   │   ├── trips.csv          ← Cleaned + shape_id linked
│   │   ├── stop_times.csv     ← Cleaned + departure_seconds added
│   │   ├── shapes.csv         ← Cleaned shapes
│   │   └── calendar.csv       ← Cleaned, dates normalised
│   ├── metro/                 ← Same structure as bus/
│   ├── qgis_vertices.csv      ← Raw QGIS vertex export (intermediate)
│   ├── osm_match_report.csv   ← All routes: matched / unmatched status
│   └── shape_match_report.csv ← Detailed matching log per route
└── validation_report.txt      ← gtfs-kit + custom validation results
```

---

*Propus — Delhi Transit Intelligence Agent · Sonal · WRI India · March 2026*