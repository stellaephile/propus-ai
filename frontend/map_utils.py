"""
map_utils.py — Folium helper functions for Delhi Transit Agent frontend.

Each add_* function mutates the passed folium.Map object in place.
map_data dict shapes are defined by the FastAPI /chat and /map/* endpoints.
"""

import folium
from folium.plugins import HeatMap, MarkerCluster
import pandas as pd 

# Delhi centre
DELHI_LAT, DELHI_LON = 28.6139, 77.2090
DEFAULT_ZOOM = 11


def build_base_map() -> folium.Map:
    """Return a dark-themed Folium map centred on Delhi."""
    m = folium.Map(
        location=[DELHI_LAT, DELHI_LON],
        zoom_start=DEFAULT_ZOOM,
        tiles="CartoDB dark_matter",
        attr="© CartoDB",
        prefer_canvas=True,
    )
    return m


# ── Choropleth ─────────────────────────────────────────────────────────────

def add_choropleth_layer(m: folium.Map, map_data: dict) -> None:
    geojson = map_data.get("geojson")
    values = map_data.get("values", {})
    column = map_data.get("column", "value")
    legend = map_data.get("legend", column)
    colormap = map_data.get("colormap", "YlOrRd")

    if not geojson or not values:
        return

    # Convert dict → DataFrame and coerce to float
    values_df = pd.DataFrame(
        list(values.items()), columns=["ward_id", column]
    )
    values_df[column] = pd.to_numeric(values_df[column], errors="coerce")
    values_df = values_df.dropna(subset=[column])

    # Attach value to each feature for tooltip
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        ward_id = str(props.get("ward_id") or props.get("id") or "")
        props["_value"] = values.get(ward_id, 0)
        feature["properties"] = props

    folium.Choropleth(
        geo_data=geojson,
        name=legend,
        data=values_df,           # ← DataFrame, not dict
        columns=["ward_id", column],
        key_on="feature.properties.ward_id",
        fill_color=colormap,
        fill_opacity=0.65,
        line_opacity=0.3,
        legend_name=legend,
        nan_fill_color="#21262d",
    ).add_to(m)

    # Tooltip overlay (unchanged)
    folium.GeoJson(
        geojson,
        style_function=lambda f: {"fillOpacity": 0, "weight": 0.5, "color": "#8b949e"},
        tooltip=folium.GeoJsonTooltip(
            fields=["ward_name", "_value"],
            aliases=["Ward", legend],
            localize=True,
            sticky=False,
        ),
    ).add_to(m)
# ── Stops ──────────────────────────────────────────────────────────────────

def add_stops_layer(m: folium.Map, map_data: dict) -> None:
    """
    Plot transit stops as circle markers (clustered if > 200).

    Expected map_data keys:
      stops  – list of dicts with keys: lat, lon, name, mode ('bus'|'metro')
    """
    stops = map_data.get("stops", [])
    if not stops:
        return

    colour_map = {"bus": "#f97316", "metro": "#22c55e"}

    if len(stops) > 200:
        cluster = MarkerCluster(name="Stops").add_to(m)
        for s in stops:
            folium.CircleMarker(
                location=[s["lat"], s["lon"]],
                radius=4,
                color=colour_map.get(s.get("mode", "bus"), "#f97316"),
                fill=True,
                fill_opacity=0.8,
                popup=folium.Popup(s.get("name", "Stop"), max_width=200),
            ).add_to(cluster)
    else:
        fg = folium.FeatureGroup(name="Stops").add_to(m)
        for s in stops:
            folium.CircleMarker(
                location=[s["lat"], s["lon"]],
                radius=5,
                color=colour_map.get(s.get("mode", "bus"), "#f97316"),
                fill=True,
                fill_opacity=0.85,
                tooltip=s.get("name", "Stop"),
            ).add_to(fg)

    folium.LayerControl().add_to(m)


# ── Route ──────────────────────────────────────────────────────────────────

def add_route_layer(m: folium.Map, map_data: dict) -> None:
    """
    Draw a route polyline plus its stops.

    Expected map_data keys:
      shape    – list of [lat, lon] pairs (route polyline)
      stops    – list of stop dicts (same schema as add_stops_layer)
      route_id – short route identifier for label
    """
    shape = map_data.get("shape", [])
    stops = map_data.get("stops", [])
    route_id = map_data.get("route_id", "Route")

    if shape:
        folium.PolyLine(
            locations=shape,
            color="#f97316",
            weight=3,
            opacity=0.9,
            tooltip=route_id,
        ).add_to(m)

    for s in stops:
        folium.CircleMarker(
            location=[s["lat"], s["lon"]],
            radius=4,
            color="#fbbf24",
            fill=True,
            fill_opacity=0.9,
            tooltip=s.get("name", "Stop"),
        ).add_to(m)

    if shape:
        m.fit_bounds([[min(p[0] for p in shape), min(p[1] for p in shape)],
                      [max(p[0] for p in shape), max(p[1] for p in shape)]])


# ── Buffer / catchment ─────────────────────────────────────────────────────

def add_buffer_layer(m: folium.Map, map_data: dict) -> None:
    """
    Draw buffer circles around metro station catchments.

    Expected map_data keys:
      stations – list of dicts: { name, lat, lon, radius_m }
      geojson  – optional dissolved buffer GeoJSON
    """
    stations = map_data.get("stations", [])
    geojson = map_data.get("geojson")

    if geojson:
        folium.GeoJson(
            geojson,
            style_function=lambda _: {
                "fillColor": "#22c55e",
                "fillOpacity": 0.2,
                "color": "#22c55e",
                "weight": 1.5,
            },
            name="Metro catchment",
        ).add_to(m)

    for st_data in stations:
        # Station marker
        folium.CircleMarker(
            location=[st_data["lat"], st_data["lon"]],
            radius=7,
            color="#22c55e",
            fill=True,
            fill_opacity=1.0,
            tooltip=st_data.get("name", "Metro station"),
        ).add_to(m)
        # Radius circle
        folium.Circle(
            location=[st_data["lat"], st_data["lon"]],
            radius=st_data.get("radius_m", 500),
            color="#22c55e",
            fill=True,
            fill_opacity=0.1,
            weight=1,
        ).add_to(m)

    if stations:
        m.location = [stations[0]["lat"], stations[0]["lon"]]
        m.zoom_start = 14