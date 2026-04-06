import folium
from folium.plugins import MarkerCluster, Fullscreen
import pandas as pd
import branca.colormap as cm

# Propus Brand Colors
PRIMARY_BLUE = "#005fb0"
TERTIARY_ORANGE = "#d6873c"
SURFACE_HIGHEST = "#e5e2dc"
OUTLINE = "#727783"
ERROR_RED = "#ba1a1a"

DELHI_LAT, DELHI_LON = 28.6139, 77.2090
DEFAULT_ZOOM = 11


def build_base_map() -> folium.Map:
    m = folium.Map(
        location=[DELHI_LAT, DELHI_LON],
        zoom_start=DEFAULT_ZOOM,
        tiles="CartoDB positron",
        attr="© Propus Intel | CartoDB",
        prefer_canvas=True,
        zoom_control=False,
    )
    Fullscreen(position="topright").add_to(m)
    return m


# ── Choropleth ────────────────────────────────────────────────────────────────

def add_choropleth_layer(m: folium.Map, map_data: dict) -> None:
    geojson = map_data.get("geojson")
    values  = map_data.get("values", {})
    legend  = map_data.get("legend", "Transit Stress")

    # ── Fix: strip None values before computing scale bounds ─────────────────
    # urban_stress_index / stops_per_10k are NULL in DB until RS pipeline runs.
    # The values dict arrives as {"ward_id": None, ...} — min/max crash on None.
    clean_values = {k: v for k, v in values.items() if v is not None}

    if not clean_values:
        # RS metrics not loaded yet — fall back to a flat grey map, no crash
        def style_fn(feature):
            return {
                "fillColor": SURFACE_HIGHEST,
                "fillOpacity": 0.55,
                "color": OUTLINE,
                "weight": 0.5,
            }
        folium.GeoJson(
            geojson,
            style_function=style_fn,
            tooltip=folium.GeoJsonTooltip(
                fields=["ward_name"],
                aliases=["Ward:"],
                style="font-family: Inter; font-size: 12px; padding: 10px;",
            ),
            name=legend,
        ).add_to(m)
        return

    vmin = min(clean_values.values())
    vmax = max(clean_values.values())
    # Guard against all-same values (flat raster) so colormap range isn't 0
    if vmin == vmax:
        vmax = vmin + 1

    colormap = cm.LinearColormap(
        colors=["#fcf9f3", TERTIARY_ORANGE, ERROR_RED],
        index=[0, 0.5, 1],
        vmin=vmin,
        vmax=vmax,
    )

    def style_fn(feature):
        ward_id = str(feature.get("properties", {}).get("ward_id", ""))
        # ── Fix: default None / missing ward to vmin (rendered as lightest) ──
        val = clean_values.get(ward_id, vmin)
        return {
            "fillColor": colormap(val),
            "fillOpacity": 0.7,
            "color": OUTLINE,
            "weight": 0.5,
        }

    folium.GeoJson(
        geojson,
        style_function=style_fn,
        tooltip=folium.GeoJsonTooltip(
            fields=["ward_name"],
            aliases=["Ward:"],
            style="font-family: Inter; font-size: 12px; padding: 10px;",
        ),
        name=legend,
    ).add_to(m)

    colormap.caption = legend
    colormap.add_to(m)


# ── Stops ─────────────────────────────────────────────────────────────────────

def add_stops_layer(m: folium.Map, map_data: dict) -> None:
    stops = map_data.get("stops", [])
    if not stops:
        return

    cluster = MarkerCluster(
        name="Transit Stops",
        disable_clustering_at_zoom=15,
    ).add_to(m)

    for s in stops:
        is_metro = s.get("mode") == "metro"
        color    = PRIMARY_BLUE if is_metro else TERTIARY_ORANGE
        folium.CircleMarker(
            location=[s["lat"], s["lon"]],
            radius=4 if is_metro else 3,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.9,
            weight=1,
            tooltip=f"<b>{s.get('name','Stop')}</b><br>{str(s.get('mode','')).upper()}",
        ).add_to(cluster)


# ── Route ─────────────────────────────────────────────────────────────────────

def add_route_layer(m: folium.Map, map_data: dict) -> None:
    shape    = map_data.get("shape", [])
    route_id = map_data.get("route_id", "Route")

    if not shape:
        return

    # Outer glow
    folium.PolyLine(locations=shape, color=PRIMARY_BLUE, weight=6, opacity=0.3).add_to(m)
    # Inner line
    folium.PolyLine(
        locations=shape, color=PRIMARY_BLUE, weight=2.5, opacity=1,
        tooltip=f"Corridor: {route_id}",
    ).add_to(m)
    m.fit_bounds(shape)


# ── Buffer ────────────────────────────────────────────────────────────────────

def add_buffer_layer(m: folium.Map, map_data: dict) -> None:
    stations = map_data.get("stations", [])

    for station in stations:
        folium.Circle(
            location=[station["lat"], station["lon"]],
            radius=station.get("radius_m", 500),
            color=PRIMARY_BLUE,
            weight=1,
            fill=True,
            fill_color=PRIMARY_BLUE,
            fill_opacity=0.1,
            dash_array="5, 5",
        ).add_to(m)

        folium.CircleMarker(
            location=[station["lat"], station["lon"]],
            radius=3,
            color="white",
            weight=2,
            fill=True,
            fill_color=PRIMARY_BLUE,
            fill_opacity=1,
            tooltip=station.get("name", "Station"),
        ).add_to(m)