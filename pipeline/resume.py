# resume.py  — run once to finish the failed ingest
import sys
sys.path.insert(0, 'pipeline')
from ingest import get_engine, load_ward_boundaries, initialise_ward_metrics, refresh_route_geometries, verify_load

engine = get_engine()
load_ward_boundaries(engine)       # re-loads wards with null fix + adds PK
initialise_ward_metrics(engine)    # FK will now succeed
refresh_route_geometries(engine)
verify_load(engine)