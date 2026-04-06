"""
pipeline/embed.py — rate-limit aware version for free tier (100 req/min)
"""

import os, time
from dotenv import load_dotenv
load_dotenv("/Users/sonalgan/propus-ai/.env")

from google import genai
from google.genai import types
from sqlalchemy import create_engine, text

client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
engine = create_engine(os.environ["DATABASE_URL"])

# Free tier: 100 requests/min → 1 request per 0.6s to stay safe
# Batch of 10 = 10 requests per batch → pause 6s between batches
BATCH = 10
PAUSE = 6.5   # seconds between batches (~9 batches/min = 90 req/min, safely under 100)

def embed_batch(texts: list[str]) -> list[list[float]]:
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=texts,
        config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
    )
    return [e.values for e in result.embeddings]

with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT stop_id, stop_name, 'bus' AS feed FROM gtfs_bus.stops
        UNION ALL
        SELECT stop_id, stop_name, 'metro' AS feed FROM gtfs_metro.stops
    """)).fetchall()
    total = len(rows)
    print(f"Total stops: {total}")
    print(f"Batches of {BATCH} with {PAUSE}s pause → ~{60//PAUSE:.0f} batches/min")
    eta_min = (total / BATCH * PAUSE) / 60
    print(f"ETA: ~{eta_min:.0f} minutes\n")

    inserted = 0
    for i in range(0, total, BATCH):
        batch = rows[i:i+BATCH]
        contexts = [f"{r.stop_name} [{r.feed} stop, Delhi]" for r in batch]

        # Retry loop with backoff
        for attempt in range(4):
            try:
                vectors = embed_batch(contexts)
                break
            except Exception as e:
                wait = (attempt + 1) * 15
                print(f"  Attempt {attempt+1} failed: {e}\n  Waiting {wait}s...")
                time.sleep(wait)
        else:
            print(f"  Skipping batch {i}–{i+BATCH} after 4 failures")
            continue

        conn.execute(text("""
            INSERT INTO embeddings.stop_embeddings
                (stop_id, stop_name, feed, context, embedding)
            VALUES (:stop_id, :stop_name, :feed, :context, :embedding)
            ON CONFLICT (stop_id) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    context   = EXCLUDED.context
        """), [
            {
                "stop_id":   r.stop_id,
                "stop_name": r.stop_name,
                "feed":      r.feed,
                "context":   ctx,
                "embedding": str(vec),
            }
            for r, ctx, vec in zip(batch, contexts, vectors)
        ])
        conn.commit()
        inserted += len(batch)

        # Progress every 100 stops
        if inserted % 100 == 0 or inserted == total:
            pct = inserted / total * 100
            print(f"  {inserted}/{total} ({pct:.0f}%) embedded...")

        time.sleep(PAUSE)

    print(f"\nDone! {inserted} stops embedded.")
    r = conn.execute(text("SELECT COUNT(*) FROM embeddings.stop_embeddings"))
    print(f"Rows in table: {r.fetchone()[0]}")
