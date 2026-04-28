"""Pull Whole Foods conventional produce prices via an Apify Amazon scraper actor.

For each zip in zips.csv, runs the configured actor with all 21 search-term URLs
from items.csv (one actor call per zip), then picks the first non-organic result
that has a price. Outputs long + wide CSVs.

Set APIFY_API_TOKEN and APIFY_ACTOR_ID in .env. See README.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from apify_client import ApifyClient
from dotenv import load_dotenv

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "output"

# Patterns that indicate organic — user requirement is conventional only.
ORGANIC_TITLE_PATTERNS = [
    re.compile(r"\borganic\b", re.IGNORECASE),
    re.compile(r"\bOG\b"),  # WFM shorthand for Organic, e.g. "ONION RED OG"
]


def is_organic(item: dict) -> bool:
    title = item.get("title") or ""
    if any(p.search(title) for p in ORGANIC_TITLE_PATTERNS):
        return True
    for f in item.get("sustainabilityFeatures") or []:
        if "organic" in (f.get("title") or "").lower():
            return True
    return False


def get_price(item: dict) -> float | None:
    p = item.get("price") or {}
    return p.get("value") if isinstance(p, dict) else None


def get_list_price(item: dict) -> float | None:
    p = item.get("getPriceBeforeDiscount") or {}
    return p.get("value") if isinstance(p, dict) else None


def parse_word_list(s: str | None) -> list[str]:
    """Pipe-delimited list -> lowercased substring list. Empty -> [] (no constraint)."""
    if not s or not s.strip():
        return []
    return [w.strip().lower() for w in s.split("|") if w.strip()]


def _pat(word: str) -> str:
    """Word boundary match with optional plural suffix.

    'grape' matches 'grape' and 'grapes'.
    'tomato' matches 'tomato' and 'tomatoes'.
    'seed' does NOT match 'seedless' (word continues past 'seed').
    """
    return r"\b" + re.escape(word) + r"(?:s|es)?\b"


def title_passes_filters(title: str, require: list[str], exclude: list[str]) -> bool:
    t = (title or "").lower()
    for w in require:
        if not re.search(_pat(w), t):
            return False
    for w in exclude:
        if re.search(_pat(w), t):
            return False
    return True


def build_search_url(search_term: str) -> str:
    return f"https://www.amazon.com/s?k={urllib.parse.quote_plus(search_term)}"


def pick_conventional(
    results: list[dict],
    require: list[str],
    exclude: list[str],
) -> tuple[dict | None, str]:
    """Return (chosen_result, reason). reason explains why nothing matched, if applicable."""
    sorted_results = sorted(results, key=lambda x: x.get("position") or 999)
    seen = len(sorted_results)
    if seen == 0:
        return None, "no search results returned"
    organic_skipped = no_price_skipped = filter_skipped = 0
    for r in sorted_results:
        if is_organic(r):
            organic_skipped += 1
            continue
        if get_price(r) is None:
            no_price_skipped += 1
            continue
        if not title_passes_filters(r.get("title") or "", require, exclude):
            filter_skipped += 1
            continue
        return r, ""
    return None, (
        f"no match in top {seen}: organic={organic_skipped}, "
        f"no_price={no_price_skipped}, filter_excluded={filter_skipped}"
    )


def run_actor_for_zip(
    client: ApifyClient,
    actor_id: str,
    urls: list[str],
    zip_code: str,
    max_items: int,
) -> list[dict]:
    actor_input = {
        "categoryOrProductUrls": [{"url": u} for u in urls],
        "zipCode": zip_code,
        "maxItemsPerStartUrl": max_items,
        "maxSearchPagesPerStartUrl": 1,
        "locationDeliverableRoutes": ["SEARCH"],
        "scrapeProductDetails": False,
        "scrapeSellers": False,
        "scrapeProductVariantPrices": False,
        "maxOffers": 0,
        "useCaptchaSolver": False,
        "proxyCountry": "AUTO_SELECT_PROXY_COUNTRY",
    }
    run = client.actor(actor_id).call(run_input=actor_input)
    if run is None or "defaultDatasetId" not in run:
        raise RuntimeError(f"Actor run did not return a dataset for zip {zip_code}")
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def process_zip_results(
    items: list[dict],
    z: dict,
    actor_results: list[dict],
) -> list[dict]:
    by_url: dict[str, list[dict]] = {}
    for r in actor_results:
        u = r.get("input")
        if u:
            by_url.setdefault(u, []).append(r)

    rows = []
    for it in items:
        url = build_search_url(it["search_term"])
        url_results = by_url.get(url, [])
        require = parse_word_list(it.get("require_words"))
        exclude = parse_word_list(it.get("exclude_words"))
        chosen, reason = pick_conventional(url_results, require, exclude)
        base = {
            "market": z["market"],
            "zip": z["zip"],
            "comm": it["comm"],
            "upc": it["upc"],
            "item_desc": it["item_desc"],
            "search_term": it["search_term"],
        }
        if chosen is None:
            rows.append({**base,
                "matched_asin": None, "matched_title": None,
                "price": None, "list_price": None, "position": None,
                "availability": "Not available",
                "error": reason,
            })
        else:
            price = get_price(chosen)
            list_price = get_list_price(chosen)
            rows.append({**base,
                "matched_asin": chosen.get("asin"),
                "matched_title": chosen.get("title"),
                "price": round(price, 2) if price is not None else None,
                "list_price": round(list_price, 2) if list_price is not None else None,
                "position": chosen.get("position"),
                "availability": "Available",
                "error": None,
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-items", type=int, help="First N items only (testing)")
    ap.add_argument("--limit-zips", type=int, help="First N zips only (testing)")
    ap.add_argument("--workers", type=int, default=4, help="Concurrent actor runs (default 4)")
    ap.add_argument("--max-items", type=int, default=10, help="Top N search results per URL (default 10)")
    ap.add_argument("--save-raw", action="store_true", help="Save each zip's full dataset JSON")
    ap.add_argument("--dry-run", action="store_true", help="Print plan and exit")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    token = os.environ.get("APIFY_API_TOKEN")
    actor_id = os.environ.get("APIFY_ACTOR_ID")
    if not args.dry_run:
        if not token:
            print("ERROR: APIFY_API_TOKEN not set in .env", file=sys.stderr)
            return 2
        if not actor_id:
            print("ERROR: APIFY_ACTOR_ID not set in .env", file=sys.stderr)
            return 2

    with open(ROOT / "items.csv", newline="") as f:
        items = list(csv.DictReader(f))
    with open(ROOT / "zips.csv", newline="") as f:
        zips = list(csv.DictReader(f))
    if args.limit_items:
        items = items[: args.limit_items]
    if args.limit_zips:
        zips = zips[: args.limit_zips]

    urls = [build_search_url(it["search_term"]) for it in items]
    print(f"Plan: {len(zips)} actor runs × {len(items)} URLs each (top {args.max_items}/URL) = "
          f"~{len(zips) * len(items) * args.max_items} results")
    if args.dry_run:
        return 0

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = OUT_DIR / f"raw_apify_{stamp}"
    if args.save_raw:
        raw_dir.mkdir(exist_ok=True)

    client = ApifyClient(token)
    rows: list[dict] = []
    started = time.time()
    completed = 0

    def task(z: dict) -> list[dict]:
        try:
            results = run_actor_for_zip(client, actor_id, urls, z["zip"], args.max_items)
            if args.save_raw:
                (raw_dir / f"{z['zip']}.json").write_text(json.dumps(results, indent=2))
            return process_zip_results(items, z, results)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            return [{
                "market": z["market"], "zip": z["zip"],
                "comm": it["comm"], "upc": it["upc"],
                "item_desc": it["item_desc"], "search_term": it["search_term"],
                "matched_asin": None, "matched_title": None,
                "price": None, "list_price": None, "position": None,
                "availability": "Error",
                "error": err,
            } for it in items]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(task, z) for z in zips]
        for fut in as_completed(futures):
            zip_rows = fut.result()
            rows.extend(zip_rows)
            completed += 1
            print(f"  {completed}/{len(zips)} zips done ({time.time() - started:.1f}s)")

    df = pd.DataFrame(rows)
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
    matched = df[df["matched_asin"].notna()]
    errs = df[df["error"].notna()]
    print(f"Matched: {len(matched)}/{len(df)}    No-match/errors: {len(errs)}")
    if len(errs):
        print("Error breakdown (top reasons):")
        for reason, n in errs["error"].value_counts().head(5).items():
            print(f"  {n:3d}  {reason[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
