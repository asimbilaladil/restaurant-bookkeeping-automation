# Revel → R365 Daily Sales Reconciliation

Fetches daily sales data from the Revel Operations Report API and automatically fills the Journal Entry tab in Restaurant365 (R365) Daily Sales Summary for all 11 LCF establishments.

> 📘 **For browser-automation internals** — exact selectors, the Kendo grid + add-row interaction sequences, how attachments are uploaded, and how Revel establishment switching works — see **[TECHNICAL.md](TECHNICAL.md)**.

---

## How It Works — End to End

```
User picks a date in the UI
        │
        ▼
POST /api/fetch
  └─ Logs into Revel (headless browser, session cached)
  └─ For each establishment:
        POST /navigation/load_establishment_tree/   ← switches active location
        GET  /reports/operations/json/              ← fetches the day's data
  └─ Streams progress back to UI via Server-Sent Events
        │
        ▼
User reviews the fetched values, then clicks "Open in R365"
        │
        ▼
POST /api/r365/navigate  (or /api/r365/reconcile-all for all at once)
  └─ Opens headless Chromium with persistent R365 login profile
  └─ Navigates to Daily Sales Summary → filters by date → clicks location row
  └─ Opens the Journal Entry tab
  └─ Reads current R365 values, compares to Revel, writes only where different
  └─ Attaches the source Revel xlsx to the DSS form
  └─ Saves the DSS form
  └─ Returns before/after screenshots + balance difference
```

---

## Project Structure

```
app/
├── server.py                    # Flask API + web server
│
├── revel/
│   ├── establishments.py        # Location list — single source of truth
│   ├── session.py               # Revel login / session caching
│   └── operations.py            # Operations Report fetcher
│
├── r365/
│   ├── session.py               # R365 login / persistent browser profile
│   └── journal_entry.py         # DSS navigation, JE form fill
│
├── dashboard.html               # Landing page  (/)
├── index.html                   # Daily Sales Reconciliation UI  (/daily-sales-reconciliation)
├── login.html                   # Login page
├── DSS_Reconciliation.xlsx      # Manual verification workbook
└── .env                         # Credentials (not committed)
```

---

## Establishments

| Revel ID | Revel Name | R365 DSS Name |
|---|---|---|
| 32 | LCF Airtex | LCF Airtex |
| 14 | LCF Beaumont | LCF Beaumont |
| 48 | LCF Downtown Houston | LCF Downtown *(override)* |
| 7 | LCF Ella | LCF Garden Oaks *(override)* |
| 6 | LCF Katy | LCF Katy |
| 25 | LCF Mission Bend | LCF Mission Bend |
| 36 | LCF Missouri City | LCF Missouri City |
| 26 | LCF Nederland | LCF Nederland |
| 20 | LCF Pasadena | LCF Fairmont *(override)* |
| 40 | LCF Rosenberg | LCF Rosenberg |
| 15 | LCF Shepherd | LCF Shepherd |

> Three locations have different names in R365's DSS grid vs. Revel. The overrides are defined in `revel/establishments.py → R365_NAME_OVERRIDES` and applied automatically before navigating R365.

---

## Revel Data Sources

### API Request

```
POST  /navigation/load_establishment_tree/
      form: { establishments, establishment, node_type=1, node_id, location=/reports/operations/ }
      ← must be called first to switch the session to the correct location

GET   /reports/operations/json/
      params:
        establishment   = <id>
        range_from      = MM/DD/YYYY 05:00:00  (the target date at 5 AM)
        range_to        = MM/DD/YYYY 05:00:00  (next day at 5 AM)
        show_unpaid     = 1
        show_irregular  = 1
```

The response is a JSON object with four top-level keys used by this tool:

```
sales_data        — flat dict of aggregated totals for the day
product_mix_data  — array of rows, one per product class / item
tax_data          — array of tax rows, includes a totals_row
discounts_data    — array of discount rows grouped into Standard and Loyalty
```

---

## Field Calculations: Revel → R365

Each R365 Journal Entry account is mapped to one or more Revel JSON fields. The table below shows the exact calculation for every field, what it's called in R365, and whether we write it or R365 auto-fills it.

### CREDITS — Revenue & Liabilities

| R365 Account | D/C | Calculation | Revel Source |
|---|---|---|---|
| **4000-01 Food Sales** | Credit | `product_mix_data["1. Food"].taxable_sales` + `product_mix_data["1. Food"].untaxable_sales` | `product_mix_data` — row where `row_type == "Class"` and `product_class == "1. Food"` |
| **4000-011 Food Sales-Tax Exempt** *(optional row, added only when needed)* | Debit | `abs(sales_data.net_sales_untaxed)` — added as a new JE row with comment "Untaxed Net Sales". Also adds this amount on top of Food Sales. | `sales_data.net_sales_untaxed` |
| **4000-02 Beverage Sales** | Credit | `product_mix_data["2. Beverage"].taxable_sales` | `product_mix_data` — row where `product_class == "2. Beverage"` |
| **4000-08 Food Delivery Sales** | Credit | `product_mix_data["5. Delivery - Food"].taxable_sales` + `product_mix_data["5. Delivery - Food"].untaxable_sales` | `product_mix_data` — row where `product_class == "5. Delivery - Food"` |
| **4000-07 Other Sales** | Credit | Sum of `taxable_sales` + `untaxable_sales` across the `Unknown Class`, `4. Other Sales`, **and** `Extra Items` classes (R365's native import combines them, e.g. 10.69 + 4.99 = 15.68). Omitting any of these leaves the JE short by that amount, which surfaces as an unbalanced Cash Over/Short plug. | `product_mix_data` — rows where `product_class` is `"Unknown Class"`, `"4. Other Sales"`, or `"Extra Items"` |
| **2240-000 Sales Tax Payable** | Credit | `tax_data[totals_row].tax` | `tax_data` — first row where `row_type == "totals_row"` |
| **70250 Credit Card Fees** | Credit | `sales_data.adj_credit_tips` | `sales_data` |
| **2301 Employee Tips Payable** | Debit | `sales_data.adj_credit_tips` | `sales_data` |

---

### DEBITS — Assets & Expenses

| R365 Account | D/C | Calculation | Revel Source |
|---|---|---|---|
| **1200-000 A/R Credit Cards Receivable** | Debit | `sales_data.credit_total` + `sales_data.credit_tips_total` | `sales_data` |
| **1245-12 A/R-UberEats** | Debit | Sum of all `custom_payments` entries where name contains `"uber eats"` | `sales_data.custom_payments` |
| **1245-03 A/R-DoorDash** | Debit | Sum of all `custom_payments` entries where name contains `"door dash"`, `"doordash"`, or `"dd marketplace"` | `sales_data.custom_payments` |
| **1245-08 A/R-GrubHub** | Debit | Sum of all `custom_payments` entries where name contains `"grub hub"` or `"grubhub"` | `sales_data.custom_payments` |
| **1255 Undeposited Funds** | Debit | `sales_data.cash_for_sales` | `sales_data` |
| **4500-02 Comps** | Debit | Sum of all non-total `discounts_data` rows where `reason == "Manager 100%"` | `discounts_data` |
| **5000-17 Employee Discount** | Debit | Sum of all non-total `discounts_data` rows where `reason` starts with `"employee"` | `discounts_data` |
| **4500-01 Discounts** | Debit | `Standard group total − employee_discount − comps − app_reward`. Written as the **remainder** so any unknown Standard discount reasons roll into this bucket automatically. | `discounts_data` — is_total row for `"Standard"` minus named buckets |
| **4500-03 Promotions** | Debit | `discounts_data` is_total row for `"Loyalty"`. R365 pre-fills this from platform data — we only write if there's a mismatch. | `discounts_data` — is_total row for `"Loyalty"` |
| **8000-06 Cash Over/Short** | Debit *or* Credit | `abs(sales_data.net_account_for − sales_data.total_payments)`. Direction: **Debit** if shortage (collected less than expected), **Credit** if overage (collected more). Added as a new row with comment "Variance". | `sales_data.net_account_for` and `sales_data.total_payments` |

---

## Discount Mapping Detail

Discounts are the most complex field because Revel groups them into two parent groups ("Standard" and "Loyalty") and one or more named reasons per group. The mapping rules are:

```
discounts_data
├── Standard group (is_total row: reason = "Standard")
│   ├── "Employee $9.79 Off", "Employee 50%", "Employee …"  → 5000-17 Employee Discount
│   ├── "Manager 100%"                                        → 4500-02 Comps
│   ├── "App Reward"                                          → tracked only, R365 pre-fills
│   ├── "Remake …"                                            → skipped (manual entries)
│   └── everything else (Police/Fire, Senior, DSP, meals…)  → 4500-01 Discounts (remainder)
│
└── Loyalty group (is_total row: reason = "Loyalty")
    └── all items regardless of reason name                  → 4500-03 Promotions
```

**Why remainder for 4500-01?**
Instead of enumerating every possible discount reason (which changes), we compute:
```
4500-01 = Standard_total − employee_discount − comps − app_reward
```
This means new discount types added in Revel automatically land in 4500-01 without any code changes.

**Write strategy:**
1. Write `4500-02 Comps` and `5000-17 Employee Discount` first, then wait for R365 to settle.
2. Read back *all* R365 discount rows (4500-01, 4500-02, 4500-03, 4500-04, 5000-17).
3. Sum them and compare to Revel total (Standard + Loyalty).
4. If variance → write the adjusted remainder to the plain (no-comment) `4500-01` row only.

---

## R365 Write Strategy

The tool does **not** blindly overwrite every field. For each account it:

1. **Reads** the current R365 value
2. **Compares** it to the Revel-calculated value
3. **Writes** only if there is a mismatch

This means running the tool twice on the same day is safe — it will detect the existing values and skip writes if already correct.

**Special cases:**
- `4500-03 Promotions` and marketplace A/R rows (UberEats, DoorDash, GrubHub) are pre-filled by R365 from platform integrations. We verify them against Revel and correct if wrong.
- `4000-011 Food Sales-Tax Exempt` and `8000-06 Cash Over/Short` are **new rows** that don't exist in the default JE template. They are added via the "Select Account" input at the bottom of the grid when needed.
- `App Reward` rows are read and tracked for the discount reconciliation calculation but never written — R365 owns that row.
- The second `2301 Employee Tips Payable` row (auto-filled, read-only) is never touched.

---

## Attaching the Source xlsx

Before saving, the automation attaches the original Revel Operations `.xlsx` to the DSS form so the journal entry carries its source document. This is handled by `_upload_attachment()` in `r365/journal_entry.py`, and R365's Angular form makes it surprisingly fragile:

- **Two inputs share `id="attachmentsModuleInput"`** — a "Ribbon Logo" uploader (`<r365-amazon-uploader>`) and the real DSS attachments input. We resolve the one that is **not** inside `<r365-amazon-uploader>` to a single element handle, used for upload + verification so they can't diverge.
- **The uploader rejects untrusted events.** Setting the file via `set_input_files` delivers it to R365's change handler but the file is silently dropped (it never reaches the S3 uploader). Instead we open the **native file chooser** (`expect_file_chooser` + click) so the browser fires a *trusted* `change` event the uploader accepts; `set_input_files` remains a fallback.
- **The uploader lives on the Angular scope, not `window`.** Success is confirmed by polling `scope.AWS_S3_Uploader.files` / `scope.uploadedAttachments` (and the DOM) for the filename.
- `attachment_status` is reported as `uploaded`, `already_present`, `failed`, or `skipped`, and is logged per run.

> If R365 changes their form again and uploads start failing, the per-run log includes a `Scope uploader state: …` dump on failure (uploader status, files, `uploadedAttachments`) to diagnose the new structure.

---

## R365 Journal Entry — Full Field Map

This is what the completed Journal Entry looks like after the automation runs:

```
Account                              Debit          Credit    Comment
─────────────────────────────────────────────────────────────────────────
4000-01 - Food Sales                               4,633.75
4000-011 - Food Sales-Tax Exempt                            Untaxed Net Sales   (only if applicable)
4000-02 - Beverage Sales                             412.50
4000-08 - Food Delivery Sales                        609.32
2240-000 - Sales Tax Payable                         536.72
70250 - Credit Card Fees                               0.74
─────────────────────────────────────────────────────────────────────────
1200-000 - A/R Credit Cards Receivable 5,310.94
1245-12 - A/R-UberEats                   115.93
1245-03 - A/R-DoorDash                   494.39
1245-08 - A/R-GrubHub                      0.00
1255 - Undeposited Funds               1,148.82
4500-02 - Comps                            3.59
5000-17 - Employee Discount               61.13
4500-01 - Discounts                       12.00
4500-03 - Promotions                      87.18   (R365 pre-fills; corrected if wrong)
2301 - Employee Tips Payable               0.74
8000-06 - Cash Over/Short                  5.50   Variance   (debit = shortage)
─────────────────────────────────────────────────────────────────────────
TOTAL                                  7,240.22   7,240.22
Difference                                 0.00 ✓ Balanced
```

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Create `.env`

```env
REVEL_USER=your_revel_username
REVEL_PASS=your_revel_password
R65_USER=your_r365_email
R65_PASS=your_r365_password
R365_URL=https://ayg.restaurant365.com
FLASK_SECRET_KEY=a-long-random-secret-key
LOGIN_USERNAME=your_app_username
LOGIN_PASSWORD_HASH=werkzeug_hash_of_your_password
```

Generate the password hash:
```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
```

### 3. Run the server

```bash
source venv/bin/activate
python3 server.py
```

Open **http://localhost:5050**, log in, and click **Daily Sales Reconciliation**.

> **Note:** Flask runs as a long-lived background process. Any change to `server.py` or the `revel/` / `r365/` modules requires killing and restarting the server to take effect.

---

## Usage

1. Log in at **http://localhost:5050**
2. Go to **Daily Sales Reconciliation**
3. Pick a **Start Date** (defaults to yesterday)
4. Click **Fetch Reports** — fetches Revel data for all 11 establishments in parallel, streams progress cards as each completes
5. Review the fetched totals on each card
6. Click **Open in R365** on a card to fill that location's Journal Entry, or use **Reconcile All** to run all sequentially
7. Before/after screenshots and a balance check appear for each location

---

## API Reference

### `POST /api/fetch`
Fetch Revel Operations Reports. Streams Server-Sent Events.

**Body:**
```json
{ "start_date": "2026-06-02", "establishments": [32, 14] }
```

**SSE events:** `progress` per establishment → `done` with full results array

---

### `POST /api/r365/navigate`
Fill the Journal Entry for a single establishment.

**Body:**
```json
{
  "date": "2026-06-02",
  "location_name": "LCF Airtex",
  "revel_data": { ...raw data object from fetch result... }
}
```

**Response:**
```json
{
  "status": "ok",
  "je_difference": 0.00,
  "je_balanced": true,
  "before_screenshot_url": "/screenshots/r365_je_LCF_Airtex_2026-06-02_before.png",
  "screenshot_url":        "/screenshots/r365_je_LCF_Airtex_2026-06-02_after.png"
}
```

---

### `POST /api/r365/reconcile-all`
Fill Journal Entries for multiple establishments sequentially. Streams SSE events.

**Body:**
```json
{
  "date": "2026-06-02",
  "establishments": [
    { "id": 32, "name": "LCF Airtex",    "data": { ... } },
    { "id": 14, "name": "LCF Beaumont",  "data": { ... } }
  ]
}
```

**SSE events:** `r365_progress` (status: `running` / `success` / `error`) per entity → `r365_done`

---

### `GET /api/establishments`
Returns all establishment IDs and display names.

---

### `GET /api/time-saved`
Powers the "time saved this month" widget on the Daily Sales Reconciliation page. Returns the count of **distinct (establishment, date) reconciliations that succeeded** since the 1st of the current calendar month (retries / auto-loop reruns of the same location+date count once).

**Response:**
```json
{ "successes": 73, "since": "2026-07-01", "month": "2026-07" }
```

The UI multiplies `successes` by its human-vs-tool per-reconciliation minute assumptions (tool ~1.2 min; fast person 5–10 min; average person 15–20 min) to render the hours-saved figure.

---

## Sessions & Auth

| System | Mechanism |
|---|---|
| App | Flask session cookie. Credentials in `.env` (`LOGIN_USERNAME` / `LOGIN_PASSWORD_HASH`). |
| Revel | Playwright browser session cached at `/tmp/revel_session.json`. Auto re-login if expired. |
| R365 | Persistent Chromium profile at `~/.r365_browser_profile`. Stays logged in across server restarts. |

---

## CLI (without the web server)

```bash
# Fetch Revel reports and print JSON
python -m revel.operations --date 2026-06-02
python -m revel.operations --date 2026-06-02 --establishments 32,14
python -m revel.operations --date 2026-06-02 --output results.json

# Open R365 and fill JE for the first DSS row matching the date
python -m r365.journal_entry --date 2026-06-02
```

---

## Debugging

`debug_je.py` inspects the live R365 JE grid DOM — useful when a new R365 deployment changes the grid structure.

```bash
python3 debug_je.py   # edit TARGET_DATE and LOCATION at top of file first
```

Screenshots saved to `/tmp/`:

| File | What it shows |
|---|---|
| `/tmp/r365_dss_filtered.png` | DSS list after date filter applied |
| `/tmp/r365_entity.png` | Entity form after clicking a row |
| `/tmp/r365_je_LCF_*_before.png` | JE grid before any writes |
| `/tmp/r365_je_LCF_*_after.png` | Full JE table rendered as HTML and screenshotted |

---

## Reconciliation Workbook

`DSS_Reconciliation.xlsx` — one tab per establishment for manual cross-check.

- Enter date, Revel values, and R365 values in the yellow input cells
- Variance column auto-calculates — red on mismatch, green when zero
