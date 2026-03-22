"""
convert_to_shapes.py
--------------------
Converts QGIS vertices export (from QuickOSM LineString layer)
to a valid GTFS shapes.txt.

Before running this:
  1. In QGIS: Vector → Geometry Tools → Extract Vertices
     on the route_bus LineString layer (651 features)
  2. Right-click the vertices layer → Export → Save Features As → CSV
     Set Geometry = AS_XY
     Save as: data/processed/qgis_vertices.csv

Then run:
    python convert_to_shapes.py
"""

import pandas as pd
from pathlib import Path

INPUT_CSV   = "data/processed/bus/qgis_vertices.csv"
OUTPUT_PATH = "data/raw/bus/shapes.txt"

# ── Load ─────────────────────────────────────────────────────────────────────
print("Loading vertices export...")
df = pd.read_csv(INPUT_CSV, dtype=str)

print(f"Columns found: {list(df.columns)}")
print(f"Total rows: {len(df)}")

# ── Find the right column names ───────────────────────────────────────────────
# QGIS exports vary — find ref, x (lon), y (lat), vertex_index columns
# by scanning for likely names

def find_col(df, candidates):
    """Return the first column name from candidates that exists in df."""
    for c in candidates:
        if c in df.columns:
            return c
    return None

ref_col   = find_col(df, ["ref", "name", "route_ref", "REF"])
x_col     = find_col(df, ["x", "X", "lon", "longitude"])
y_col     = find_col(df, ["y", "Y", "lat", "latitude"])
seq_col   = find_col(df, ["vertex_index", "vertex_part_index",
                           "vertices_index", "index"])

print(f"\nMapped columns:")
print(f"  ref   → {ref_col}")
print(f"  x/lon → {x_col}")
print(f"  y/lat → {y_col}")
print(f"  seq   → {seq_col}")

missing = [n for n, c in [("ref",ref_col),("x",x_col),
                            ("y",y_col),("seq",seq_col)] if c is None]
if missing:
    print(f"\nCould not find columns for: {missing}")
    print("Paste your full column list above to diagnose.")
    raise SystemExit(1)

# ── Build shapes.txt ──────────────────────────────────────────────────────────
shapes = pd.DataFrame({
    "shape_id":         "shape_" + df[ref_col].str.strip(),
    "shape_pt_lat":     pd.to_numeric(df[y_col], errors="coerce"),
    "shape_pt_lon":     pd.to_numeric(df[x_col], errors="coerce"),
    "shape_pt_sequence": pd.to_numeric(df[seq_col], errors="coerce"),
})

# Drop rows with null ref or coordinates
before = len(shapes)
shapes = shapes.dropna()
if before - len(shapes) > 0:
    print(f"Dropped {before - len(shapes)} rows with null values")

# Drop any rows where ref was 'None' or empty
shapes = shapes[shapes["shape_id"] != "shape_None"]
shapes = shapes[shapes["shape_id"] != "shape_"]

# Reset sequence to start at 1 per shape (QGIS starts at 0)
shapes["shape_pt_sequence"] = (
    shapes.sort_values(["shape_id", "shape_pt_sequence"])
          .groupby("shape_id")
          .cumcount() + 1
)

shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])

# Round coordinates to 6 decimal places
shapes["shape_pt_lat"] = shapes["shape_pt_lat"].round(6)
shapes["shape_pt_lon"] = shapes["shape_pt_lon"].round(6)

# ── Validate Delhi bbox ───────────────────────────────────────────────────────
outside = shapes[
    ~shapes["shape_pt_lat"].between(28.40, 28.88) |
    ~shapes["shape_pt_lon"].between(76.84, 77.35)
]
if len(outside) > 0:
    print(f"Warning: {len(outside)} points outside Delhi bbox — dropping")
    shapes = shapes[
        shapes["shape_pt_lat"].between(28.40, 28.88) &
        shapes["shape_pt_lon"].between(76.84, 77.35)
    ]

# ── Save ──────────────────────────────────────────────────────────────────────
Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
shapes.to_csv(OUTPUT_PATH, index=False)

print(f"\n✓ shapes.txt written to {OUTPUT_PATH}")
print(f"  Unique shapes (routes): {shapes['shape_id'].nunique()}")
print(f"  Total coordinate rows:  {len(shapes)}")
print(f"  Avg points per route:   {len(shapes) / shapes['shape_id'].nunique():.0f}")
print(f"\nSample (first 5 rows):")
print(shapes.head().to_string(index=False))
