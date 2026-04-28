# Whole Foods price puller

Pulls Whole Foods / Amazon Fresh prices for a list of ASINs across a list of US zip codes,
using the [Rainforest API](https://www.rainforestapi.com/).

## Inputs

- `items.csv` — 21 ASINs (mapped from Kroger UPCs / PLU codes)
- `zips.csv` — 24 zip codes, one per market

Total: **504 API calls per run**. At Rainforest's standard rate (~$0.003/call) that's ~$1.50/run.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste your Rainforest API key
```

## Usage

**Smoke test first** (1 item × 2 zips = 2 calls, ~$0.01):

```bash
python fetch_prices.py --limit-items 1 --limit-zips 2 --save-raw
```

Inspect `output/raw_*/` to confirm Whole Foods pricing is actually returned (not generic Amazon.com).
Some ASINs may need the `--save-raw` JSON inspected to confirm the buybox is the WFM seller.

**Full run:**

```bash
python fetch_prices.py
```

## Outputs

Written to `output/`:

- `prices_long_<timestamp>.csv` — one row per (item, zip), with price, unit_price, availability, fulfillment, error
- `prices_wide_<timestamp>.csv` — pivot with items as rows and markets as columns

## Known caveats

1. **Unit mismatch**: Kroger may price per bag, WFM per lb. Compare via `unit_price` column.
2. **KRO-branded items** (e.g. `KRO CUTPEELED BBY CARROT`) have no direct WFM equivalent — the ASINs in `items.csv` are best-effort mappings; verify against the response `title`.
3. **Out-of-stock zips**: produce availability varies. Check the `availability` and `error` columns.
4. **Buybox vs WFM-only**: if an ASIN is also sold by 3rd-party sellers, Rainforest may return their price. Use the `fulfillment` column to filter to Amazon/Whole Foods.
