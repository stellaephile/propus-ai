"""
link_shapes_to_trips.py  (v2)
------------------------------
Links shapes to trips using route_long_name (since route_short_name
is empty in the Delhi bus GTFS feed).

Extracts route number from route_long_name by stripping direction
suffixes: UP, DOWN, STL, DWN, PMSL, EXT, and trailing digits.

Examples:
    828AUP       -> 828A   -> shape_828A  (tries 828A first, then 828)
    971DOWN      -> 971    -> shape_971
    824STLDOWN2  -> 824    -> shape_824
    103B+        -> 103B+  -> shape_103B+
    172AUP       -> 172A   -> shape_172A
    113STLUP     -> 113    -> shape_113

Matching strategy (in order):
    1. Exact:   shape_{ref}        e.g. shape_828A
    2. Shorter: shape_{ref[:-1]}   e.g. shape_828  (drop trailing letter)
    3. Shorter: shape_{ref[:-2]}   (only if ref >= 4 chars)

Usage:
    python link_shapes_to_trips.py
"""

import re
import pandas as pd
from pathlib import Path

TRIPS_IN   = "data/raw/bus/trips.txt"
ROUTES_IN  = "data/raw/bus/routes.txt"
SHAPES_IN  = "data/raw/bus/shapes.txt"
TRIPS_OUT  = "data/processed/bus/trips.csv"

SUFFIX_PATTERN = re.compile(
    r"(PMSLSTLDOWN\d*|PMSLSTLUP\d*|PMSLDOWN\d*|PMSLUP\d*|"
    r"LNKSTLDOWN\d*|LNKSTLUP\d*|LNKSTLDWN\d*|LNKSTL\d*|"
    r"STLDOWN\d*|STLUP\d*|STLDWN\d*|STL\d*|"
    r"DOWN\d*|DWN\d*|UP\d+|UP$|EXT\d*)$",
    re.IGNORECASE
)

# Special route name mappings — OSM ref → shape_id fragment
# These are routes where OSM uses a descriptive name, not a number
SPECIAL_MAP = {
    "OMS(+)":            "(+) OMS",
    "OMS(-)":            "(-) OMS",
    "TMS(+)":            "(+) TMS",
    "TMS(-)":            "(-) TMS",
    "(+)OUTERMUDRIKA":   "(+) OMS",
    "(-)OUTERMUDRIKA":   "(-) OMS",
    "OUTERMUDRIKA":      "Outer Mudrika Service",
    "GR.MUDRIKA":        "Gr. Mudrika",
    "OUTERMUDRKASERVICE":"Outer Mudrika Service",
    "MS":                "MS",
    "BPG":               "BPG",
    "HUDA":              "HUDA",
    "EXPRESS":           "EXPRESS",
}


def extract_route_ref(long_name: str) -> str:
    """Strip direction + night-service suffixes from route_long_name."""
    if pd.isna(long_name):
        return ""
    s = str(long_name).strip().upper()

    # Strip (NS) night-service marker e.g. 0114(NS) → 0114
    s = re.sub(r"\(NS\)$", "", s).strip()

    # Two passes for stacked suffixes e.g. LNKSTLUP2
    s = SUFFIX_PATTERN.sub("", s).strip()
    s = SUFFIX_PATTERN.sub("", s).strip()

    return s if s else str(long_name).strip().upper()


SHORT_ROUTES = {"1","8","33","34","39","47","48","66","73","85","88","8A","94","99","MS"}

def normalise_ref(ref: str) -> list:
    """
    Generate candidate ref variants in priority order.

    Handles:
      - Leading zeros:    0114  → 114
      - (NS) remnants:    0114  → 114
      - Trailing +/-:     CBD1+ → CBD1
      - Hyphens:          GL-91 → GL91
      - LNK suffix:       136LNK → 136
      - AIRPORTEXP:       0AIRPORTEXP-4 → AIRPORTEXP-4 → AIR-05 etc.
      - Special names:    OMS(+) → (+ ) OMS
      - Progressive trim: 828A → 828
    """
    if not ref:
        return []

    candidates = []

    # Check special map first
    if ref in SPECIAL_MAP:
        candidates.append(SPECIAL_MAP[ref])

    # Original
    candidates.append(ref)

    # Strip leading zeros (0114 → 114, 0GL-23 → GL-23)
    no_lead_zero = re.sub(r"^0+(?=[A-Za-z0-9])", "", ref)
    if no_lead_zero != ref and no_lead_zero:
        if len(no_lead_zero) >= 3 or no_lead_zero in SHORT_ROUTES:
            candidates.append(no_lead_zero)
        if no_lead_zero in SPECIAL_MAP:
            candidates.append(SPECIAL_MAP[no_lead_zero])

    # Strip trailing + or - (CBD1+ → CBD1, 103B+ → 103B)
    no_trail = re.sub(r"[+]+$", "", ref)
    if no_trail != ref:
        candidates.append(no_trail)

    # Remove hyphens (GL-91 → GL91, AIR-05 → AIR05)
    no_hyphen = ref.replace("-", "")
    if no_hyphen != ref:
        candidates.append(no_hyphen)

    # Leading zero + no hyphen
    if no_lead_zero != ref:
        candidates.append(no_lead_zero.replace("-", ""))

    # Strip LNK (136LNK → 136)
    no_lnk = re.sub(r"LNK$", "", ref, flags=re.IGNORECASE)
    if no_lnk != ref:
        candidates.append(no_lnk)

    # Trim trailing letters only -- never trim digits
    # 828A -> 828  ok    828 -> stop, never go to 82 or 8
    # 172A -> 172  ok    991A -> 991  ok   103B -> 103  ok
    # Guard: only accept trim result if it is >= 3 chars OR is a known
    # short route (1-2 chars). Prevents 828A matching shape_8.
    base = no_lead_zero if no_lead_zero != ref else ref
    trimmed = base
    while trimmed and trimmed[-1].isalpha() and len(trimmed) > 1:
        trimmed = trimmed[:-1]
        # Only add if long enough or an exact known short route
        if len(trimmed) >= 3 or trimmed in SHORT_ROUTES:
            candidates.append(trimmed)

    # Deduplicate preserving order
    seen, unique = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def find_best_shape(ref: str, shape_set: set) -> str | None:
    for candidate in normalise_ref(ref):
        shape_id = f"shape_{candidate}"
        if shape_id in shape_set:
            return shape_id
    return None


def main():
    routes = pd.read_csv(ROUTES_IN, dtype=str)
    trips  = pd.read_csv(TRIPS_IN,  dtype=str)
    shapes = pd.read_csv(SHAPES_IN, dtype=str)

    shape_set = set(shapes["shape_id"].unique())
    print(f"Routes:  {len(routes):,}")
    print(f"Trips:   {len(trips):,}")
    print(f"Shapes:  {len(shape_set)} unique shape_ids")

    route_to_shape = {}
    match_detail   = []

    for _, r in routes.iterrows():
        long_name = r.get("route_long_name", "")
        ref       = extract_route_ref(long_name)
        shape_id  = find_best_shape(ref, shape_set)
        if shape_id:
            route_to_shape[r["route_id"]] = shape_id
        match_detail.append({
            "route_id":        r["route_id"],
            "route_long_name": long_name,
            "extracted_ref":   ref,
            "shape_id":        shape_id or "",
            "matched":         shape_id is not None,
        })

    detail_df = pd.DataFrame(match_detail)
    matched   = detail_df["matched"].sum()

    print(f"\nRoutes matched:   {matched} / {len(routes)} ({matched/len(routes)*100:.1f}%)")
    print(f"Routes unmatched: {len(routes) - matched}")

    print("\nSample matched routes:")
    print(detail_df[detail_df["matched"]][["route_long_name","extracted_ref","shape_id"]]
          .head(15).to_string(index=False))

    print("\nSample unmatched routes:")
    print(detail_df[~detail_df["matched"]][["route_long_name","extracted_ref"]]
          .head(15).to_string(index=False))

    if "shape_id" not in trips.columns:
        trips["shape_id"] = None
    trips["shape_id"] = trips["route_id"].map(route_to_shape)

    linked = trips["shape_id"].notna().sum()
    print(f"\nTrips linked:   {linked:,} ({linked/len(trips)*100:.1f}%)")
    print(f"Trips unlinked: {trips['shape_id'].isna().sum():,}")

    Path(TRIPS_OUT).parent.mkdir(parents=True, exist_ok=True)
    trips.to_csv(TRIPS_OUT, index=False)

    report_path = "data/processed/shape_match_report.csv"
    detail_df.to_csv(report_path, index=False)

    print(f"\n✓ trips.csv        → {TRIPS_OUT}")
    print(f"✓ match report     → {report_path}")


if __name__ == "__main__":
    main()