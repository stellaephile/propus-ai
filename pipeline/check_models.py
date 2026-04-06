# check_models.py
# Run this once: python check_models.py
# Uses the same env vars your agent already reads (GOOGLE_CLOUD_PROJECT etc.)

import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

# Picks up GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
# from your .env — same as the agent
client = genai.Client(
    vertexai=True,
    project=os.environ["GCP_PROJECT_ID"],
    location=os.environ.get("CLOUD_RUN_REGION", "asia-south2"),
)

print(f"Project : {os.environ['GCP_PROJECT_ID']}")
print(f"Location: {os.environ.get('CLOUD_RUN_REGION', 'us-central1')}")
print()

# We care about models that can generateContent (i.e. usable as agent backbone)
# and are Gemini models (skip embedding, imagen, etc.)
WANT = {"generateContent"}

rows = []
for model in client.models.list():
    actions = set(getattr(model, "supported_actions", []) or [])
    if not actions.issuperset(WANT):
        continue
    name = model.name  # e.g. "models/gemini-2.5-flash"
    short = name.split("/")[-1]
    if not short.startswith("gemini"):
        continue
    rows.append({
        "name":       short,
        "display":    getattr(model, "display_name", ""),
        "input_tok":  getattr(model, "input_token_limit", "?"),
        "output_tok": getattr(model, "output_token_limit", "?"),
    })

rows.sort(key=lambda r: r["name"])

print(f"{'Model':<35} {'Display name':<30} {'Input tokens':>14} {'Output tokens':>14}")
print("-" * 97)
for r in rows:
    print(f"{r['name']:<35} {r['display']:<30} {str(r['input_tok']):>14} {str(r['output_tok']):>14}")

print()
print("# Paste the model names you want into MODEL_CHAIN in agent.py")
print("# Recommended order: fastest/cheapest → highest quota → most capable")