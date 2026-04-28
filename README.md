# Whole Foods conventional price puller (Apify edition)

Pulls **conventional** Whole Foods Market produce prices for a list of items
across a list of US zip codes, using an [Apify](https://apify.com/) Amazon
scraper actor. One actor run per zip, all items in one run.

## Inputs

- `items.csv` — `comm`, `upc`, `item_desc`, `search_term`, `notes` (21 rows)
- `zips.csv` — `market`, `zip` (24 rows)

## How it picks the right product

For each search response, the script picks the first result that:

1. Title does NOT contain `"organic"` or `" OG"` (WFM shorthand for organic)
2. `sustainabilityFeatures` does NOT include `"Organic content"` certification
3. `price.value` is not null (null = WFM-only add-on item with no scrapeable price)

Lower `position` (search rank) wins. If nothing matches, the row records "No
conventional match" and you can iterate on the search term.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
# or: source .venv/bin/activate                    # macOS/Linux
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and set:

- `APIFY_API_TOKEN` — get this from your Apify account → Settings → Integrations → API tokens
- `APIFY_ACTOR_ID` — the actor's ID in `username~actor-name` form (find it in the actor's URL on apify.com console)

The actor must accept this input shape (most Amazon scrapers on Apify do):
```json
{
  "categoryOrProductUrls": [{"url": "..."}],
  "zipCode": "30349",
  "maxItemsPerStartUrl": 10,
  "maxSearchPagesPerStartUrl": 1,
  "locationDeliverableRoutes": ["SEARCH"],
  "scrapeProductDetails": false,
  "proxyCountry": "AUTO_SELECT_PROXY_COUNTRY"
}
```
And return records with at least: `title`, `asin`, `price.value`, `input`,
`position`. (Optional: `sustainabilityFeatures`, `getPriceBeforeDiscount`.)

## Usage

**Smoke test** (3 items × 2 zips, ~$0.10):

```bash
python fetch_prices.py --limit-items 3 --limit-zips 2 --save-raw
```

Open `output/prices_long_*.csv` and check that `matched_title` looks right
and that prices differ across zips.

**Full run** (24 zips × 21 items, ~$3-5):

```bash
python fetch_prices.py
```

## Outputs

Written to `output/`:
- `prices_long_<timestamp>.csv` — one row per (item, zip): matched ASIN, title, price, list_price, position, error
- `prices_wide_<timestamp>.csv` — pivot, items × markets, prices only
- `raw_apify_<timestamp>/<zip>.json` (with `--save-raw`) — full dataset per zip for debugging

## Caveats

1. **Unit mismatch**: Kroger may price per bag, WFM per item or per lb. Use
   `list_price` and `matched_title` columns to spot mismatches.
2. **Substitution risk**: For ambiguous searches (e.g. `vidalia onion` → "Sweet
   Onion"), inspect `matched_title` on the first run. Tweak `search_term` in
   `items.csv` if a better term gets a cleaner match.
3. **No conventional listing**: A few items in some zips may have no
   conventional match in the top 10. The row will be flagged "No conventional
   match" — increase `--max-items` to 20 or refine the search term.
4. **Actor schema drift**: Apify actors evolve. If the picked actor changes
   field names (e.g. `title` → `productTitle`), update the field accessors at
   the top of `fetch_prices.py`.
