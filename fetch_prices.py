"""Pull Whole Foods conventional produce prices via Rainforest API search.

For each (item, zip) pair: search by name + zip, then pick the first result that:
  1. Title does NOT contain "organic" (user constraint: conventional only)
  2. Has a price
  3. Is tagged Whole Foods Market (preferred), then Amazon Fresh, then any
     same-day grocery delivery match.

Set RAINFOREST_API_KEY in .env. See README.md for details.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ENDPOINT = "https://api.rainforestapi.com/request"
ROOT = Path(__file__).parent
OUT_DIR = ROOT / "output"

# Same-day grocery delivery taglines look like "$12.99 delivery Today 2PM - 6PM".
GROCERY_DELIVERY_RE = re.compile(r"\bToday\b.*\d{1,2}(AM|PM)", re.IGNORECASE)


@dataclass
class Row:
    market: str
    zip: str
    comm: str
    upc: str
    item_desc: str
    search_term: str
    matched_asin: str | None
    matched_title: str | None
    price: float | None
    currency: str | None
    unit_price: str | None
    delivery_tagline: str | None
    match_source: str | None  # wfm | fresh | grocery | none
    excluded_organic_count: int
    error: str | None


@retry(
    retry=retry_if_exception_type((requests.RequestException,)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    reraise=True,
)
def search_amazon(api_key: str, search_term: str, zipcode: str, verify_ssl: bool = True, timeout: int = 60) -> dict:
    params = {
        "api_key": api_key,
        "type": "search",
        "amazon_domain": "amazon.com",
        "search_term": search_term,
        "customer_zipcode": zipcode,
        "output": "json",
    }
    r = requests.get(ENDPOINT, params=params, timeout=timeout, verify=verify_ssl)
    r.raise_for_status()
    return r.json()


def is_grocery_delivery(result: dict) -> bool:
    tagline = ((result.get("delivery") or {}).get("tagline")) or ""
    return bool(GROCERY_DELIVERY_RE.search(tagline))


def pick_match(results: list[dict]) -> tuple[dict | None, str, int]:
    """Return (chosen_result, match_source, organic_count_skipped)."""
    organic_skipped = 0
    candidates = []
    for r in results:
        title = (r.get("title") or "").lower()
        if "organic" in title:
            organic_skipped += 1
            continue
        price = ((r.get("price") or {}).get("value")) if isinstance(r.get("price"), dict) else None
        if price is None:
            continue
        candidates.append(r)

    for r in candidates:
        if r.get("is_whole_foods_market"):
            return r, "wfm", organic_skipped
    for r in candidates:
        if r.get("is_amazon_fresh"):
            return r, "fresh", organic_skipped
    for r in candidates:
        if is_grocery_delivery(r):
            return r, "grocery", organic_skipped
    return None, "none", organic_skipped


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
    ap.add_argument("--save-raw", action="store_true", help="Save full search JSON for each call")
    ap.add_argument("--dry-run", action="store_true", help="Print plan and exit")
    ap.add_argument("--no-verify-ssl", action="store_true",
                    help="Disable SSL verification (use only on corp networks where pip-system-certs didn't help)")
    args = ap.parse_args()

    if args.no_verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print("WARNING: SSL verification disabled. Only do this on a trusted corporate network.")

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
    print(f"Plan: {len(items)} items x {len(zips)} zips = {len(pairs)} requests (~${len(pairs) * 0.003:.2f} at $0.003/req)")
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
            payload = search_amazon(api_key, item["search_term"], z["zip"], verify_ssl=not args.no_verify_ssl)
            if args.save_raw:
                fname = f"{item['comm']}_{item['upc']}_{z['zip']}.json"
                (raw_dir / fname).write_text(json.dumps(payload, indent=2))
            results = payload.get("search_results") or []
            chosen, source, organic_n = pick_match(results)
            if chosen is None:
                return Row(
                    market=z["market"], zip=z["zip"], comm=item["comm"], upc=item["upc"],
                    item_desc=item["item_desc"], search_term=item["search_term"],
                    matched_asin=None, matched_title=None,
                    price=None, currency=None, unit_price=None,
                    delivery_tagline=None, match_source="none",
                    excluded_organic_count=organic_n, error=None,
                )
            price_obj = chosen.get("price") or {}
            return Row(
                market=z["market"], zip=z["zip"], comm=item["comm"], upc=item["upc"],
                item_desc=item["item_desc"], search_term=item["search_term"],
                matched_asin=chosen.get("asin"),
                matched_title=chosen.get("title"),
                price=price_obj.get("value"),
                currency=price_obj.get("currency"),
                unit_price=chosen.get("unit_price"),
                delivery_tagline=(chosen.get("delivery") or {}).get("tagline"),
                match_source=source,
                excluded_organic_count=organic_n,
                error=None,
            )
        except Exception as e:
            return Row(
                market=z["market"], zip=z["zip"], comm=item["comm"], upc=item["upc"],
                item_desc=item["item_desc"], search_term=item["search_term"],
                matched_asin=None, matched_title=None,
                price=None, currency=None, unit_price=None,
                delivery_tagline=None, match_source=None,
                excluded_organic_count=0,
                error=f"{type(e).__name__}: {e}",
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

    df = pd.DataFrame([r.__dict__ for r in rows])
    long_path = OUT_DIR / f"prices_long_{stamp}.csv"
    df.to_csv(long_path, index=False)

    wide = df.pivot_table(
        index=["comm", "upc", "item_desc", "search_term"],
        columns="market",
        values="price",
        aggfunc="first",
    )
    wide_path = OUT_DIR / f"prices_wide_{stamp}.csv"
    wide.to_csv(wide_path)

    print(f"\nSaved: {long_path.relative_to(ROOT)}")
    print(f"Saved: {wide_path.relative_to(ROOT)}")
    src_counts = df["match_source"].value_counts(dropna=False).to_dict()
    print(f"Match sources: {src_counts}")
    errs = df[df["error"].notna()]
    if len(errs):
        print(f"Errors: {len(errs)}. First 5:")
        for _, r in errs.head().iterrows():
            print(f"  {r['search_term']} @ {r['zip']}: {r['error']}")
    no_match = df[(df["match_source"] == "none") & df["error"].isna()]
    if len(no_match):
        print(f"No-match (no conventional WFM/Fresh/grocery result): {len(no_match)}. Items affected:")
        for it in no_match["item_desc"].unique():
            zips_aff = no_match[no_match["item_desc"] == it]["zip"].tolist()
            print(f"  {it}: {len(zips_aff)} zips ({zips_aff[:3]}{'...' if len(zips_aff) > 3 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
