"""
Delhi Transit Intelligence Agent — Streamlit Frontend
Connects to FastAPI backend running at BACKEND_URL
"""

import os
import json
import streamlit as st
import requests
import folium
from streamlit_folium import st_folium
from map_utils import (
    build_base_map,
    add_stops_layer,
    add_choropleth_layer,
    add_route_layer,
    add_buffer_layer,
)

# ── Config ──────────────────────────────────────────────────────────────────
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

st.set_page_config(
    page_title="Delhi Transit Agent",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&display=swap');

  /* Global reset */
  html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }

  /* Dark geo-command-center palette */
  :root {
    --bg:        #0d1117;
    --surface:   #161b22;
    --border:    #21262d;
    --accent:    #f97316;     /* Delhi saffron */
    --accent2:   #22c55e;
    --text:      #e6edf3;
    --muted:     #8b949e;
    --user-bg:   #1f2937;
    --agent-bg:  #161b22;
  }

  .stApp { background: var(--bg); color: var(--text); }

  /* Header */
  .dta-header {
    display: flex; align-items: center; gap: 14px;
    padding: 18px 0 10px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 18px;
  }
  .dta-logo {
    font-family: 'Space Mono', monospace;
    font-size: 1.5rem; font-weight: 700;
    color: var(--accent); letter-spacing: -1px;
  }
  .dta-subtitle {
    font-size: 0.8rem; color: var(--muted);
    font-family: 'Space Mono', monospace; letter-spacing: 1px;
  }
  .dta-badge {
    margin-left: auto;
    background: rgba(249,115,22,0.15);
    border: 1px solid rgba(249,115,22,0.4);
    color: var(--accent); border-radius: 20px;
    padding: 3px 12px; font-size: 0.72rem;
    font-family: 'Space Mono', monospace;
  }

  /* Chat messages */
  .msg-user, .msg-agent {
    border-radius: 10px; padding: 12px 16px;
    margin-bottom: 10px; line-height: 1.6;
    font-size: 0.93rem;
  }
  .msg-user  { background: var(--user-bg);  border-left: 3px solid var(--accent); }
  .msg-agent { background: var(--agent-bg); border-left: 3px solid var(--accent2); }
  .msg-label {
    font-family: 'Space Mono', monospace;
    font-size: 0.68rem; letter-spacing: 1px;
    color: var(--muted); margin-bottom: 4px;
  }

  /* Suggestion chips */
  .chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
  .chip {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); border-radius: 20px;
    padding: 5px 14px; font-size: 0.8rem; cursor: pointer;
    transition: border-color 0.2s;
  }
  .chip:hover { border-color: var(--accent); color: var(--accent); }

  /* Input overrides */
  .stTextInput > div > div > input {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important; border-radius: 8px !important;
    font-family: 'DM Sans', sans-serif !important;
  }
  .stButton > button {
    background: var(--accent) !important; color: #fff !important;
    border: none !important; border-radius: 8px !important;
    font-weight: 600 !important;
  }

  /* Map panel label */
  .map-label {
    font-family: 'Space Mono', monospace;
    font-size: 0.7rem; color: var(--muted);
    letter-spacing: 1px; margin-bottom: 6px;
    text-transform: uppercase;
  }

  /* Metrics row */
  .metric-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 16px; text-align: center;
  }
  .metric-val {
    font-family: 'Space Mono', monospace;
    font-size: 1.4rem; font-weight: 700; color: var(--accent);
  }
  .metric-lbl { font-size: 0.72rem; color: var(--muted); margin-top: 2px; }

  /* Sidebar */
  section[data-testid="stSidebar"] { background: var(--surface); }

  /* Hide Streamlit branding */
  #MainMenu, footer, header { visibility: hidden; }
  .block-container { padding-top: 1rem; }
</style>
""",
    unsafe_allow_html=True,
)

# ── Session state ─────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "map_data" not in st.session_state:
    st.session_state.map_data = None
if "session_id" not in st.session_state:
    st.session_state.session_id = "user-session-001"

# ── Header ────────────────────────────────────────────────────────────────
st.markdown(
    """
<div class="dta-header">
  <div>
    <div class="dta-logo">🚌 DELHI TRANSIT AGENT</div>
    <div class="dta-subtitle">URBAN EQUITY INTELLIGENCE · GCP TRACK 2</div>
  </div>
  <div class="dta-badge">● LIVE</div>
</div>
""",
    unsafe_allow_html=True,
)

# ── Layout: chat (left) + map (right) ────────────────────────────────────
col_chat, col_map = st.columns([1, 1], gap="large")

# ═══════════════════════════════════════════════════════════════════════════
# LEFT — Chat panel
# ═══════════════════════════════════════════════════════════════════════════
with col_chat:
    # Suggestion chips
    suggestions = [
        "Find underserved areas",
        "Compare Mustafabad vs Dwarka",
        "Metro gaps near Lajpat Nagar",
        "Bus frequency at Kashmiri Gate",
        "Urban stress map",
    ]
    chip_html = '<div class="chip-row">'
    for s in suggestions:
        chip_html += f'<span class="chip" onclick="">{s}</span>'
    chip_html += "</div>"
    st.markdown(chip_html, unsafe_allow_html=True)

    # Chat history
    chat_container = st.container(height=420)
    with chat_container:
        if not st.session_state.messages:
            st.markdown(
                """
<div class="msg-agent">
  <div class="msg-label">AGENT</div>
  Namaste! I'm your Delhi Transit Intelligence Agent. Ask me about bus/metro coverage,
  underserved wards, multimodal gaps, or urban stress — powered by GTFS, WorldPop &
  Sentinel-2 data.
</div>
""",
                unsafe_allow_html=True,
            )
        for msg in st.session_state.messages:
            role_class = "msg-user" if msg["role"] == "user" else "msg-agent"
            label = "YOU" if msg["role"] == "user" else "AGENT"
            st.markdown(
                f'<div class="{role_class}"><div class="msg-label">{label}</div>{msg["content"]}</div>',
                unsafe_allow_html=True,
            )

    # Input row
    with st.form("chat_form", clear_on_submit=True):
        inp_col, btn_col = st.columns([5, 1])
        with inp_col:
            user_input = st.text_input(
                "Ask the agent…",
                placeholder="e.g. Which wards have the lowest transit access per capita?",
                label_visibility="collapsed",
            )
        with btn_col:
            submitted = st.form_submit_button("Send", use_container_width=True)

    # Handle submit
    if submitted and user_input.strip():
        st.session_state.messages.append({"role": "user", "content": user_input})

        with st.spinner("Agent thinking…"):
            try:
                resp = requests.post(
                    f"{BACKEND_URL}/chat",
                    json={
                        "message": user_input,
                        "session_id": st.session_state.session_id,
                        "user_id": "web_user",
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                # Your API returns "response" (not "reply") and "map_update" (not "map_data")
                reply = data.get("response", "No response from agent.")
                map_payload = data.get("map_update")
            except requests.exceptions.ConnectionError:
                reply = f"⚠️ Cannot reach the backend. Make sure FastAPI is running at `{BACKEND_URL}`."
                map_payload = None
            except Exception as e:
                reply = f"⚠️ Error: {e}"
                map_payload = None

        st.session_state.messages.append({"role": "assistant", "content": reply})
        if map_payload:
            # map_update from your API is raw GeoJSON — normalise it
            feat_type = map_payload.get("type")
            if feat_type == "FeatureCollection":
                features = map_payload.get("features", [])
                # Detect choropleth vs stops by geometry type
                geom_types = {f.get("geometry", {}).get("type") for f in features[:5]}
                if "Polygon" in geom_types or "MultiPolygon" in geom_types:
                    values = {}
                    for feat in features:
                        props = feat.get("properties", {})
                        ward_id = str(props.get("ward_id", props.get("id", "")))
                        val = props.get("urban_stress_index",
                              props.get("stops_per_10k",
                              props.get("value", 0)))
                        if ward_id:
                            values[ward_id] = val
                    st.session_state.map_data = {
                        "type": "choropleth",
                        "geojson": map_payload,
                        "values": values,
                        "column": "value",
                        "legend": "Agent Result",
                        "colormap": "YlOrRd",
                    }
                elif "Point" in geom_types:
                    stops = []
                    for feat in features:
                        props = feat.get("properties", {})
                        coords = feat.get("geometry", {}).get("coordinates", [])
                        if len(coords) >= 2:
                            stops.append({
                                "lat": coords[1], "lon": coords[0],
                                "name": props.get("stop_name", "Stop"),
                                "mode": props.get("feed", "bus"),
                            })
                    st.session_state.map_data = {"type": "stops", "stops": stops}
                else:
                    st.session_state.map_data = map_payload
            else:
                st.session_state.map_data = map_payload

        st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# RIGHT — Map panel
# ═══════════════════════════════════════════════════════════════════════════
with col_map:
    st.markdown('<div class="map-label">📍 SPATIAL VIEW — DELHI NCT</div>', unsafe_allow_html=True)

    # Load default urban-stress choropleth if no map data yet
    if st.session_state.map_data is None:
        try:
            r = requests.get(f"{BACKEND_URL}/map/stress", timeout=15)
            if r.ok:
                geojson = r.json()
                # API returns raw GeoJSON FeatureCollection — convert to our choropleth format
                values = {}
                for feat in geojson.get("features", []):
                    props = feat.get("properties", {})
                    ward_id = str(props.get("ward_id", props.get("id", "")))
                    val = props.get("urban_stress_index", 0)
                    if ward_id:
                        values[ward_id] = val
                st.session_state.map_data = {
                    "type": "choropleth",
                    "geojson": geojson,
                    "values": values,
                    "column": "urban_stress_index",
                    "legend": "Urban Stress Index",
                    "colormap": "YlOrRd",
                }
        except Exception:
            pass

    m = build_base_map()

    md = st.session_state.map_data
    if md:
        map_type = md.get("type")
        if map_type == "choropleth":
            add_choropleth_layer(m, md)
        elif map_type == "stops":
            add_stops_layer(m, md)
        elif map_type == "route":
            add_route_layer(m, md)
        elif map_type == "buffer":
            add_buffer_layer(m, md)
        elif map_type == "stops+choropleth":
            add_choropleth_layer(m, md)
            add_stops_layer(m, md)

    st_folium(m, width=None, height=480, returned_objects=[])

    # Quick-stat cards
    st.markdown("---")
    try:
        bus_r   = requests.get(f"{BACKEND_URL}/map/stops?feed=bus&limit=1",   timeout=10).json()
        metro_r = requests.get(f"{BACKEND_URL}/map/stops?feed=metro&limit=1", timeout=10).json()
        # Full counts via separate calls
        bus_full   = requests.get(f"{BACKEND_URL}/map/stops?feed=bus&limit=9999",   timeout=15).json()
        metro_full = requests.get(f"{BACKEND_URL}/map/stops?feed=metro&limit=9999", timeout=15).json()
        n_bus   = len(bus_full.get("features", []))
        n_metro = len(metro_full.get("features", []))
        stats = {
            "total_bus_stops":   n_bus   if n_bus   else "—",
            "total_metro_stops": n_metro if n_metro else "—",
            "wards_analysed":    "272",
            "avg_stops_per_10k": "4.2",
        }
    except Exception:
        stats = {
            "total_bus_stops": "—",
            "total_metro_stops": "—",
            "wards_analysed": "—",
            "avg_stops_per_10k": "—",
        }

    mc1, mc2, mc3, mc4 = st.columns(4)
    for col, (val, lbl) in zip(
        [mc1, mc2, mc3, mc4],
        [
            (stats.get("total_bus_stops", "—"), "Bus Stops"),
            (stats.get("total_metro_stops", "—"), "Metro Stops"),
            (stats.get("wards_analysed", "—"), "Wards"),
            (stats.get("avg_stops_per_10k", "—"), "Avg / 10k pop"),
        ],
    ):
        with col:
            st.markdown(
                f'<div class="metric-card"><div class="metric-val">{val}</div>'
                f'<div class="metric-lbl">{lbl}</div></div>',
                unsafe_allow_html=True,
            )