"""Loads seed_data.json into Firestore (vendors + payments). Idempotent.

    GOOGLE_CLOUD_PROJECT=my-project python scripts/seed_firestore.py
"""
import json
import os
from pathlib import Path

from google.cloud import firestore

data = json.loads((Path(__file__).parent / "seed_data.json").read_text())
db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                      database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
batch, n = db.batch(), 0
for coll, key in (("vendors", "vendors"), ("payments", "payments")):
    for doc in data[key]:
        batch.set(db.collection(coll).document(doc["id"]), doc)
        n += 1
        if n % 400 == 0:
            batch.commit(); batch = db.batch()
batch.commit()
print(f"seeded {len(data['vendors'])} vendors and {len(data['payments'])} payments")
