# Revel → R365 Journal Entry Automation

Fetches daily sales data from Revel Operations Reports and automatically fills the Journal Entry tab in Restaurant365 (R365) Daily Sales Summary for all 11 LCF establishments.

---

## What It Does

1. Logs into Revel and fetches the Operations Report JSON for each establishment
2. Extracts all mapped field values (sales, tax, tips, marketplace payments, discounts)
3. Opens R365 in a browser, navigates to the Daily Sales Summary for the matching location and date
4. Clicks the Journal Entry tab and fills all confirmed mapped fields

---

## Project Structure

```
revel-fetcher/
├── server.py                  # Flask API server (main entry point)
├── revel_fetcher.py           # Revel login + Operations Report fetcher
├── r365_fetcher.py            # R365 browser automation + JE form filler
├── index.html                 # Frontend UI
├── debug_je.py                # Debug tool to inspect R365 JE grid DOM
├── DSS_Reconciliation.xlsx    # Manual reconciliation workbook (11 location tabs)
├── requirements.txt           # Python dependencies
└── .env                       # Credentials (not committed)
```

---

## Setup

### 1. Requirements

- Python 3.11+
- Node not required

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
```

### 4. Run the server

```bash
source venv/bin/activate
python3 server.py
```

Then open **http://localhost:5050** in your browser.

---

## Usage

1. Pick a **Start Date** in the UI
2. Click **Fetch Reports** — fetches Revel data for all 11 establishments
3. Once done, click **Open in R365** on any establishment card
4. A headed browser window opens, logs into R365, navigates to the DSS for that location and date, and fills all Journal Entry fields automatically

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
| 1255 - Undeposited Funds | Read-only — auto-filled by R365 | — |
| 2301 - Employee Tips Payable (editable) | `sales_data.adj_total` | ✅ Confirmed |
| 4500-02 - Comps | `discounts_data[Manager 100%]` | ⚠️ Unconfirmed |
| 5000-17 - Employee Discount | `discounts_data[Employee $9.79 Off]` | ⚠️ Unconfirmed |
| 4500-01 - Discounts | `discounts_data[Military/Police/Senior/Standard remainder]` | ⚠️ Unconfirmed |
| 4500-03 - Promotions | `discounts_data[Loyalty meal promos]` | ⚠️ Unconfirmed |

### Read-only (auto-filled by R365, not touched)

| R365 Account | Notes |
|---|---|
| 1255 - Undeposited Funds | Auto-calculated from cash |
| 8000-06 - Cash Over/Short | Auto-calculated from variance |
| 2301 - Employee Tips Payable (2nd row) | Auto-calculated from tips_total |

> **⚠️ Discount mappings are unconfirmed.** The code uses suspected reason→account rules based on discount reason names. These must be verified with the team before relying on automation for discount fields. See `TODO` comment in `server.py` `_extract_revel_values()`.

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
      "discount": 13.80,        // Item Disc
      "order_discount": 182.84, // Order Disc
      "voids_amount": 43.41,    // Voids/Returns
      "comps_amount": 0.00      // Comps
    }
  ],
  "tax_data": [
    { "row_type": "totals_row", "tax": 536.72 }  // → 2240-000 Sales Tax Payable
  ],
  "sales_data": {
    "credit_total": 5286.19,        // Credit card sales
    "credit_tips_total": 24.75,     // Tips on credit cards
    "credit_refunds": 0.00,         // Credit refunds
    "adj_total": 0.74,              // CC tip adjustment → 70250 CC Fees + 2301 Tips (editable)
    "cash_for_sales": 1148.82,      // Cash → 1255 Undeposited Funds (read-only)
    "net_account_for": 7039.83,     // Net to account for
    "total_payments": 7045.33,      // Grand total payments
    "custom_payments": {
      "payment_202": { "name": "Uber Eats", "total": "115.93" },
      "payment_209": { "name": "DD Marketplace", "total": "494.39" },
      "payment_200": { "name": "Door Dash", "total": "0.00" }
    }
  },
  "discounts_data": [
    { "reason": "Employee $9.79 Off", "amount": 61.13 },
    { "reason": "Manager 100%", "amount": 3.59 },
    { "reason": "Free 3 Finger Meal - 1", "amount": 87.18, "is_total": false }
  ]
}
```

---

## API Endpoints

### `POST /api/fetch`
Fetches Revel Operations Reports for all establishments. Streams progress via Server-Sent Events.

**Body:**
```json
{ "start_date": "2026-06-02", "establishments": [32, 14, 48] }
```

**Stream events:** `progress` (per establishment) → `done` (full results)

---

### `POST /api/r365/navigate`
Opens R365, navigates to DSS for the given date/location, and fills the Journal Entry with all mapped values extracted from `revel_data`.

**Body:**
```json
{
  "date": "2026-06-02",
  "location_name": "LCF Airtex",
  "revel_data": { ... }
}
```

`revel_data` is the raw `data` object from a single establishment's fetch result. The server extracts all JE field values via `_extract_revel_values()` in `server.py`.

---

### `GET /api/establishments`
Returns the list of establishment IDs and names.

---

## Sessions & Login

- **Revel**: Browser session cached at `/tmp/revel_session.json`. Auto re-login if expired.
- **R365**: Persistent browser profile at `~/.r365_browser_profile`. Stays logged in across runs.

---

## Reconciliation Workbook

`DSS_Reconciliation.xlsx` contains one tab per establishment for manual verification.

- Enter the date, Revel values, and R365 values in the yellow input cells
- Variance column auto-calculates and turns **red** if there's a mismatch, **green** if zero
- Orange rows = discount fields awaiting mapping confirmation

---

## Debugging

Use `debug_je.py` to inspect the R365 Journal Entry grid DOM structure:

```bash
source venv/bin/activate
python3 debug_je.py
```

Screenshots are saved to `/tmp/`:
- `r365_dss_filtered.png` — DSS list after date filter
- `r365_journal_entry.png` — JE tab after clicking
- `r365_je_filled.png` — JE after fill attempt
