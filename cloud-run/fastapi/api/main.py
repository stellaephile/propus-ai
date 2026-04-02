"""
api/main.py
-----------
FastAPI REST layer for the Propus Delhi Transit Intelligence Agent.

Endpoints:
    POST /chat              — Send a message, get agent response + map data
    GET  /map/stress        — Urban stress choropleth GeoJSON
    GET  /map/stops         — All stops GeoJSON (bus + metro)
    GET  /health            — Health check

The agent is instantiated once at startup and reused across requests.
Sessions are keyed by session_id (passed in request body or auto-generated).

Usage:
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

Requires:
    pip install fastapi uvicorn pydantic python-dotenv
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Lazy import — agent module is heavy (GEE, sqlalchemy)
_runner = None
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SKIP_AGENT_ON_STARTUP = not DATABASE_URL


def get_runner():
    """Get or create the agent runner."""
    global _runner
    if _runner is None:
        if not DATABASE_URL:
            log.warning("⚠ DATABASE_URL not set — agent will not be available")
            return None
        try:
            from agent.agent import create_runner
            _runner = create_runner()
            log.info("✓ Agent initialized successfully")
        except Exception as e:
            log.error(f"✗ Failed to initialize agent: {e}")
            return None
    return _runner


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SKIP_AGENT_ON_STARTUP:
        log.info("Propus API starting up — initialising agent...")
        get_runner()
        log.info("Agent ready.")
    else:
        log.info("Propus API starting up (database-less mode)")
    yield
    log.info("Propus API shutting down.")


app = FastAPI(
    title="Propus Delhi Transit Intelligence API",
    version="1.0.0",
    description="ADK-powered transit equity agent for Delhi GTFS + PostGIS data",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., description="User's natural language question")
    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Session ID for conversation continuity",
    )
    user_id: str = Field(default="web_user", description="User identifier")


class ChatResponse(BaseModel):
    response: str
    session_id: str
    map_update: dict | None = Field(
        default=None,
        description="Optional map data to render (GeoJSON or stop list)",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check endpoint."""
    db_status = "✓ Connected" if DATABASE_URL else "⚠ No database configured"
    return {
        "status": "ok",
        "service": "propus-backend",
        "database": db_status,
        "version": "1.0.0"
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Send a message to the Delhi Transit Agent and receive a response.

    The agent may call PostGIS tools internally. If the response
    includes geographic data (stops, wards, routes), it is returned
    in map_update for the Streamlit frontend to render.
    """
    if not DATABASE_URL:
        log.warning("Database not configured — returning demo response")
        return ChatResponse(
            response="🔧 **Demo Mode**: Database not configured on this Cloud Run instance.\n\n"
                    "To enable full functionality:\n\n"
                    "1. Set up a PostgreSQL database with Delhi transit data\n"
                    "2. Update the environment variable:\n"
                    "```bash\n"
                    "gcloud run services update propus-backend \\\\\n"
                    "  --set-env-vars DATABASE_URL=postgresql://user:pass@host/db \\\\\n"
                    "  --region asia-south2\n"
                    "```\n\n"
                    "For now, the map features (Bus/Metro toggles) are working with mock data!",
            session_id=req.session_id,
            map_update=None
        )
    
    runner = get_runner()
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="Agent not available — database connection failed"
        )
    
    try:
        from agent.agent import run_query
        response_text = await run_query(
            query=req.message,
            session_id=req.session_id,
            user_id=req.user_id,
        )
    except Exception as exc:
        log.error(f"Agent error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    # Attempt to extract any embedded GeoJSON from the response
    map_update = _extract_map_data(response_text)

    return ChatResponse(
        response=response_text,
        session_id=req.session_id,
        map_update=map_update,
    )


@app.get("/map/stress")
async def map_stress():
    """
    Return GeoJSON FeatureCollection of all wards coloured by urban_stress_index.
    Used to render the default choropleth on app load.
    """
    if not DATABASE_URL:
        log.info("Database not configured — returning mock stress map")
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "ward_id": "1",
                        "urban_stress_index": 0.45,
                        "name": "Sample Ward"
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [77.0, 28.5],
                            [77.1, 28.5],
                            [77.1, 28.6],
                            [77.0, 28.6],
                            [77.0, 28.5]
                        ]]
                    }
                }
            ]
        }
    
    try:
        from mcp_server.server import get_urban_stress_map
        result = get_urban_stress_map()
        return json.loads(result)
    except Exception as exc:
        log.error(f"Map stress error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/map/stops")
async def map_stops(feed: str = "both", limit: int = 500):
    """
    Return GeoJSON FeatureCollection of bus and/or metro stops.

    Args:
        feed: 'bus', 'metro', or 'both'
        limit: Max stops to return per feed (default 500)
    """
    if not DATABASE_URL:
        # Return mock stops data
        log.info(f"Database not configured — returning mock {feed} stops")
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "stop_id": "BUS_1",
                        "stop_name": "Kasturba Nagar Bus Stop",
                        "feed": "bus"
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [77.2298, 28.5921]
                    }
                },
                {
                    "type": "Feature",
                    "properties": {
                        "stop_id": "METRO_1",
                        "stop_name": "Rajiv Chowk Metro Station",
                        "feed": "metro"
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [77.2200, 28.6328]
                    }
                }
            ]
        }
    
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(DATABASE_URL)

        queries = []
        if feed in ("bus", "both"):
            queries.append(f"""
                SELECT stop_id, stop_name, 'bus' AS feed, stop_lat, stop_lon
                FROM gtfs_bus.stops LIMIT {int(limit)}
            """)
        if feed in ("metro", "both"):
            queries.append(f"""
                SELECT stop_id, stop_name, 'metro' AS feed, stop_lat, stop_lon
                FROM gtfs_metro.stops LIMIT {int(limit)}
            """)

        features = []
        with engine.connect() as conn:
            for sql in queries:
                rows = conn.execute(text(sql)).fetchall()
                for r in rows:
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(r.stop_lon), float(r.stop_lat)],
                        },
                        "properties": {
                            "stop_id": r.stop_id,
                            "stop_name": r.stop_name,
                            "feed": r.feed,
                        },
                    })

        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        log.error(f"Error fetching stops: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _extract_map_data(text: str) -> dict | None:
    """
    Try to extract GeoJSON or coordinate data from agent response text.
    Returns a map_update dict if found, None otherwise.
    """
    # Look for embedded GeoJSON FeatureCollections
    if '"type": "FeatureCollection"' in text or '"type":"FeatureCollection"' in text:
        try:
            start = text.index('{"type"')
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass
    return None