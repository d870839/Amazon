"""Quick check: which of the zips in zips.csv does Rainforest actually accept?

Sends one cheap search per zip ("yellow onion"), reports which return 200 vs 400.
Usage:
    python validate_zips.py            # ~24 credits
    python validate_zips.py --no-verify-ssl
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
ENDPOINT = "https://api.rainforestapi.com/request"


def check_zip(api_key: str, zipcode: str, verify_ssl: bool = True) -> tuple[str, int, str]:
    params = {
        "api_key": api_key,
        "type": "search",
        "amazon_domain": "amazon.com",
        "search_term": "yellow onion",
        "customer_zipcode": zipcode,
        "output": "json",
    }
    try:
        r = requests.get(ENDPOINT, params=params, timeout=60, verify=verify_ssl)
        body = r.text[:200] if not r.ok else ""
        return zipcode, r.status_code, body
    except Exception as e:
        return zipcode, -1, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify-ssl", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.no_verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("RAINFOREST_API_KEY")
    if not api_key:
        print("ERROR: RAINFOREST_API_KEY not set.", file=sys.stderr)
        return 2

    with open(ROOT / "zips.csv", newline="") as f:
        zips = list(csv.DictReader(f))
    print(f"Checking {len(zips)} zips ({len(zips)} credits, ~${len(zips) * 0.003:.2f})...")

    results: dict[str, tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_zip, api_key, z["zip"], not args.no_verify_ssl): z for z in zips}
        for fut in as_completed(futures):
            z, status, body = fut.result()
            results[z] = (status, body)

    ok, bad = [], []
    for z in zips:
        status, body = results[z["zip"]]
        marker = "OK " if status == 200 else "BAD"
        line = f"  {marker}  {z['zip']}  {z['market']:30s}  status={status}"
        if body:
            line += f"  body={body[:120]}"
        print(line)
        (ok if status == 200 else bad).append(z)

    print(f"\nValid zips: {len(ok)}/{len(zips)}")
    if bad:
        print(f"Invalid zips ({len(bad)}):")
        for z in bad:
            print(f"  - {z['zip']} ({z['market']})")
        print("\nConsider removing these from zips.csv or substituting nearby zips that have Fresh/WFM delivery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
