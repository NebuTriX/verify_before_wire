"""Public datasets for evaluation and load testing. Needs a Kaggle account:
put your API token in ~/.kaggle/kaggle.json (or KAGGLE_USERNAME / KAGGLE_KEY).

  python scripts/datasets.py enron  --limit 500   # real corporate emails -> demo/eval/legit/
  python scripts/datasets.py paysim --vendors 300 # synthetic transfers  -> load-test CSVs

What each dataset is honestly good for:
- Enron (wcukierski/enron-email-dataset): ~500k real emails from a real company. We keep
  messages that talk about invoices, payments or wires and use them as LEGITIMATE examples to
  measure false alarms. (A few may be fraud-adjacent; this is Enron. Review before relying on it.)
- PaySim (ealaxi/paysim1): a simulation of mobile-money transfers. NOT B2B vendor payments and
  NOT fraud we detect; used only to create realistic volumes of payment history for load tests.
There is no public dataset of labelled business-email-compromise payment fraud. Fraud examples
come from demo/eval/fraud/ (synthetic, hand-written) and, later, from customers' shadow-mode cases."""
from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from email import policy
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
PAYMENT_WORDS = re.compile(r"\b(invoice|payment|wire transfer|remit|bank account|pay(?:able)?)\b", re.I)


def kaggle_download(slug: str, dest: Path) -> None:
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
    except ImportError:
        sys.exit("pip install kaggle  (and add your Kaggle API token)")
    api = KaggleApi()
    api.authenticate()
    dest.mkdir(parents=True, exist_ok=True)
    api.dataset_download_files(slug, path=str(dest), unzip=True)


def enron(limit: int) -> None:
    src = RAW / "enron"
    if not (src / "emails.csv").exists():
        kaggle_download("wcukierski/enron-email-dataset", src)
    out = ROOT / "demo" / "eval" / "legit"
    out.mkdir(parents=True, exist_ok=True)
    csv.field_size_limit(10_000_000)
    kept = 0
    with open(src / "emails.csv", newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            msg = Parser(policy=policy.default).parsestr(row["message"])
            body = msg.get_body(preferencelist=("plain",))
            text = body.get_content() if body else ""
            if len(text) < 200 or not PAYMENT_WORDS.search(text) or "-----Original Message" in text:
                continue
            (out / f"enron_{kept:05d}.eml").write_bytes(msg.as_bytes())
            kept += 1
            if kept >= limit:
                break
    print(f"wrote {kept} legitimate payment-related emails to {out}")


def paysim(vendors: int) -> None:
    src = RAW / "paysim"
    files = list(src.glob("*.csv"))
    if not files:
        kaggle_download("ealaxi/paysim1", src)
        files = list(src.glob("*.csv"))
    rnd = random.Random(7)
    by_dest: dict[str, list[dict]] = {}
    with open(files[0], newline="") as f:
        for r in csv.DictReader(f):
            if r["type"] == "TRANSFER" and r["isFraud"] == "0":
                by_dest.setdefault(r["nameDest"], []).append(r)
    picked = rnd.sample([k for k, v in by_dest.items() if len(v) >= 3], vendors)
    out = ROOT / "data" / "loadtest"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "vendors.csv", "w", newline="") as fv, open(out / "payments.csv", "w", newline="") as fp:
        from app.onboarding import PAYMENT_COLUMNS, VENDOR_COLUMNS
        wv, wp = csv.writer(fv), csv.writer(fp)
        wv.writerow(VENDOR_COLUMNS)
        wp.writerow(PAYMENT_COLUMNS)
        n = 0
        for i, dest in enumerate(picked):
            vid, acct = f"ps-{i:04d}", f"{rnd.randrange(10**9, 10**10)}"
            wv.writerow([vid, f"Loadtest Vendor {i:04d} Pte Ltd", "", "SG", f"vendor{i:04d}.example.com",
                         "DBS Bank Ltd", acct, "", "DBSSSGSG", "SG", "2024-01-01", "Accounts", "+65 6000 0000",
                         "SGD", "30"])
            for r in by_dest[dest]:
                day = 1 + int(r["step"]) // 24        # PaySim steps are hours over ~30 days
                wp.writerow([f"{vid}-{n}", vid, f"INV-{n:07d}", r["amount"], "SGD",
                             f"2026-{1 + (day // 28) % 9:02d}-{1 + day % 28:02d}", acct])
                n += 1
    print(f"wrote {vendors} vendors and {n} payments to {out} (synthetic, for load testing only)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", choices=["enron", "paysim"])
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--vendors", type=int, default=300)
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    enron(a.limit) if a.dataset == "enron" else paysim(a.vendors)
