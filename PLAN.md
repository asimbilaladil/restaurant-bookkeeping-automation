# Project Plan — Restaurant Bookkeeping Automation

## Overview

A Flask web application that automates two accounting workflows for AYG Foods restaurant group:
1. **Daily Sales Reconciliation** — Fetches Revel POS data and posts journal entries to Restaurant365
2. **Receivable Reconciliation** — Navigates R365 GL Account Detail Export to set account filters (e.g. 1245-12 A/R-UberEats)

**Stack:** Python · Flask · Playwright · Revel API · Restaurant365 (R365) web UI  
**Server:** `http://localhost:5050` — run with `.venv/bin/python server.py`  
**Auth:** Session-based login. Credentials in `.env` (`LOGIN_USERNAME` / `LOGIN_PASSWORD_HASH`)

---

## Architecture

```
restaurant-bookkeeping-automation/
├── server.py                  # Flask API + HTML routes
├── dashboard.html             # Landing page — lists all automations
├── daily-sales-reconciliation.html  (served from server.py)
├── receivable-reconciliation.html   # Beta — Report Viewer UI
├── login.html                 # Login form
├── revel/
│   ├── __init__.py            # exports fetch_reports, DEFAULT_ESTABLISHMENTS
│   ├── establishments.py      # establishment ID ↔ name mapping
│   ├── session.py             # Revel cookie-based login
│   └── operations.py          # fetch Revel Operations Report per establishment
├── r365/
│   ├── __init__.py            # exports open_r365_journal_entry, open_report_viewer
│   ├── session.py             # R365 Playwright login + ensure_logged_in_r365()
│   ├── journal_entry.py       # Daily Sales JE automation
│   └── report_viewer.py       # GL Account Detail Export navigation (beta)
├── logs/                      # Per-entity per-date log files
├── .env                       # Credentials (not committed)
└── requirements.txt
```

---

## Automation 1 — Daily Sales Reconciliation ✅ Active

### What it does
1. User selects a date in the dashboard UI and clicks Run
2. Server fetches Revel Operations Reports for all 11 LCF establishments via Revel API
3. Server maps Revel fields to R365 journal entry line items
4. Playwright opens R365, navigates to the Daily Sales Summary JE for each establishment, and fills in all values
5. SSE stream pushes real-time progress to the browser

### Key files
- `revel/operations.py` — fetches and parses the Revel Operations Report
- `r365/journal_entry.py` — fills R365 JE fields via Playwright
- `server.py` → `_extract_revel_values()` — maps Revel → R365 field names

### Field Mapping (Revel → R365 Journal Entry)

| R365 JE Line | Revel Source Field | Notes |
|---|---|---|
| Food Sales | `adj_food` | Net of delivery items |
| Beverage Sales | `adj_beverage` | Net of delivery items |
| Delivery Sales | Delivery category total | Food + beverage delivery |
| Sales Tax | Revel tax total | Must exclude marketplace-remitted tax (DD + Uber) — pending |
| Employee Tips (2301) | `adj_credit_tips` | Confirmed |
| Credit Card Fees (70250) | `adj_credit_tips` | Confirmed |
| Credit Cards AR (1200-000) | `credit_total + credit_tips` | Must exclude marketplace payments — pending |
| A/R UberEats (1245-12) | Revel custom payment total | Revel only captures partial — pending reconciliation |
| DoorDash (1245-03) | Revel custom payment total | Minor variance flagged |
| Employee Discount (5000-17) | Revel discount by reason | Matched via `startswith('employee')` |
| Comps (4500-02) | Revel discount — "Manager 100%" | Verified |
| Promotions (4500-03) | Revel discount — "Free Meal" etc | Unconfirmed mapping |
| Discounts (4500-01) | Everything else | Default fallback |

### Known issues / pending
- Sales Tax: must subtract marketplace-remitted tax (DoorDash + Uber Eats delivery tax)
- Credit Cards AR (1200-000): must exclude marketplace payments
- A/R UberEats: Revel POS captures partial orders — real total comes from UberEats dashboard
- 8000-06: R365 auto-calculates — skip write (already implemented)
- Discount mapping for "4 Finger Meal - 1" and "Remake" — awaiting team confirmation

### Completed fixes
- `_switch_establishment()`: POSTs to Revel `/navigation/load_establishment_tree/` before each fetch — without this, Revel ignored `?establishment=` param and returned stale data
- `employee_tips` / `credit_card_fees`: corrected from `adj_total` → `adj_credit_tips`
- Employee Discount cell: polls until the cell settles before reading discount totals
- emp/comps written to R365 BEFORE reading discount totals (prevents stale reads)

---

## Automation 2 — Receivable Reconciliation / Report Viewer 🔶 Beta (In Progress)

### Goal
Navigate R365 automatically to:
1. Login to R365
2. Go to My Reports page
3. Click the Accounting tab
4. Find "GL Account Detail Export" → click Customize
5. Set Account filter to **1245-12 - A/R-UberEats**
6. Return a screenshot confirming the setup

### UI Entry Point
`http://localhost:5050/receivable-reconciliation` → "Open Report Viewer in R365" button  
Calls `POST /api/r365/report-viewer` → runs `open_report_viewer()` in a thread pool

### Technical implementation (`r365/report_viewer.py`)

**Step 1 — Login + wait for React sidebar**
- `ensure_logged_in_r365()` handles login (detects `identity.restaurant365.com`, `login`, or `logout` in URL)
- Waits up to 30s for React app sidebar to render at `/react/home`

**Step 2 — Navigate to My Reports via sidebar link**
- Clicks the sidebar `<a href="...reports-management...">` link (React Router client-side nav)
- Direct `page.goto(MY_REPORTS_URL)` was abandoned — causes AngularJS to get stuck in permanent loading state after fresh login
- Falls back to direct goto if sidebar link not found

**Step 3 — Detect legacy AngularJS iframe**
- The React shell embeds AngularJS content inside an `<iframe>`
- All `page.evaluate()` calls search the React document — miss iframe content entirely
- `_get_legacy_frame(page)` finds the first non-main frame on `restaurant365.com`
- All subsequent DOM operations use `ctx = frame` instead of `page`

**Step 4 — Poll for Customize button (in iframe context)**
- Polls up to 90s via `ctx.evaluate()` (searches iframe document)
- Logs frame URL and button count every 15s
- Takes screenshots every 15s for debugging (`/tmp/poll_Xs_*.png`)

**Step 5 — Click Accounting tab**
- `ctx.evaluate("document.querySelector('li[aria-label=\"Accounting\"] a').click()")`
- The AngularJS nav bar uses `<li aria-label="Accounting">` — exact selector required
- Previous attempts failed by finding the sidebar "Accounting" nav item instead of the tab

**Step 6 — Re-poll for Customize after tab switch**
- Polls 30s for `document.getElementById('customizeViewer-GL Account Detail Export')`

**Step 7 — Click Customize button**
- Clicks by ID first; falls back to first `button[text=Customize]`

**Step 8 — Click ACCOUNT button**
- Finds button where text is exactly `ACCOUNT` (not `ACCOUNTS AVAILABLE`)
- `t.includes('ACCOUNT') && !t.includes('ACCOUNTS') && !t.includes('AVAILABLE')`

**Step 9 — Type "uber eats" in search**
- Uses `ctx.locator('input').last.click()` → `.fill('')` → `.type('uber eats')`
- Falls back to `page.keyboard.type()` if locator fails

**Step 10 — Select 1245-12 option**
- Searches `li, md-option, span, div` for element containing `1245-12` or `uber eats`
- If not found, logs visible options for debugging

### Bugs fixed along the way

| Bug | Root Cause | Fix |
|---|---|---|
| ERR_ABORTED on page.goto | React SPA aborts navigation requests | `wait_until="commit"` + catch exception |
| `frame.keyboard` AttributeError | `keyboard` lives on `page` not `frame` | Use `page.keyboard.type()` |
| `tab_result` undefined | Stale variable reference after rename | Removed stale reference |
| Clicked "Accounts Available" | `startsWith('ACCOUNT')` too broad | Added `!t.includes('ACCOUNTS') && !t.includes('AVAILABLE')` |
| Session expired / logout URL | `ensure_logged_in_r365` didn't detect `/#/user/logout` | Added `"logout" in page.url.lower()` |
| `wait_for_function` invalidated | After `window.location.href`, JS context destroyed | Use `page.wait_for_url()` then `page.evaluate()` |
| Accounting tab → wrong element | `textContent === 'Accounting'` matched left sidebar nav | Use `li[aria-label="Accounting"] a` |
| AngularJS stuck loading (direct goto) | `page.goto(MY_REPORTS_URL)` from fresh login breaks AngularJS bootstrap | Navigate via sidebar link (React Router keeps app state) |
| anyCustomize always 0 despite visible buttons | AngularJS content is inside `<iframe>` — `page.evaluate()` searches wrong document | Detect iframe with `_get_legacy_frame(page)`, use `ctx.evaluate()` throughout |

### Current status
The iframe detection fix was applied most recently and has not been tested yet. All prior attempts failed at the polling step (`anyCustomize: 0`) because evaluation was targeting the React shell document instead of the iframe.

---

## Infrastructure

### Flask Server (`server.py`)
- Port 5050, all interfaces
- Session auth (`FLASK_SECRET_KEY` from `.env`)
- SSE streaming for long-running Revel fetches
- Per-entity log files written to `logs/`
- Endpoints:
  - `GET /` → dashboard
  - `GET /login`, `POST /login`, `GET /logout`
  - `GET /daily-sales-reconciliation`
  - `GET /receivable-reconciliation`
  - `POST /api/fetch` (SSE) — runs Daily Sales Reconciliation
  - `GET /api/establishments` — returns establishment list
  - `POST /api/r365/journal-entry` — runs JE automation for one establishment
  - `POST /api/r365/report-viewer` — runs Report Viewer navigation (beta)
  - `GET /api/logs/<filename>` — streams a per-entity log file

### Environment (`.env`)
```
REVEL_USER=...
REVEL_PASS=...
R65_USER=...
R65_PASS=...
R365_URL=https://ayg.restaurant365.com
FLASK_SECRET_KEY=...
LOGIN_USERNAME=admin
LOGIN_PASSWORD_HASH=scrypt:...
```

### Playwright profiles
- `~/.r365_browser_profile` — persistent Chrome profile reused across runs (keeps R365 session)
- `headless=False` — browser is visible so user can watch automation

---

## Next Steps

### Automation 2 (Receivable Reconciliation)
1. **Test iframe fix** — verify `_get_legacy_frame()` finds the frame and `anyCustomize > 0`
2. **Verify Accounting tab click** — confirm `li[aria-label="Accounting"] a` fires correctly in frame context
3. **Verify Customize opens** — panel should open with Account / Accounts Available fields
4. **Verify uber eats search** — type in search box, 1245-12 option appears
5. **Verify selection** — Account field updates to "1245-12 - A/R-UberEats"
6. Once working end-to-end, mark automation as Active on dashboard

### Automation 1 (Daily Sales Reconciliation)
1. Fix Sales Tax — subtract marketplace-remitted tax
2. Fix Credit Cards AR — exclude marketplace payments
3. Confirm discount reason mappings with team
4. Add Save button click to `journal_entry.py` (entries currently filled but not saved)
