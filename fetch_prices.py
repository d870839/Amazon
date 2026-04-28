"""Pull Whole Foods / Amazon Fresh prices for a list of ASINs across a list of zip codes.

Uses the Rainforest API (https://www.rainforestapi.com/). Set RAINFOREST_API_KEY in .env.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ENDPOINT = "https://api.rainforestapi.com/request"
ROOT = Path(__file__).parent
OUT_DIR = ROOT / "output"


@dataclass
class Row:
    market: str
    zip: str
    comm: str
    upc: str
    item_desc: str
    asin: str
    price: float | None
    currency: str | None
    unit_price: str | None
    availability: str | None
    title: str | None
    fulfillment: str | None
    error: str | None
    raw_json_path: str | None


@retry(
    retry=retry_if_exception_type((requests.RequestException,)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    reraise=True,
)
def fetch_one(api_key: str, asin: str, zipcode: str, timeout: int = 60) -> dict:
    params = {
        "api_key": api_key,
        "type": "product",
        "amazon_domain": "amazon.com",
        "asin": asin,
        "customer_zipcode": zipcode,
    }
    r = requests.get(ENDPOINT, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_response(payload: dict) -> dict:
    """Pull the fields we care about out of a Rainforest product response."""
    product = payload.get("product", {}) or {}
    buybox = product.get("buybox_winner", {}) or {}
    price_obj = buybox.get("price") or product.get("price") or {}
    avail = (buybox.get("availability") or {}).get("raw")
    fulfillment = None
    seller = buybox.get("seller") or {}
    if seller:
        fulfillment = seller.get("name")
    return {
        "price": price_obj.get("value") if isinstance(price_obj, dict) else None,
        "currency": price_obj.get("currency") if isinstance(price_obj, dict) else None,
        "unit_price": (buybox.get("unit_price") or {}).get("raw")
        if isinstance(buybox.get("unit_price"), dict)
        else None,
        "availability": avail,
        "title": product.get("title"),
        "fulfillment": fulfillment,
    }


def load_inputs() -> tuple[list[dict], list[dict]]:
    with open(ROOT / "items.csv", newline="") as f:
        items = list(csv.DictReader(f))
    with open(ROOT / "zips.csv", newline="") as f:
        zips = list(csv.DictReader(f))
    return items, zips


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-items", type=int, help="Only fetch first N items (for testing)")
    ap.add_argument("--limit-zips", type=int, help="Only fetch first N zips (for testing)")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent requests (default 8)")
    ap.add_argument("--save-raw", action="store_true", help="Save full JSON for each call")
    ap.add_argument("--dry-run", action="store_true", help="Print plan and exit")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("RAINFOREST_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: RAINFOREST_API_KEY not set. Copy .env.example to .env and add your key.", file=sys.stderr)
        return 2

    items, zips = load_inputs()
    if args.limit_items:
        items = items[: args.limit_items]
    if args.limit_zips:
        zips = zips[: args.limit_zips]

    pairs = [(it, z) for it in items for z in zips]
    print(f"Plan: {len(items)} items x {len(zips)} zips = {len(pairs)} requests")
    if args.dry_run:
        return 0

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = OUT_DIR / f"raw_{stamp}"
    if args.save_raw:
        raw_dir.mkdir(exist_ok=True)

    rows: list[Row] = []
    completed = 0
    started = time.time()

    def task(item: dict, z: dict) -> Row:
        try:
            payload = fetch_one(api_key, item["asin"], z["zip"])
            parsed = parse_response(payload)
            raw_path = None
            if args.save_raw:
                fname = f"{item['asin']}_{z['zip']}.json"
                p = raw_dir / fname
                p.write_text(json.dumps(payload, indent=2))
                raw_path = str(p.relative_to(ROOT))
            return Row(
                market=z["market"], zip=z["zip"], comm=item["comm"], upc=item["upc"],
                item_desc=item["item_desc"], asin=item["asin"],
                price=parsed["price"], currency=parsed["currency"],
                unit_price=parsed["unit_price"], availability=parsed["availability"],
                title=parsed["title"], fulfillment=parsed["fulfillment"],
                error=None, raw_json_path=raw_path,
            )
        except Exception as e:
            return Row(
                market=z["market"], zip=z["zip"], comm=item["comm"], upc=item["upc"],
                item_desc=item["item_desc"], asin=item["asin"],
                price=None, currency=None, unit_price=None, availability=None,
                title=None, fulfillment=None,
                error=f"{type(e).__name__}: {e}", raw_json_path=None,
            )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(task, it, z) for it, z in pairs]
        for fut in as_completed(futures):
            row = fut.result()
            rows.append(row)
            completed += 1
            if completed % 25 == 0 or completed == len(pairs):
                elapsed = time.time() - started
                print(f"  {completed}/{len(pairs)} done ({elapsed:.1f}s)")

    long_path = OUT_DIR / f"prices_long_{stamp}.csv"
    df = pd.DataFrame([r.__dict__ for r in rows])
    df.to_csv(long_path, index=False)

    wide = df.pivot_table(
        index=["comm", "upc", "asin", "item_desc"],
        columns="market",
        values="price",
        aggfunc="first",
    )
    wide_path = OUT_DIR / f"prices_wide_{stamp}.csv"
    wide.to_csv(wide_path)

    errs = df[df["error"].notna()]
    print(f"\nSaved: {long_path.relative_to(ROOT)}")
    print(f"Saved: {wide_path.relative_to(ROOT)}")
    print(f"Success: {len(df) - len(errs)} / {len(df)}    Errors: {len(errs)}")
    if len(errs):
        print("First 5 errors:")
        for _, r in errs.head().iterrows():
            print(f"  {r['asin']} @ {r['zip']}: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
