# Revel → R365 Journal Entry Automation

Fetches daily sales data from Revel Operations Reports and automatically fills the Journal Entry tab in Restaurant365 (R365) Daily Sales Summary for all 11 LCF establishments.

---

## What It Does

1. Logs into Revel and fetches the Operations Report JSON for each establishment
2. Extracts all mapped field values (sales, tax, tips, marketplace payments, discounts)
3. Opens R365 in a headless browser, navigates to the Daily Sales Summary for the matching location and date
4. Clicks the Journal Entry tab and fills all confirmed mapped fields

---

## Project Structure

```
app/
├── server.py                    # Flask API + web server (main entry point)
│
├── revel/                       # Revel package
│   ├── __init__.py              # Re-exports: fetch_reports, DEFAULT_ESTABLISHMENTS, ESTABLISHMENT_NAMES
│   ├── establishments.py        # ESTABLISHMENTS list — single source of truth for all locations
│   ├── session.py               # Revel login, session caching, ensure_logged_in
│   └── operations.py            # Operations Report fetcher + CLI
│
├── r365/                        # R365 package
│   ├── __init__.py              # Re-exports: open_r365_journal_entry
│   ├── session.py               # R365 login, persistent browser profile, ensure_logged_in_r365
│   └── journal_entry.py         # DSS navigation, JE form fill, open_r365_journal_entry + CLI
│
├── debug_je.py                  # Debug tool — inspects R365 JE grid DOM structure
│
├── dashboard.html               # Landing page (served at /)
├── index.html                   # Daily Sales Reconciliation UI (served at /daily-sales-reconciliation)
├── login.html                   # Login page
│
├── DSS_Reconciliation.xlsx      # Manual reconciliation workbook (11 location tabs)
├── requirements.txt             # Python dependencies
└── .env                         # Credentials (not committed)
```

### Adding a new location
Edit `revel/establishments.py` — one line in the `ESTABLISHMENTS` list. `DEFAULT_ESTABLISHMENTS` and `ESTABLISHMENT_NAMES` are derived from it automatically.

### Adding a new Revel report
Add a new file under `revel/` (e.g. `revel/labor.py`) and import session helpers from `revel.session`.

### Adding a new R365 automation
Add a new file under `r365/` (e.g. `r365/invoices.py`) and import login helpers from `r365.session`.

---

## Setup

### 1. Requirements

- Python 3.11+

### 2. Create virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Create `.env` file

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

To generate `LOGIN_PASSWORD_HASH`:
```bash
source venv/bin/activate
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
```

### 4. Run the server

```bash
source venv/bin/activate
python3 server.py
```

Then open **http://localhost:5050** in your browser and log in.

---

## Routes

| Route | Description |
|---|---|
| `GET /` | Dashboard (login required) |
| `GET /daily-sales-reconciliation` | Reconciliation tool UI |
| `GET /login` | Login page |
| `GET /logout` | Clears session, redirects to login |
| `GET /api/establishments` | Returns all establishment IDs and names |
| `POST /api/fetch` | Fetches Revel reports (SSE stream) |
| `POST /api/r365/navigate` | Opens R365 and fills one JE |
| `POST /api/r365/reconcile-all` | Fills JEs for all establishments (SSE stream) |

---

## Usage

1. Log in at **http://localhost:5050**
2. Navigate to **Daily Sales Reconciliation** from the dashboard
3. Pick a **Start Date** in the UI
4. Click **Fetch Reports** — fetches Revel data for all 11 establishments
5. Once done, click **Open in R365** on any establishment card to fill its Journal Entry

---

## Establishments

| Revel ID | Location Name |
|---|---|
| 32 | LCF Airtex |
| 14 | LCF Beaumont |
| 48 | LCF Downtown Houston |
| 7 | LCF Ella |
| 6 | LCF Katy |
| 25 | LCF Mission Bend |
| 36 | LCF Missouri City |
| 26 | LCF Nederland |
| 20 | LCF Pasadena |
| 40 | LCF Rosenberg |
| 15 | LCF Shepherd |

---

## Field Mappings (Revel → R365)

All mappings are derived from live DOM inspection of the R365 Journal Entry grid and cross-verified against Revel Operations Report JSON.

### Credits (R365 Credit column)

| R365 Account | Revel JSON Field | Status |
|---|---|---|
| 4000-01 - Food Sales | `product_mix_data[1. Food].price` | ✅ Confirmed |
| 4000-02 - Beverage Sales | `product_mix_data[2. Beverage].price` | ✅ Confirmed |
| 4000-08 - Food Delivery Sales | `product_mix_data[5. Delivery - Food].price` | ✅ Confirmed |
| 2240-000 - Sales Tax Payable | `tax_data[totals_row].tax` | ✅ Confirmed |
| 70250 - Credit Card Fees | `sales_data.adj_total` | ✅ Confirmed |

### Debits (R365 Debit column)

| R365 Account | Revel JSON Field | Status |
|---|---|---|
| 1200-000 - A/R Credit Cards Receivable | `credit_total + credit_tips_total − credit_refunds` | ✅ Confirmed |
| 1245-12 - A/R-UberEats | `custom_payments[Uber Eats].total` | ✅ Confirmed |
| 1245-03 - A/R-DoorDash | `custom_payments[Door Dash + DD Marketplace].total` (summed) | ✅ Confirmed |
| 1245-08 - A/R-GrubHub | `custom_payments[Grub Hub].total` | ✅ Confirmed |
| 2301 - Employee Tips Payable (editable) | `sales_data.adj_total` | ✅ Confirmed |
| 4500-02 - Comps | `discounts_data[Manager 100%]` | ✅ Confirmed |
| 5000-17 - Employee Discount | `discounts_data[Employee $9.79 Off]` | ✅ Confirmed |
| 4500-01 - Discounts | `standard_total + loyalty_total − employee_discount − comps − promotions − app_reward` | ✅ Confirmed |
| 4500-03 - Promotions | `discounts_data[Free … meal reasons] + loyalty_total` | ✅ Confirmed |

### Read-only (auto-filled by R365, not touched)

| R365 Account | Notes |
|---|---|
| 1255 - Undeposited Funds | Auto-calculated from cash |
| 8000-06 - Cash Over/Short | Auto-calculated from variance |
| 2301 - Employee Tips Payable (2nd row) | Auto-calculated from tips_total |
| App Reward row | Tracked internally but not written — R365 pre-fills this line |

**Discount mapping notes:**
- **4500-01 Discounts** is computed as the remainder (`standard_total + loyalty_total − named buckets`), so new discount reasons (Police/Fire, Military, Senior, paid meal combos like "3 Finger Meal", etc.) land here automatically without code changes.
- **4500-03 Promotions** only includes reasons starting with `"Free "` (free meal promos from the Loyalty group). Paid combo meals stay in 4500-01.
- `App Reward` rows are skipped for R365 write since R365 auto-fills that line.

---

## Revel JSON Structure

The Operations Report API (`/reports/operations/json/`) returns:

```json
{
  "product_mix_data": [
    {
      "product_class": "1. Food",
      "row_type": "Class",
      "price": 4633.75,         // Gross Sales → 4000-01 Food Sales
      "discount": 13.80,
      "order_discount": 182.84,
      "voids_amount": 43.41,
      "comps_amount": 0.00
    }
  ],
  "tax_data": [
    { "row_type": "totals_row", "tax": 536.72 }  // → 2240-000 Sales Tax Payable
  ],
  "sales_data": {
    "credit_total": 5286.19,        // Credit card sales
    "credit_tips_total": 24.75,     // Tips on credit cards
    "credit_refunds": 0.00,
    "adj_total": 0.74,              // CC tip adjustment → 70250 CC Fees + 2301 Tips (editable)
    "cash_for_sales": 1148.82,      // Cash → 1255 Undeposited Funds (read-only)
    "net_account_for": 7039.83,
    "total_payments": 7045.33,
    "custom_payments": {
      "payment_202": { "name": "Uber Eats", "total": "115.93" },
      "payment_209": { "name": "DD Marketplace", "total": "494.39" },
      "payment_200": { "name": "Door Dash", "total": "0.00" }
    }
  },
  "discounts_data": [
    { "reason": "Employee $9.79 Off", "amount": 61.13, "is_total": false },
    { "reason": "Manager 100%",        "amount": 3.59,  "is_total": false },
    { "reason": "3 Finger Meal - 1",   "amount": 12.00, "is_total": false },
    { "reason": "Free 3 Finger Meal - 1", "amount": 87.18, "is_total": false },
    { "reason": "Standard",            "amount": 76.72, "is_total": true  },
    { "reason": "Loyalty",             "amount": 87.18, "is_total": true  },
    { "reason": "App Reward",          "amount": 5.00,  "is_total": false }
  ]
}
```

---

## API Endpoints

### `POST /api/fetch`
Fetches Revel Operations Reports. Streams progress via Server-Sent Events.

**Body:**
```json
{ "start_date": "2026-06-02", "establishments": [32, 14, 48] }
```

**Stream events:** `progress` (per establishment) → `done` (full results array)

---

### `POST /api/r365/navigate`
Opens a headless R365 browser, navigates to the DSS for the given date/location, and fills the Journal Entry.

**Body:**
```json
{
  "date": "2026-06-02",
  "location_name": "LCF Airtex",
  "revel_data": { ... }
}
```

`revel_data` is the raw `data` object from a single establishment's fetch result.

---

### `POST /api/r365/reconcile-all`
Fills Journal Entries for multiple establishments sequentially. Streams SSE events.

**Body:**
```json
{
  "date": "2026-06-02",
  "establishments": [
    { "id": 32, "name": "LCF Airtex", "data": { ... } },
    { "id": 14, "name": "LCF Beaumont", "data": { ... } }
  ]
}
```

**Stream events:** `r365_progress` (per entity, status: `running`/`success`/`error`) → `r365_done`

---

### `GET /api/establishments`
Returns all establishment IDs and names.

---

## Sessions & Login

- **App login**: Session-based auth via Flask. Credentials set in `.env` (`LOGIN_USERNAME` / `LOGIN_PASSWORD_HASH`).
- **Revel**: Browser session cached at `/tmp/revel_session.json`. Auto re-login if expired.
- **R365**: Persistent browser profile at `~/.r365_browser_profile`. Stays logged in across runs.

---

## CLI Usage

Both packages expose a CLI for standalone use without the server.

**Fetch Revel reports:**
```bash
source venv/bin/activate
python -m revel.operations --date 2026-06-02
python -m revel.operations --date 2026-06-02 --establishments 32,14
python -m revel.operations --date 2026-06-02 --output results.json
```

**Open R365 Journal Entry:**
```bash
source venv/bin/activate
python -m r365.journal_entry --date 2026-06-02
```

---

## Debugging

Use `debug_je.py` to inspect the live R365 Journal Entry grid DOM — useful when the form fill breaks or a new R365 deployment changes the DOM structure.

```bash
source venv/bin/activate
python3 debug_je.py
```

Edit `TARGET_DATE` and `LOCATION` at the top of the file before running. Screenshots are saved to `/tmp/`:

| File | Contents |
|---|---|
| `/tmp/debug_dss.png` | DSS iframe state if not found |
| `/tmp/r365_dss_filtered.png` | DSS list after date filter applied |
| `/tmp/r365_entity.png` | Entity form after clicking |
| `/tmp/r365_journal_entry.png` | JE tab after clicking |
| `/tmp/r365_je_filled.png` | JE after fill attempt |

---

## Reconciliation Workbook

`DSS_Reconciliation.xlsx` contains one tab per establishment for manual verification.

- Enter the date, Revel values, and R365 values in the yellow input cells
- Variance column auto-calculates — turns **red** on mismatch, **green** if zero
- Discount field mappings are now confirmed — orange rows can be treated as fully automated.
