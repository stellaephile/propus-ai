"""
Propus AI — Delhi Transit Intelligence
Streamlit Frontend v2 — fixes:
  1. Chat uses st.chat_message() instead of raw HTML bubbles
     (st.container + unsafe_allow_html is unreliable in Streamlit ≥1.30)
  2. Left panel no longer clips at top (removed conflicting padding CSS)
  3. Send response actually appears after submit
"""

import os
import requests
import streamlit as st
from streamlit_folium import st_folium
from map_utils import (
    build_base_map,
    add_stops_layer,
    add_choropleth_layer,
    add_route_layer,
    add_buffer_layer,
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

st.set_page_config(
    page_title="Propus · Delhi Transit Intelligence",
    page_icon="🛰",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Session state ────────────────────────────────────────────────────────────
if "messages"       not in st.session_state: st.session_state.messages       = []
if "map_data"       not in st.session_state: st.session_state.map_data       = None
if "session_id"     not in st.session_state: st.session_state.session_id     = "user-session-001"
if "show_info_card" not in st.session_state: st.session_state.show_info_card = True

# ── Global CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400;1,6..72,600;1,6..72,700&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&family=DM+Mono:wght@400;500&display=swap');

:root {
  --navy:      #0D1C2E;
  --bg:        #ede9e1;
  --white:     #ffffff;
  --border:    rgba(0,0,0,0.08);
  --border-md: rgba(0,0,0,0.13);
  --primary:   #1a4fa0;
  --primary-h: #2e62b8;
  --amber:     #c97a2a;
  --text:      #1a1a1a;
  --muted:     #6b7280;
  --light:     #9ca3af;
  --nav-h:     52px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
  --shadow-lg: 0 8px 32px rgba(0,0,0,0.14);
}

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif !important; }
#MainMenu, footer, header  { visibility: hidden !important; }
div[data-testid="stToolbar"],
div[data-testid="stDecoration"] { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }

.stApp { background: var(--bg) !important; }

/* Remove default block-container padding — nav offset handled per-column */
.block-container {
  padding: 0 !important;
  max-width: 100% !important;
}

/* Remove column gap */
div[data-testid="stHorizontalBlock"] {
  gap: 0 !important;
  align-items: stretch !important;
}
div[data-testid="column"] { padding: 0 !important; }

/* ── Fixed top nav ── */
.propus-nav {
  position: fixed;
  top: 0; left: 0; right: 0;
  height: var(--nav-h);
  background: var(--navy);
  display: flex;
  align-items: center;
  padding: 0 24px;
  z-index: 9999;
  border-bottom: 1px solid rgba(255,255,255,0.05);
}
.pn-logo {
  font-family: 'Newsreader', serif;
  font-style: italic; font-size: 1.32rem; font-weight: 600;
  color: #fff; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 5px;
  margin-right: 36px; flex-shrink: 0;
}
.pn-dot { display:inline-block; width:5px; height:5px; background:#c97a2a; border-radius:50%; }
.pn-tabs { flex:1; display:flex; align-items:center; justify-content:center; height:100%; }
.pn-tab {
  font-family: 'DM Sans', sans-serif;
  font-size: .74rem; font-weight: 500; letter-spacing: .05em; text-transform: uppercase;
  color: rgba(255,255,255,0.48);
  padding: 0 20px; height: 100%;
  display: flex; align-items: center;
  border-bottom: 2px solid transparent;
  transition: color .15s;
}
.pn-tab.active { color:#fff; border-bottom-color:#fff; font-weight:600; }
.pn-right { display:flex; align-items:center; gap:8px; flex-shrink:0; margin-left:20px; }
.pn-badge {
  display:flex; align-items:center; gap:5px;
  padding: 3px 9px;
  border: 1px solid rgba(255,255,255,0.13); border-radius: 2px;
  font-family: 'DM Mono', monospace; font-size: .57rem;
  letter-spacing:.07em; text-transform:uppercase; color: rgba(255,255,255,0.58);
}
.pn-bdot { width:6px; height:6px; border-radius:50%; flex-shrink:0; }
.pn-sep  { width:1px; height:17px; background:rgba(255,255,255,0.13); margin:0 5px; }
.pn-label {
  font-family:'DM Sans',sans-serif; font-size:.66rem;
  letter-spacing:.08em; text-transform:uppercase; color:rgba(255,255,255,0.38);
}

/* ── Left column ── */
div[data-testid="column"]:first-child {
  background: var(--bg);
  border-right: 1px solid rgba(0,0,0,0.09);
  min-height: 100vh;
  padding-top: var(--nav-h) !important;
  display: flex;
  flex-direction: column;
}

/* ── Right column ── */
div[data-testid="column"]:last-child {
  background: #cbc7bf;
  position: relative;
  min-height: 100vh;
  padding-top: var(--nav-h) !important;
}

/* ── Headings ── */
.ask-h {
  font-family:'Newsreader',serif; font-size:1.8rem; font-weight:400;
  color:var(--text); margin:0 0 3px; line-height:1.15;
}
.ask-sub { font-size:.82rem; color:var(--muted); margin:0 0 16px; }

/* ── st.chat_message overrides ── */
div[data-testid="stChatMessage"] {
  background: transparent !important;
  padding: 2px 0 !important;
}
/* User bubble */
div[data-testid="stChatMessage"][data-testid*="user"] .stChatMessageContent,
div[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) .stMarkdown {
  background: #e8eefa !important;
  border: 1px solid rgba(26,79,160,.12) !important;
  border-radius: 4px !important;
  padding: 10px 14px !important;
  font-size: .84rem !important;
}
/* Assistant bubble */
div[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) .stMarkdown {
  background: var(--white) !important;
  border: 1px solid var(--border) !important;
  border-radius: 4px !important;
  padding: 12px 14px !important;
  font-size: .84rem !important;
  box-shadow: var(--shadow-sm) !important;
}
/* Hide default avatars — we use our own label */
div[data-testid="stChatMessage"] img,
div[data-testid="stChatMessage"] [data-testid*="Avatar"] { display: none !important; }

/* ── Suggestion buttons ── */
.sug-hdr {
  font-family:'DM Mono',monospace; font-size:.57rem;
  letter-spacing:.14em; text-transform:uppercase;
  color:var(--light); margin-bottom:8px;
}
div[data-testid="column"]:first-child .stButton > button {
  background: var(--white) !important;
  color: var(--text) !important;
  border: 1px solid rgba(0,0,0,0.09) !important;
  border-radius: 4px !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: .82rem !important; font-weight: 400 !important;
  text-align: left !important; padding: 11px 13px !important;
  width: 100% !important; box-shadow: none !important;
  text-transform: none !important; margin-bottom: 5px !important;
  transition: border-color .15s !important;
}
div[data-testid="column"]:first-child .stButton > button:hover {
  border-color: rgba(26,79,160,.3) !important;
  background: #f8f6f2 !important;
}

/* ── Chat input at bottom ── */
div[data-testid="stChatInput"] {
  background: var(--white) !important;
  border: 1px solid var(--border-md) !important;
  border-radius: 4px !important;
  box-shadow: none !important;
}
div[data-testid="stChatInput"] textarea {
  font-family: 'DM Sans', sans-serif !important;
  font-size: .84rem !important; color: var(--text) !important;
}
div[data-testid="stChatInput"] button {
  background: var(--primary) !important;
  border-radius: 3px !important;
}
div[data-testid="stChatInput"] button:hover {
  background: var(--primary-h) !important;
}

/* Divider */
.left-divider { border:none; border-top:1px solid rgba(0,0,0,0.08); margin:12px 0 10px; }

/* Footer */
.ifooter {
  display:flex; justify-content:space-between; align-items:center;
  padding: 6px 2px 10px;
}
.ifooter-link { font-size:.7rem; color:var(--muted); margin-right:12px; }
.ifooter-ver  {
  font-family:'DM Mono',monospace; font-size:.56rem;
  color:var(--light); letter-spacing:.1em; text-transform:uppercase;
}

/* ── Map floating overlays ── */
.fc {
  position: absolute;
  background: rgba(255,255,255,0.93);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(0,0,0,0.07); border-radius: 4px;
  box-shadow: var(--shadow-lg); z-index: 1000;
}
.fc-legend { bottom:18px; left:18px; padding:14px 16px; min-width:190px; }
.fc-legend-t {
  font-family:'DM Mono',monospace; font-size:.55rem;
  letter-spacing:.14em; text-transform:uppercase; color:var(--muted); margin-bottom:9px;
}
.fc-row   { display:flex; align-items:center; gap:8px; margin-bottom:5px; }
.fc-sw    { width:10px; height:10px; border-radius:2px; border:1px solid rgba(0,0,0,0.09); flex-shrink:0; }
.fc-lbl   { font-size:.7rem; color:#4b5563; }
.fc-ramp  {
  width:100%; height:4px; border-radius:999px;
  background:linear-gradient(to right,#ede9e1,#c97a2a,#8b1a1a); margin-top:10px;
}
.fc-info {
  top:18px; left:50%; transform:translateX(-50%);
  width:420px; max-width:calc(100% - 36px); padding:20px 22px 18px;
}
.fc-title { font-family:'Newsreader',serif; font-size:1.06rem; font-weight:600; margin:0 0 5px; }
.fc-body  { font-size:.78rem; color:var(--muted); line-height:1.6; margin:0 0 13px; }
.fc-actions { display:flex; gap:9px; }
.fc-btn-p {
  padding:7px 16px; background:var(--primary); color:#fff; border:none; border-radius:3px;
  font-family:'DM Mono',monospace; font-size:.62rem; font-weight:500;
  letter-spacing:.07em; text-transform:uppercase; cursor:pointer;
}
.fc-btn-o {
  padding:7px 16px; background:transparent; color:var(--text);
  border:1px solid var(--border-md); border-radius:3px;
  font-family:'DM Mono',monospace; font-size:.62rem;
  letter-spacing:.07em; text-transform:uppercase; cursor:pointer;
}

div[data-testid="stForm"] { border: none !important; padding: 0 !important; }
div[data-testid="column"]:last-child { position: relative !important; overflow: hidden !important; }
.stSpinner > div { border-top-color: var(--primary) !important; }
</style>

<div class="propus-nav">
  <div class="pn-logo">Propus<span class="pn-dot"></span></div>
  <div class="pn-tabs">
    <div class="pn-tab active">Intelligence</div>
    <div class="pn-tab">Fleet</div>
    <div class="pn-tab">Stations</div>
    <div class="pn-tab">Settings</div>
  </div>
  <div class="pn-right">
    <div class="pn-badge"><span class="pn-bdot" style="background:#22c55e;"></span>Bus GTFS</div>
    <div class="pn-badge"><span class="pn-bdot" style="background:#5b9bf0;"></span>Metro GTFS</div>
    <div class="pn-badge"><span class="pn-bdot" style="background:#2dd4bf;"></span>Sentinel-2</div>
    <div class="pn-sep"></div>
    <span class="pn-label">Delhi Transit Intelligence</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Two-column layout ────────────────────────────────────────────────────────
col_left, col_right = st.columns([38, 62], gap="small")

# ════════════════════════════════════════════════════════════════════════════
# LEFT PANEL
# ════════════════════════════════════════════════════════════════════════════
with col_left:
    st.markdown("""
    <div style="padding:24px 24px 0;">
      <div class="ask-h">Ask Propus</div>
      <div class="ask-sub">Natural language queries and spatial analysis.</div>
    </div>
    """, unsafe_allow_html=True)

    # ── Chat history — uses st.chat_message so Streamlit actually renders it ──
    chat_container = st.container(height=360, border=False)
    with chat_container:
        if not st.session_state.messages:
            with st.chat_message("assistant"):
                st.markdown(
                    "Namaste! Ask me about bus and metro coverage, underserved wards, "
                    "multimodal gaps, or urban stress across Delhi NCT."
                )
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    # ── Suggestions ──
    st.markdown('<div style="padding:0 24px;">', unsafe_allow_html=True)
    st.markdown('<div class="sug-hdr">Suggested starting points</div>',
                unsafe_allow_html=True)
    for s in [
        "How far is Mustafabad from the nearest metro?",
        "Compare transit access in Dwarka vs Seemapuri",
    ]:
        if st.button(s + "  →", key=f"s_{s[:14]}", use_container_width=True):
            st.session_state._prefill = s
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div style="padding:0 24px;"><hr class="left-divider"/></div>',
                unsafe_allow_html=True)

    # ── Chat input — st.chat_input stays pinned at bottom of its container ──
    st.markdown('<div style="padding:0 24px 6px;">', unsafe_allow_html=True)

    # Handle suggestion prefill
    prefill = st.session_state.pop("_prefill", None)
    if prefill:
        # Write to state and rerun so chat_input picks it up as a fresh submit
        st.session_state._pending_query = prefill

    user_input = st.chat_input(
        "Describe the transit analysis you need...",
        key="chat_input",
    )

    # Also consume any pending prefill query
    if not user_input and "_pending_query" in st.session_state:
        user_input = st.session_state.pop("_pending_query")

    st.markdown("""
    <div class="ifooter">
      <div><span class="ifooter-link">⊕ Attach</span><span class="ifooter-link">↗ Chart</span></div>
      <span class="ifooter-ver">V2.4 Ready</span>
    </div>
    """, unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Handle query ──────────────────────────────────────────────────────────
    if user_input and user_input.strip():
        st.session_state.messages.append({"role": "user", "content": user_input.strip()})
        st.session_state.show_info_card = False

        with st.spinner("Propus is reasoning..."):
            try:
                resp = requests.post(
                    f"{BACKEND_URL}/chat",
                    json={
                        "message":    user_input.strip(),
                        "session_id": st.session_state.session_id,
                        "user_id":    "web_user",
                    },
                    timeout=90,
                )
                resp.raise_for_status()
                data        = resp.json()
                reply       = data.get("response", "No response from agent.")
                map_payload = data.get("map_update")
            except requests.exceptions.ConnectionError:
                reply       = f"⚠ Cannot reach backend at `{BACKEND_URL}`. Is uvicorn running?"
                map_payload = None
            except requests.exceptions.Timeout:
                reply       = "⚠ Agent timed out (>90s). The query may be too complex — try rephrasing."
                map_payload = None
            except Exception as e:
                reply       = f"⚠ Error: {e}"
                map_payload = None

        st.session_state.messages.append({"role": "assistant", "content": reply})

        # ── Parse map payload ─────────────────────────────────────────────
        if map_payload:
            feat_type  = map_payload.get("type")
            if feat_type == "FeatureCollection":
                features   = map_payload.get("features", [])
                geom_types = {f.get("geometry", {}).get("type") for f in features[:5]}
                if "Polygon" in geom_types or "MultiPolygon" in geom_types:
                    values = {
                        str(f.get("properties", {}).get("ward_id",
                            f.get("properties", {}).get("id", ""))):
                        f.get("properties", {}).get("urban_stress_index",
                        f.get("properties", {}).get("stops_per_10k",
                        f.get("properties", {}).get("value", 0)))
                        for f in features
                    }
                    st.session_state.map_data = {
                        "type": "choropleth", "geojson": map_payload,
                        "values": values, "column": "value",
                        "legend": "Agent Result", "colormap": "YlOrRd",
                    }
                elif "Point" in geom_types:
                    stops = [
                        {
                            "lat":  f["geometry"]["coordinates"][1],
                            "lon":  f["geometry"]["coordinates"][0],
                            "name": f.get("properties", {}).get("stop_name", "Stop"),
                            "mode": f.get("properties", {}).get("feed", "bus"),
                        }
                        for f in features
                        if len(f.get("geometry", {}).get("coordinates", [])) >= 2
                    ]
                    st.session_state.map_data = {"type": "stops", "stops": stops}
            else:
                st.session_state.map_data = map_payload

        st.rerun()

# ════════════════════════════════════════════════════════════════════════════
# RIGHT PANEL — Map
# ════════════════════════════════════════════════════════════════════════════
with col_right:
    # Load default stress choropleth on first visit
    if st.session_state.map_data is None:
        try:
            r = requests.get(f"{BACKEND_URL}/map/stress", timeout=15)
            if r.ok:
                geojson = r.json()
                values  = {
                    str(f.get("properties", {}).get("ward_id",
                        f.get("properties", {}).get("id", ""))):
                    f.get("properties", {}).get("urban_stress_index", 0)
                    for f in geojson.get("features", [])
                }
                st.session_state.map_data = {
                    "type": "choropleth", "geojson": geojson,
                    "values": values, "column": "urban_stress_index",
                    "legend": "Urban Stress Index", "colormap": "YlOrRd",
                }
        except Exception:
            pass

    m  = build_base_map()
    md = st.session_state.map_data
    if md:
        t = md.get("type")
        if   t == "choropleth":       add_choropleth_layer(m, md)
        elif t == "stops":            add_stops_layer(m, md)
        elif t == "route":            add_route_layer(m, md)
        elif t == "buffer":           add_buffer_layer(m, md)
        elif t == "stops+choropleth":
            add_choropleth_layer(m, md)
            add_stops_layer(m, md)

    st_folium(m, width=None, height=820, returned_objects=[])

    # ── Floating overlays ────────────────────────────────────────────────────
    if st.session_state.show_info_card:
        st.markdown("""
        <div class="fc fc-info">
          <div style="display:flex;gap:15px;align-items:flex-start;">
            <div style="width:34px;height:34px;flex-shrink:0;background:rgba(26,79,160,.09);
                        border-radius:50%;display:flex;align-items:center;justify-content:center;
                        font-family:'DM Mono',monospace;font-size:.8rem;font-weight:600;color:#1a4fa0;">i</div>
            <div>
              <div class="fc-title">Intelligence Framework</div>
              <div class="fc-body">
                Propus uses Sentinel-2 imagery, WorldPop data, and GTFS feeds
                to map transit equity across Delhi's urban landscape.
              </div>
              <div class="fc-actions">
                <button class="fc-btn-p">Begin Analysis</button>
                <button class="fc-btn-o">Methodology</button>
              </div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("""
    <div class="fc fc-legend">
      <div class="fc-legend-t">Urban Stress Index</div>
      <div class="fc-row">
        <div class="fc-sw" style="background:#ede9e1;border-color:#ccc;"></div>
        <span class="fc-lbl">Low Stress (High Equity)</span>
      </div>
      <div class="fc-row">
        <div class="fc-sw" style="background:#c97a2a;"></div>
        <span class="fc-lbl">Moderate Pressure</span>
      </div>
      <div class="fc-row">
        <div class="fc-sw" style="background:#8b1a1a;"></div>
        <span class="fc-lbl">Critical / Underserved</span>
      </div>
      <div class="fc-ramp"></div>
    </div>
    """, unsafe_allow_html=True)