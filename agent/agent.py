"""
agent/agent.py
--------------
Google ADK agent for Delhi Transit Intelligence (Propus / Quiver).

Connects to the Propus MCP server (mcp_server/server.py) which exposes
all 10 transit tools backed by real PostGIS data.

The agent uses Gemini 2.0 Flash as the reasoning model and is configured
with a detailed system prompt that understands the Delhi transit data schema,
the WRI equity framing, and how to combine RS-derived metrics with GTFS data.

Usage:
    # Interactive terminal session
    python agent/agent.py

    # Single query (useful for testing)
    python agent/agent.py --query "Which wards in Delhi have no metro access?"

    # As a module (imported by api/main.py)
    from agent.agent import create_runner, run_query

Requires:
    pip install google-adk google-generativeai python-dotenv

Environment variables (.env):
    GOOGLE_API_KEY        Gemini API key
    DATABASE_URL          PostgreSQL connection (used by MCP server)
    MCP_SERVER_SCRIPT     Path to mcp_server/server.py
                          (default: mcp_server/server.py relative to this file)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
MCP_SERVER_SCRIPT = os.environ.get(
    "MCP_SERVER_SCRIPT",
    str(PROJECT_ROOT / "mcp_server" / "server.py"),
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the Delhi Transit Intelligence Agent for Propus/Quiver,
a WRI India transit equity analysis platform.

You have access to real Delhi GTFS data loaded into PostGIS:
- 10,559 bus stops across 2,403 routes (DTC bus network)
- 262 metro stops across 36 metro lines (DMRC)
- 290 Delhi ward polygons with computed transit metrics
- WorldPop 2020 population data (when loaded): stops per 10,000 residents
- Sentinel-2 NDVI/NDBI indices (when loaded): green cover and built-up density

KEY DATA FACTS:
- Stop IDs are prefixed: bus stops = 'bus_<number>', metro stops = 'metro_<number>'
- Ward IDs follow the shapefile: e.g. 'CANT_1', 'ward_fallback_000'
- Transit scores range 0–1 (higher = better served)
- Urban stress index > 0.7 = highest priority for intervention
- The 23 wards with zero bus stops are: Sangam Vihar cluster, Tigri, Raj Nagar,
  Prem Nagar, Sagarpur, Said Ul Ajaib — these are the most underserved

EQUITY FRAMING (WRI standard):
- Raw stop counts are misleading — always prefer stops_per_10k (population-weighted)
- A ward with 50 stops but 500,000 residents is less served than one with 20 stops
  and 50,000 residents
- Combine transit_score + urban_stress_index for the most meaningful equity ranking

HOW TO ANSWER:
- For "nearby stops" queries → use get_nearby_stops with lat/lon
- For "underserved areas" → use find_underserved_areas with stops_per_10k metric
- For "tell me about [ward]" → use get_ward_rs_profile
- For route questions → use get_route_coverage
- For metro station coverage → use get_metro_catchment
- For comparing two areas → use compare_transit_access
- For place name searches → use semantic_stop_search
- For the full Delhi map → use get_urban_stress_map

Always cite data sources and note when RS metrics (NDVI, NDBI, pop_total) are
not yet loaded (returns NULL). Be specific about distances and counts.
Format numbers clearly: populations with commas, scores to 4 decimal places.
"""

# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def create_agent():
    """
    Create and return the ADK Agent connected to the Propus MCP server.

    The MCP server is launched as a subprocess via stdio transport —
    no separate server process needed for local development.
    """
    from google.adk.agents import Agent
    from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

    log.info(f"Connecting to MCP server: {MCP_SERVER_SCRIPT}")

    mcp_toolset = MCPToolset(
        connection_params=StdioServerParameters(
            command="python",
            args=[MCP_SERVER_SCRIPT],
            env={
                **os.environ,
                "DATABASE_URL": os.environ["DATABASE_URL"],
            },
        )
    )

    agent = Agent(
        name="delhi_transit_agent",
        model="gemini-2.5-flash",
        description=(
            "Delhi transit equity intelligence agent. Answers questions about "
            "bus/metro coverage, underserved wards, route details, and population-weighted "
            "transit access using real GTFS + PostGIS data."
        ),
        instruction=SYSTEM_PROMPT,
        tools=[mcp_toolset],
    )

    log.info("Agent created successfully")
    return agent


def create_runner(agent=None):
    """Create an ADK Runner with in-memory session for the agent."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    if agent is None:
        agent = create_agent()

    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="propus_transit",
        session_service=session_service,
    )
    return runner


# ---------------------------------------------------------------------------
# Query helper (used by FastAPI and interactive mode)
# ---------------------------------------------------------------------------

async def run_query(query: str, session_id: str = "default",
                    user_id: str = "user") -> str:
    """
    Run a single query through the agent and return the text response.

    Args:
        query: Natural language question about Delhi transit.
        session_id: Session ID for conversation continuity.
        user_id: User identifier.

    Returns:
        Agent's text response as a string.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    runner = create_runner()

    # Ensure session exists
    await runner.session_service.create_session(
        app_name="propus_transit",
        user_id=user_id,
        session_id=session_id,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text=query)],
    )

    response_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response():
            if event.content and event.content.parts:
                response_text = event.content.parts[0].text
            break

    return response_text


# ---------------------------------------------------------------------------
# Interactive terminal session
# ---------------------------------------------------------------------------

async def _interactive_session():
    """Run an interactive chat session in the terminal."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    print("\n" + "=" * 60)
    print("  Propus — Delhi Transit Intelligence Agent")
    print("  Type 'exit' or Ctrl-C to quit")
    print("=" * 60 + "\n")

    runner = create_runner()
    session_id = "terminal_session"
    user_id = "terminal_user"

    await runner.session_service.create_session(
        app_name="propus_transit",
        user_id=user_id,
        session_id=session_id,
    )

    # Suggested opening queries
    print("Example queries:")
    print("  • Which wards in Delhi have the worst transit access?")
    print("  • How many buses serve Kashmere Gate?")
    print("  • Find bus stops near Connaught Place (lat 28.6315, lon 77.2167)")
    print("  • Compare transit access between Sangam Vihar and Kashmere Gate")
    print("  • Which bus stops are farthest from any metro station?\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if user_input.lower() in {"exit", "quit", "q"}:
            print("Goodbye.")
            break

        if not user_input:
            continue

        print("Agent: ", end="", flush=True)

        content = types.Content(
            role="user",
            parts=[types.Part(text=user_input)],
        )

        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    print(event.content.parts[0].text)
            # Show tool calls in real time
            elif hasattr(event, "tool_call") and event.tool_call:
                tool_name = getattr(event.tool_call, "name", "tool")
                print(f"\n  [calling {tool_name}...]", end="", flush=True)

        print()  # blank line between turns


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delhi Transit Intelligence Agent")
    parser.add_argument(
        "--query", "-q",
        type=str,
        default=None,
        help="Single query to run (non-interactive mode)",
    )
    args = parser.parse_args()

    if args.query:
        async def _single():
            response = await run_query(args.query)
            print(f"\nAgent: {response}\n")
        asyncio.run(_single())
    else:
        asyncio.run(_interactive_session())