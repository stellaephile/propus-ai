"""
diagnose_shape_match.py
-----------------------
Prints samples from both routes.txt and shapes.txt so you can
see exactly why the matching is failing.

Usage:
    python diagnose_shape_match.py
"""

import pandas as pd

ROUTES_IN = "data/raw/bus/routes.txt"
SHAPES_IN = "data/raw/bus/shapes.txt"

routes = pd.read_csv(ROUTES_IN, dtype=str)
shapes = pd.read_csv(SHAPES_IN, dtype=str)

# ── What's in routes.txt ──────────────────────────────────────────────────────
print("=" * 55)
print("routes.txt columns:", list(routes.columns))
print(f"\nFirst 20 rows of route_id + route_short_name + route_long_name:")
cols = [c for c in ["route_id","route_short_name","route_long_name"] if c in routes.columns]
print(routes[cols].head(20).to_string(index=False))

# ── What's in shapes.txt ──────────────────────────────────────────────────────
print("\n" + "=" * 55)
unique_shapes = sorted(shapes["shape_id"].unique())
print(f"shapes.txt — {len(unique_shapes)} unique shape_ids")
print("First 30:")
for s in unique_shapes[:30]:
    print(f"  {s}")

# ── Side by side comparison ───────────────────────────────────────────────────
print("\n" + "=" * 55)
print("Side by side — route_short_name vs shape_id (first 20):")
print(f"  {'route_short_name':<25} {'would need shape_id':<30} {'exists?'}")
print("  " + "-" * 65)

shape_set = set(unique_shapes)
for _, r in routes.head(20).iterrows():
    rsn = str(r.get("route_short_name","")).strip()
    candidate = f"shape_{rsn}"
    exists = "✓" if candidate in shape_set else "✗"
    print(f"  {rsn:<25} {candidate:<30} {exists}")
