# Enrichment Pipeline

The enrichment pipeline (`src/enrich_transactions.py`) transforms raw bank statement CSVs into a single enriched dataset with merchant names, categories, AI descriptions, and Google Places business data. It runs either from the CLI or automatically in the background when the UI detects new statement files.

```
data/statements/**/*.csv  →  [enrichment pipeline]  →  data/enriched_transactions.csv
```

---

## Overview

The pipeline processes transactions through **7 sequential phases**:

| # | Phase | API | What it does |
|---|-------|-----|-------------|
| 1 | **Load** | — | Read all CSVs from `data/statements/`, deduplicate, exclude trashed |
| 2 | **Pre-process** | Gemini | Classify each description → `purchase_mode` + `merchant_name` |
| 3 | **Google Places** | Places API | Look up real business info for in-person merchants |
| 4 | **AI Descriptions** | Gemini | Generate human-readable description per transaction |
| 5 | **Categorize** | Gemini | Assign a spending category + emoji to each transaction |
| 6 | **Summary Descriptions** | Gemini | Generate short descriptions for each category and merchant |
| 7 | **Write CSV** | — | Write the final `enriched_transactions.csv` |

Every phase is **incremental**: results from previous runs are cached, and only new/uncached items hit the APIs.

---

## Phase 1 — Load Transactions

Reads every `.csv` file under `data/statements/` recursively. Three formats are supported:

- **Credit CSVs** (from `pdf_parser.py`): header starts with `date,description,amount,cashback_pct`
- **Parsed debit PDFs** (from `pdf_parser.py`): header starts with `date,description,amount,account_type`
- **Raw Desjardins debit exports**: headerless, 14-column layout with institution/transit/account/date/description/withdrawal/deposit fields

Each row becomes a dict with: `source_file`, `account_type` (credit/debit), `date`, `description`, `amount`, `cashback_pct`.

**Deduplication**: Rows with identical `(account_type, date, description, amount)` across files are collapsed to one.

**Trash exclusion**: Transactions the user has trashed (stored in `data/trash.json`) are filtered out by their `(date, amount, description, source_file)` key.

---

## Phase 2 — Pre-process (Gemini)

**Function**: `classify_all()`

Sends all unique, uncached descriptions to Gemini in batches of 80. For each description, Gemini returns:

- `purchase_mode`: one of `online`, `in_person`, `transfer`, `income`, `fee`, `other`
- `merchant_name`: a clean merchant name guess (e.g. `"STARBUCKS #1234 MONTREAL QC"` → `"Starbucks"`)

**System prompt**:
> You are a bank-statement transaction classifier. Given a JSON array of raw transaction descriptions, return a JSON array in the EXACT SAME ORDER with one object per description containing `purchase_mode` and `merchant_name`.

**Caching**: Descriptions that exist in the previous `enriched_transactions.csv` output are loaded and reused — only genuinely new descriptions hit Gemini.

**User corrections**: Descriptions found in `data/category_corrections.json` are pre-resolved with their user-assigned category and skip this Gemini call entirely.

**Error handling**: If Gemini returns a count mismatch (different number of results than inputs), the batch is split in half and each half is retried recursively until individual items are isolated.

---

## Phase 3 — Google Places Lookup

**Function**: `enrich_with_places()`

For every description classified as `in_person`, queries the [Google Places Text Search API](https://developers.google.com/maps/documentation/places/web-service/text-search) to find the real business.

**Query**: `"{merchant_name} {city}"` (default city: `Montreal, QC`)

**Location bias**: A rectangular region from south of Montréal (45.3°N, 74.3°W) to north of Québec City (47.1°N, 70.8°W).

**Fields extracted** from the first result:

| Field | Source |
|-------|--------|
| `place_name` | `displayName.text` |
| `place_address` | `formattedAddress` |
| `place_phone` | `nationalPhoneNumber` |
| `place_website` | `websiteUri` |
| `place_rating` | `rating` |
| `place_google_maps_url` | `googleMapsUri` |
| `place_types` | `types` (joined with `\|`) |

**Rate limiting**: 0.5 seconds between requests.

**Caching**: Results are persisted to `data/places_cache.json`. On subsequent runs, cached merchants are skipped entirely (0 API calls for fully cached data).

---

## Phase 4 — AI Descriptions (Gemini)

**Function**: `_enrich_post_places()`

Generates a short (max 100 characters), human-readable description for each transaction by sending the raw description, merchant name, purchase mode, and Google Places data to Gemini.

**Example outputs**:
- `"Coffee at Starbucks on Ste-Catherine St"`
- `"Monthly Netflix subscription"`
- `"Interac transfer to John Smith"`
- `"Golf game at Albatros Golf Club"`

**System prompt**:
> You are a bank transaction enricher. [...] Use the Places data (place_types, place_name) when available — they are very reliable.

The prompt is augmented with:
- Up to 40 user category corrections as few-shot examples
- Up to 30 user notes for specific transactions

**Caching**: Descriptions that already have an `ai_description` from a previous run are skipped.

---

## Phase 5 — Categorize (Gemini)

**Function**: `_categorize_transactions()`

The final categorization pass. Sends each transaction's full enriched context (raw description, merchant name, purchase mode, Places types, Places name, AI description) to Gemini and asks it to pick the best category.

**Category list**: Gemini is given a constrained list of known categories to **strongly prefer**:

```
Amazon Purchase, Apple Services, Streaming, Software/SaaS, Rent, Ride-sharing,
Food Delivery, Groceries, Food & Dining, Pharmacy, Health & Medical, Dentist/Medical,
Clothing & Fashion, Electronics, Entertainment, Transport, Automotive,
Travel & Accommodation, Utilities, Hydro, Telecom & Internet, Payroll / Income,
Interac Transfer, Bill Payment, Investment, Insurance, Bank Fee, ATM, Government,
Education, Charity, Sports & Fitness, Golf, Ski, Pets, Cat, Personal Care,
Bars/Festivals/Alcohol, Weed, e-Commerce / Online Shopping, Other
```

This list can be customized by the user via `data/custom_categories.json` (managed from the UI's **Edit > Edit Category List** dialog). When that file exists, it replaces the hardcoded defaults entirely.

**System prompt key instruction**:
> STRONGLY prefer one of these existing categories: [...] Parse through ALL the categories above before choosing. Pick the one that fits the transaction BEST. [...] If NONE is a good fit, you MAY invent a new short category name. "Other" is the ABSOLUTE LAST RESORT.

Gemini also returns a single emoji per category, which is saved to `data/category_emojis.json`.

**User overrides**: Descriptions in `category_corrections.json` get their user-defined category immediately and skip this Gemini call. Up to 40 corrections are also injected into the prompt as few-shot examples to teach Gemini the user's preferences.

---

## Phase 6 — Summary Descriptions (Gemini)

**Function**: `_generate_summary_descriptions()`

Generates short (max 8 words) summary descriptions used in the UI's summary table tooltips:

- **Category descriptions**: e.g. `"Coffee shops, fast food, and restaurants"` for Food & Dining
- **Merchant descriptions**: e.g. `"Canadian coffee chain"` for Starbucks

Each item is sent with up to 15 (categories) or 10 (merchants) sample transaction descriptions as context.

**Caching**: Only categories/merchants missing from the existing `data/summary_descriptions.json` are requested. The file is merged (never overwritten from scratch) so failed batches don't destroy previously good data.

---

## Phase 7 — Write CSV

Writes `data/enriched_transactions.csv` with these columns:

| Column | Source |
|--------|--------|
| `source_file` | Original CSV filename |
| `account_type` | `credit` or `debit` |
| `date` | Transaction date |
| `description` | Raw bank description |
| `amount` | Transaction amount |
| `cashback_pct` | Credit card cashback % (credit only) |
| `purchase_mode` | From Phase 2: `online`, `in_person`, `transfer`, etc. |
| `gemini_merchant` | From Phase 2: clean merchant name |
| `gemini_category` | From Phase 5: spending category |
| `place_name` | From Phase 3: Google Places business name |
| `place_address` | From Phase 3: formatted address |
| `place_phone` | From Phase 3: phone number |
| `place_website` | From Phase 3: website URL |
| `place_rating` | From Phase 3: Google rating |
| `place_google_maps_url` | From Phase 3: Maps link |
| `place_types` | From Phase 3: Google place types |
| `ai_description` | From Phase 4: human-readable description |
| `user_notes` | User-added notes (from `data/user_notes.json`) |

---

## Caching & Incremental Runs

The pipeline is designed to be **re-run cheaply**. On a typical incremental run (a few new statements added), most phases complete with 0 API calls:

| Cache file | What it stores | Cleared by re-enrich? |
|-----------|---------------|----------------------|
| `enriched_transactions.csv` | All enriched data from previous run (classifications, places, descriptions) | **No** — preserved during re-enrich so data doesn't disappear |
| `data/places_cache.json` | Google Places results keyed by description | **Yes** |
| `data/summary_descriptions.json` | Category + merchant descriptions | **Yes** |
| `data/category_emojis.json` | Category → emoji map | **Yes** |
| `data/txn_descriptions_cache.json` | Transaction description cache | **Yes** |
| `data/ai_analysis_cache.json` | AI analysis cache | **Yes** |

**Preserved during re-enrich** (user data):
- `data/category_corrections.json` — manual category overrides
- `data/user_notes.json` — user-added notes
- `data/trash.json` — trashed transactions
- `data/official_income_sources.json` — income source configuration
- `data/custom_categories.json` — user-defined category list

---

## Gemini Configuration

| Setting | Value |
|---------|-------|
| Model | `gemini-2.5-flash` |
| Batch size | 80 descriptions per API call |
| Max retries | 3 (with exponential backoff) |
| Rate limit handling | Parses `429` errors, extracts retry delay from error message |
| Error recovery | On count mismatch, recursively splits batch in half |

---

## Selective Recategorization

When the user deletes categories via **Edit > Edit Category List**, only the transactions belonging to deleted categories are recategorized. This is handled by `recategorize_descriptions()`:

1. Loads existing enrichment data from the CSV
2. Clears the `category` field for the targeted descriptions
3. Runs only Phase 5 (categorization) with the updated category list
4. Rewrites the CSV with new categories

---

## Running from CLI

```bash
# Default: Montreal, QC
python -m src.enrich_transactions

# Custom city
python -m src.enrich_transactions --city "Quebec City, QC"

# Custom statements directory
python -m src.enrich_transactions --statements-dir data/statements
```

---

## Running from the UI

- **Automatic**: When new CSVs are dropped into `data/statements/credit/` or `data/statements/debit/`, enrichment starts automatically after 30 seconds of quiet.
- **Manual**: **File > Re-enrich from Scratch** (Ctrl+Shift+R) clears caches and re-runs the full pipeline.
- **On startup**: If any statement CSV is newer than `enriched_transactions.csv`, enrichment runs immediately.

Progress is shown in the status bar: `🔄 [1:23] Categorizing: Batch 2/12 (80 items)`.
