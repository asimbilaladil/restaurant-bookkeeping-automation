# Project Plan — Revel → R365 Journal Entry Automation

## What Has Been Done

### 1. Initial Automation (commit `51ebde8`)
- Fetches Revel Operations Reports for all 11 LCF establishments
- Automatically fills R365 Daily Sales Summary Journal Entry fields via Playwright

### 2. Login & Auth (commit `07d32fe`)
- Session-based login protecting all routes and API endpoints
- Scrypt-hashed password stored in `.env`
- Styled login page with `/logout` support

### 3. Dashboard & UI Redesign (commit `a54f5f9`)
- New dashboard landing page at `/`
- Daily Sales Reconciliation UI at `/daily-sales-reconciliation`
- Inter font, CSS variables, full layout refresh
- Branding updated to TechStep Solutions
- Both Playwright contexts switched to `headless=True` for server deployment
- `ESTABLISHMENTS` consolidated into a single list-of-tuples as source of truth

### 4. Package Restructure (commit `f5864fe`)
- Split monolithic files into `revel/` and `r365/` packages
- `revel/`: `establishments.py`, `session.py`, `operations.py`, `__init__.py`
- `r365/`: `session.py`, `journal_entry.py`, `__init__.py`
- `debug_je.py` reduced from 201 → 120 lines by importing shared R365 login

### 5. README Update (commit `4eb3330`)
- Documents new package structure, routes, setup instructions, and CLI usage

### 6. Activity Log Improvements (commit `2587b3a`)
- Screenshot links and R365 direct links added to the activity log

### 7. Fix Establishment Switching (commit `2a67df8`)
- `revel/operations.py`: Added `_switch_establishment()` — POSTs to `/navigation/load_establishment_tree/` before each fetch
- Without this fix, Revel's server ignored the `?establishment=` URL param and always returned data for the last active session establishment
- Verified: Shepherd returns 6241.49, Airtex returns 7422.35 (previously both returned Airtex data)

### 8. Fix JE Field Mappings (commit `2a67df8`)
- `employee_tips` and `credit_card_fees` corrected from `adj_total` → `adj_credit_tips`
- Confirmed from live `sales_data` response

---

## What Is Pending

### High Priority

| Item | File | Notes |
|---|---|---|
| **Save button** | `r365/journal_entry.py` → `fill_journal_entry()` | Fills all JE cells but never clicks Save — entries not persisted in R365 |
| **Sales Tax** | `server.py` → `_extract_revel_values()` | Must exclude marketplace-remitted tax (DoorDash + Uber delivery tax) |
| **Credit Cards AR (1200-000)** | `server.py` | Must exclude marketplace payments from `credit_total + credit_tips` |

### Field Mapping Corrections

| JE Field | Current Source | Correct Source | Status |
|---|---|---|---|
| Employee Tips (2301) | `adj_credit_tips` | `adj_credit_tips` | Done |
| Credit Card Fees (70250) | `adj_credit_tips` | `adj_credit_tips` | Done |
| Sales Tax | Full Revel tax total | Exclude DD + Uber marketplace-remitted tax | Pending |
| Delivery Sales | "5. Delivery-Food" `price` only | Include beverage delivery too | Pending |
| Food Sales | "1. Food" `price` (includes delivery items) | Net of items in Delivery Sales | Pending |
| Beverage Sales | "2. Beverage" `price` (includes delivery items) | Net of items in Delivery Sales | Pending |
| Credit Cards AR (1200-000) | `credit_total + credit_tips` | Exclude marketplace payments | Pending |
| A/R UberEats (1245-12) | Revel custom payment total | Actual platform total (Revel POS captures partial orders only) | Pending |
| DoorDash (1245-03) | Revel custom payment total | 922.83 vs 925.27 — team flagged to verify | Pending |

### Discount Mapping

| Reason | Maps To | Status |
|---|---|---|
| "Manager 100%" | 4500-02 Comps | Verified (Shepherd 06/03: $5.49) |
| "Employee $9.79 Off" | 5000-17 Employee Discount | Unconfirmed |
| "Free/3/5 Finger Meal" | 4500-03 Promotions | Unconfirmed |
| "4 Finger Meal - 1" | Currently falls to Discounts | Likely Promotions — awaiting team confirmation |
| "Remake" | Skipped | Unconfirmed |
| Everything else | 4500-01 Discounts | Default |

### Cleanup
- Remove temporary debug `log.info` lines in `server.py` → `_extract_revel_values()` once field names are confirmed from log output
