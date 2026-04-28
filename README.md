# Whole Foods conventional price puller

Pulls **conventional** Whole Foods Market produce prices for a list of items
across a list of US zip codes, using the [Rainforest API](https://www.rainforestapi.com/).

## Why search-by-name (not fetch-by-ASIN)

The Rainforest `type=product` endpoint returns no price for Whole Foods produce
ASINs — they're flagged as "add-on items" and prices are only revealed inside a
qualifying Fresh/WFM cart. The `type=search` endpoint, however, returns parsed
prices in the search results page when `customer_zipcode` is set. This script
uses search-by-name and picks the best Whole Foods conventional match.

## Inputs

- `items.csv` — `comm`, `upc`, `item_desc`, **`search_term`**, `notes` (21 rows)
- `zips.csv` — `market`, `zip` (24 rows)

Total: **504 API calls per run**. At Rainforest's $0.003/call, ~$1.50/run.

## Match rules

For each search response, the script picks the **first** result that:

1. Title does **NOT** contain `"organic"` (user requirement: conventional only)
2. Has a price
3. Is tagged in this priority order:
   - `is_whole_foods_market: true`
   - `is_amazon_fresh: true`
   - delivery tagline includes a same-day grocery window (`Today HH(AM|PM)`)

If no result satisfies these rules, the row is recorded with `match_source=none`
and you can iterate on the search term.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit and paste your Rainforest API key
```

## Usage

**Smoke test** (3 items × 2 zips = 6 calls, ~$0.02):

```bash
python fetch_prices.py --limit-items 3 --limit-zips 2 --save-raw
```

Open the CSV in `output/` and check:
- Are the `matched_title` strings what you expect?
- Is `match_source` mostly `wfm` (good) vs `fresh`/`grocery` (acceptable) vs `none` (needs search-term tuning)?
- Do prices vary between the two zips?

**Full run:**

```bash
python fetch_prices.py
```

## Outputs

Written to `output/`:
- `prices_long_<timestamp>.csv` — one row per (item, zip) with full match details
- `prices_wide_<timestamp>.csv` — pivot, items × markets, prices only

## Editing the search terms

If a row comes back as `match_source=none` or pulls a wrong product, edit the
`search_term` column in `items.csv` and re-run. The current mappings include
two user-specific overrides:
- `TOMATOES HYDROPONIC` → `beefsteak tomato`
- `TOMATOES BUNCHED` → `tomato on the vine`

## Caveats

1. **Unit mismatch**: Kroger may price per bag, WFM per lb. Use the `unit_price`
   column to normalize before comparing.
2. **Conventional availability**: Whole Foods online skews heavily organic, so
   a few items may legitimately have no conventional WFM listing in some zips.
   The fallback to Amazon Fresh (also conventional grocery delivery) catches
   most of these.
3. **Substitution risk**: The first WFM match isn't always the *exact* item
   (e.g. "Vidalia onion" might match a generic sweet onion). Inspect the
   `matched_title` column on the smoke test before committing to a full run.
