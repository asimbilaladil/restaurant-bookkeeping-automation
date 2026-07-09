# Restaurant Bookkeeping Automation — Technical Specification

Reference document for the automations currently in this repo. Use this to
bootstrap a new automation without re-discovering how Revel or R365 work.

- **Automation 1** — Daily Sales Reconciliation (DSS): Revel → R365 Journal Entry
- **Automation 2** — Receivable Reconciliation: R365 Balance Sheet → GL Detail xlsx

---

## 1. Shared Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Language | Python 3 | Playwright + openpyxl + Flask ecosystem |
| Browser automation | **Playwright** (sync API), headless Chromium | Reliable frames/tabs handling for legacy iframes inside React apps |
| HTTP inside browser | `context.request` | Reuses the browser's cookies/session — no separate auth |
| Web server | **Flask 3** + `flask-cors` | Simple, session-based auth, easy SSE streaming |
| Excel | **openpyxl** | Header detection, unmerge, insert cols, static values |
| Persistence | **SQLite** (`dss_runs.db`) via `db.py` | Track per-location per-date run state, idempotent retries |
| Config | `.env` via `python-dotenv` | Creds + tenant URL + Flask secret + API token |
| Streaming | Server-Sent Events (SSE) via `Response(mimetype="text/event-stream")` | Live progress from Flask to `EventSource` in browser |

`requirements.txt`:
```
playwright>=1.40.0
flask>=3.0.0
flask-cors>=4.0.0
python-dotenv>=1.0.0
openpyxl>=3.1.0
```
Also required: `playwright install chromium` after `pip install`.

### Directory conventions

```
revel/                # Revel POS client (session + establishments + operations)
r365/                 # R365 client (session + journal_entry + report_viewer + gl_excel_processor)
server.py             # Flask app: DSS UI + Receivable UI + APIs
daily_reconcile.py    # Cron entry point for DSS
db.py                 # SQLite run-tracking
dss_runs.db           # SQLite database file
logs/                 # Per-run entity logs: {SafeName}_{YYYY-MM-DD}.log
downloads/            # Processed GL xlsx outputs (kept locally, ignored by git)
/tmp/                 # Playwright screenshots + Revel PDFs + Revel session cache
```

### Environment variables (`.env`)

```env
REVEL_USER=...                  # Revel login email
REVEL_PASS=...                  # Revel login password
R65_USER=...                    # R365 login email  (note the key is R65_USER, not R365_USER)
R65_PASS=...                    # R365 login password
R365_URL=https://ayg.restaurant365.com   # Tenant URL

FLASK_SECRET_KEY=...            # Flask session signing
LOGIN_USERNAME=admin            # Dashboard login username
LOGIN_PASSWORD_HASH=scrypt:...  # werkzeug-generated hash

DSS_RUNS_API_TOKEN=...          # Static bearer token for /api/dss-runs/failed-yesterday
```

Generate the password hash with:
```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('mypass'))"
```

---

## 2. Revel POS Access (reusable client)

Files: `revel/session.py`, `revel/establishments.py`, `revel/operations.py`.

### 2.1 Auth

- Tenant base URL: `https://laynes.revelup.com`
- No public API — Revel uses username/password behind a redirect to
  `authentication.revelup.com` (their identity provider).
- Credentials in `.env`: `REVEL_USER`, `REVEL_PASS`.

**Session caching:** Playwright `storage_state` is written to
`/tmp/revel_session.json`. On subsequent runs, the context is created with
`storage_state=` and only re-authenticates if the URL contains
`"authentication"` or `"login"` (expired session).

Login flow (`revel/session.py::login_and_save`):
1. `page.goto(BASE_URL)`
2. `page.wait_for_url("**authentication.revelup.com**")`
3. Fill `input[name="username"]`, submit
4. Fill `input[name="password"]`, submit
5. `wait_for_load_state("networkidle")`
6. `context.storage_state(path=STATE_FILE)`

### 2.2 Establishments

`revel/establishments.py` — hardcoded list of 11 LCF locations:

```python
ESTABLISHMENTS = [
    (32, "LCF Airtex"), (14, "LCF Beaumont"), (48, "LCF Downtown Houston"),
    (7,  "LCF Ella"),   (6,  "LCF Katy"),     (25, "LCF Mission Bend"),
    (36, "LCF Missouri City"), (26, "LCF Nederland"),
    (20, "LCF Pasadena"), (40, "LCF Rosenberg"), (15, "LCF Shepherd"),
]

# R365's DSS grid uses different display names for 3 locations
R365_NAME_OVERRIDES = {
    48: "LCF Downtown",     # Revel: "LCF Downtown Houston"
    7:  "LCF Garden Oaks",  # Revel: "LCF Ella"
    20: "LCF Fairmont",     # Revel: "LCF Pasadena"
}
```

**Anytime you're crossing Revel → R365, remember `R365_NAME_OVERRIDES`.**

### 2.3 Switching establishment (multi-tenant flow)

Revel's backend tracks the "active establishment" in the session. You **must**
POST the switch before every data fetch:

```python
csrftoken = next(c["value"] for c in context.cookies() if c["name"] == "csrftoken")
context.request.post(
    f"{BASE_URL}/navigation/load_establishment_tree/",
    form={
        "establishments": str(est_id),
        "establishment":  str(est_id),
        "node_type":      "1",
        "node_id":        str(est_id),
        "location":       "/reports/operations/",
    },
    headers={"X-CSRFToken": csrftoken, "Referer": f"{BASE_URL}/dashboard/"},
)
# Response JSON: {"errors": [...]}. Empty errors == success.
```

Then run the report GET. Same context reuses cookies + the just-switched
establishment. One browser session handles all 11 locations sequentially.

### 2.4 Fetching Operations Report

**JSON:** `GET /reports/operations/json/`
**PDF:** `GET /reports/operations/data.pdf`  (same params, binary body)

Params (identical for both):
```python
{
    "establishment":       est_id,
    "employee":            "",
    "online_app":          "",
    "online_app_type":     "",
    "online_app_platform": "",
    "show_unpaid":         1,
    "show_irregular":      1,
    "range_from":          "M/D/YYYY 05:00:00",  # inclusive
    "range_to":            "M/D/YYYY 05:00:00",  # exclusive (next day)
}
```

**Business day quirk:** Revel closes at 05:00, not midnight. To fetch
"June 2, 2026" use `range_from = "6/2/2026 05:00:00"` and
`range_to = "6/3/2026 05:00:00"`. Helper: `revel/operations.py::build_date_range`.

**Response shape (JSON) — the pieces the automation reads:**
```jsonc
{
  "sales_data": {
    "total_sales": ..., "total_payments": ...,
    "credit_total": ..., "credit_tips_total": ..., "adj_credit_tips": ...,
    "cash_for_sales": ..., "net_sales_untaxed": ..., "net_account_for": ...,
    "custom_payments": {
      "<uuid>": {"name": "UberEats Marketplace", "total": 115.93},
      "<uuid>": {"name": "DoorDash Marketplace", "total": 494.39}
    }
  },
  "product_mix_data": [
    {"row_type": "Class", "product_class": "1. Food",
     "taxable_sales": ..., "untaxable_sales": ...},
    // "2. Beverage", "4. Other Sales", "5. Delivery - Food",
    // "Unknown Class", "Extra Items", ...
  ],
  "tax_data":       [ /* rows + one {"row_type":"totals_row","tax":...} */ ],
  "discounts_data": [ /* rows with is_total flag; groups: "Standard", "Loyalty" */ ]
}
```

### 2.5 Revel → R365 GL mapping (DSS)

The mapping lives in `server.py::_extract_revel_values`. Reference table:

**Credit side (revenue + liabilities):**

| R365 acct | Field | Revel calculation |
|---|---|---|
| 4000-01 Food Sales           | `food_sales`           | `product_mix_data["1. Food"].taxable_sales + .untaxable_sales` |
| 4000-011 Food Sales-Tax Exempt (only if ≠ 0) | `tax_exempt_amount` | `abs(sales_data.net_sales_untaxed)` |
| 4000-02 Beverage Sales       | `beverage_sales`       | `product_mix_data["2. Beverage"].taxable_sales` |
| 4000-07 Other Sales (only if ≠ 0) | `other_sales`     | Sum of `taxable + untaxable` for `Unknown Class`, `4. Other Sales`, `Extra Items` |
| 4000-08 Food Delivery Sales  | `delivery_food_sales`  | `product_mix_data["5. Delivery - Food"].taxable + .untaxable` |
| 2240-000 Sales Tax Payable   | `sales_tax`            | `tax_data` row where `row_type == "totals_row"` → `.tax` |
| 2301 Employee Tips Payable   | `employee_tips`        | `sales_data.adj_credit_tips` |
| 70250 Credit Card Fees       | `credit_card_fees`     | `sales_data.adj_credit_tips` |

**Debit side (assets + expenses):**

| R365 acct | Field | Revel calculation |
|---|---|---|
| 1200-000 A/R Credit Cards    | `credit_cards_ar`   | `credit_total + credit_tips_total` |
| 1245-12 A/R-UberEats         | `uber_eats`         | Sum of `custom_payments[*].total` where name contains `"uber eats"` |
| 1245-03 A/R-DoorDash         | `doordash`          | Sum where name contains `"door dash"`, `"doordash"`, or `"dd marketplace"` |
| 1245-08 A/R-GrubHub          | `grubhub`           | Sum where name contains `"grub hub"` or `"grubhub"` |
| 1255    Undeposited Funds    | `undeposited_funds` | `sales_data.cash_for_sales` |
| 4500-01 Discounts            | `item_discounts`    | `Standard_total − employee_discount − comps − app_reward` |
| 4500-02 Comps                | `comps`             | Sum of `discounts_data` non-total rows where `reason == "manager 100%"` |
| 4500-03 Promotions           | `promotions`        | Loyalty group total (`is_total && reason=="Loyalty"`) |
| 5000-17 Employee Discount    | `employee_discount` | Sum where `reason.lower().startswith("employee")` |
| 8000-06 Cash Over/Short (only if ≠ 0) | `cash_over_short` | `abs(net_account_for − total_payments)`; sign → debit if shortage, credit if overage |

**Discount reconciliation strategy:** Rather than assume Revel and R365 discount
lines match 1:1, we read the existing R365 discount rows, sum them, compute the
variance vs. Revel's Standard+Loyalty totals, and only adjust the **plain**
(no-comment) `4500-01 Discounts` row. This means new Revel discount reasons
land in 4500-01 without code changes.

---

## 3. R365 Access (reusable client)

File: `r365/session.py`.

### 3.1 Auth

- Tenant URL from env `R365_URL` (default `https://ayg.restaurant365.com`)
- SSO via `identity.restaurant365.com` (OpenID Connect); callback lands at
  `/NetCore/signin-oidc`, then app.
- Credentials in `.env`: `R65_USER`, `R65_PASS`. **Note the key names are
  `R65_...`, not `R365_...`.**

**Persistent browser profile:** `~/.r365_browser_profile` (via
`browser.launch_persistent_context`). Cookies survive across runs, so login
happens once until R365's session expires.

Login flow (`login_r365`):
1. `page.wait_for_selector("#Username")`
2. `fill("#Username", ...)`, `fill("#Password", ...)`, click `button[type="submit"]`
3. Wait for URL to leave both `identity.restaurant365.com` AND `/signin-oidc`
4. `wait_for_load_state("domcontentloaded")` + settle 3s
5. Verify not on the identity URL and not on nginx 400

### 3.2 Cookie hygiene (mandatory before every navigation)

R365's OIDC handshake writes transient cookies:
- `.AspNetCore.Correlation.*`
- `.AspNetCore.OpenIdConnect.nonce.*`

They're supposed to be cleared after `/signin-oidc` but any aborted login
leaves them behind. They accumulate until nginx returns:

```
400 Bad Request — Request Header Or Cookie Too Large
```

Every R365 entry point calls `_prune_transient_cookies(context)` before
navigating. If nginx still 400s, `ensure_logged_in_r365` clears **all**
cookies and re-logs in (costs one full login).

**When building a new R365 flow, always start with `ensure_logged_in_r365(page, context)` — do not roll your own login.**

### 3.3 UI shape

- App is React (`/react/…` routes) hosting **legacy AngularJS 1.x pages inside iframes**.
- Kendo UI grids (`.k-grid`) inside those iframes.
- Playwright must iterate through `page.frames` — the useful selectors are
  usually inside an iframe, not the top document.

Idiom to find the right frame:
```python
target_frame = next(
    (f for f in page.frames if "DailySalesSummariesGrid" in f.url),  # or whatever marker
    None,
)
```

### 3.4 Date filters

DSS grid filter (5th text input in the grid header):
```python
date_str = target_date.strftime("%-m/%-d/%Y")  # "6/2/2026" — no leading zeros
date_filter = dss_frame.locator('input[type="text"]').nth(4)
date_filter.click(click_count=3)
date_filter.fill(date_str)
date_filter.press("Tab")
```

Report Viewer "As Of" date and drilldown URL params use the same **M/D/YYYY** format.

---

## 4. Automation 1: Daily Sales Reconciliation (DSS)

### What & why

Every night each restaurant closes and Revel produces a daily operations
report. The bookkeeper used to open R365, find the DSS row for each location
and each date, and manually fill a Journal Entry that mirrors Revel's numbers
across ~15 GL accounts, then attach the Revel PDF and Approve the entry. This
automation does that for all 11 locations in one run — and re-runs safely
because SQLite tracks which (location, date) pairs already succeeded.

### End-to-end flow

1. **Trigger**: cron (`daily_reconcile.py`) or dashboard UI (`POST /api/fetch`
   then `POST /api/r365/reconcile-all`).
2. **Fetch (Revel)**: for each establishment, switch session, GET JSON + PDF.
3. **Transform**: `_extract_revel_values(data)` → dict of debits/credits
   keyed by R365 account code.
4. **Post (R365)**: navigate DSS grid, filter to date, click the location row
   (opens new tab), open Journal Entry tab, fill grid cells, add missing rows
   (`4000-07`, `4000-011`, `8000-06`), verify balanced (Σdebit = Σcredit),
   upload PDF, Save, Approve.
5. **Log**: write result row to `dss_runs` in SQLite.

Success requires **balanced AND approved** — a balanced entry that fails to
approve is considered failure (it's stuck in Unapproved).

### Entry points

- CLI: `python3 daily_reconcile.py [YYYY-MM-DD]` (defaults to yesterday)
- API: `POST /api/fetch` (SSE) → `POST /api/r365/reconcile-all` (SSE)

### R365 DSS navigation (`r365/journal_entry.py`)

- URL: `https://ayg.restaurant365.com/react/sales-and-forecasting/legacy/DailySalesSummary`
- After `goto`, sleep ~15s for the legacy iframe (`DailySalesSummariesGrid`) to settle
- Filter grid by date (see §3.4)
- Click the row: `table tbody tr:has(td:nth-child(3):has-text("<Location>")) td:nth-child(5)`
  — this opens a new tab; poll `context.pages` for ~15s
- On the new tab, click `li[role="tab"] span.k-link:has-text("Journal Entry")` (search all frames)

### Journal Entry grid interaction

Grid: `#DSSJournalEntryGrid` (Kendo). Row cells (0-indexed): `0=#, 1=Account, 2=Debit, 3=Credit, 4=Comment, 5=Location`.

Read a cell — evaluate JS that finds the row by account text (stripping the
`<select>` clone inside the Account cell) and returns the parsed number.

Write a cell — same JS to click the cell, wait 800ms for the Kendo editor,
then Playwright locator: `input[name="debit"]` or `input[name="credit"]`,
`.fill(value)`, `.press("Tab")` to commit.

Add a new row — use the `input[placeholder="Select Account"]` at the bottom:
type the account code (e.g. `4000-07`), wait for autocomplete, press Enter,
Tab through Debit → Credit → Comment, then click `.k-grid-toolbar button:has-text("Add")`.

### PDF attachment

R365's attachments module in Angular has two elements with the same id
(`attachmentsModuleInput`) and rejects synthetic `change` events. Preferred
sequence: resolve to a single element handle, open the **native file chooser**
(trusted event), fall back to `set_input_files`, then poll
`scope.AWS_S3_Uploader.files` and the DOM for up to 45s for the filename.

### Save & Approve

Both are `<li>` menu items, not `<button>`s:
```python
document.querySelector('[data-testid="saveMenuItem"]').click()
# wait ~4s, then:
document.querySelector('[data-testid="approveMenuItem"]').click()
```
Wrap the Approve click in `page.expect_response(...ServiceStack/SaveTransaction POST...)`
to know whether the approval actually succeeded.

Only attempt Approve if `je_balanced` (Σdebit - Σcredit rounded to 0.00). Otherwise
status is recorded as `approved="skipped"`.

### Persistence (`db.py`, `dss_runs.db`)

```sql
CREATE TABLE dss_runs (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  establishment_id    INTEGER,
  establishment_name  TEXT,
  run_date            TEXT,        -- YYYY-MM-DD
  status              TEXT,        -- 'success' | 'failed' | 'error'
  error               TEXT,
  je_difference       REAL,
  je_balanced         INTEGER,
  attachment_status   TEXT,        -- uploaded|already_present|failed|skipped
  approved            TEXT,        -- approved|failed|skipped
  log_filename        TEXT,
  created_at          TEXT
);
```

Key helpers in `db.py`:
- `record_run(...)` — insert a new row
- `count_runs(run_date, establishment_id, status)` — for skip-if-succeeded
- `get_failed_no_success(run_date)` — most-recent attempt per (est, date)
  where status ≠ success AND that pair has never succeeded

### Dashboard & APIs (`server.py`)

Login-guarded HTML routes: `/`, `/daily-sales-reconciliation`, `/dss-runs`,
`/receivable-reconciliation`.

DSS APIs:
- `GET  /api/establishments` — id + display-name map
- `POST /api/fetch` — SSE stream of `progress` events (per establishment) + `done`
- `POST /api/r365/navigate` — single (date, location, revel_data, pdf_path) → fills one JE
- `POST /api/r365/reconcile-all` — SSE stream of `r365_progress` events + `r365_done`
- `GET  /api/dss-runs` — paginated run history (filters: `date`, `establishment_id`, `page`, `per_page`)
- `GET  /api/dss-runs/failed-yesterday` — static-token protected

Static-token auth (see commit `Add static API token auth`): accepts
`Authorization: Bearer <token>`, `X-API-Token: <token>`, or `?token=<token>`;
constant-time compare against `DSS_RUNS_API_TOKEN`.

---

## 5. Automation 2: Receivable Reconciliation (GL Detail export)

### What & why

Reconciles marketplace receivables (UberEats, DoorDash, GrubHub) against
what R365 says is outstanding. Instead of posting anything back, this
automation just delivers a **cleaned Excel per location** so the bookkeeper
can color-code and reconcile in the sheet, then post the JE by hand.

**Not fully automated** — the human decides the receivable account, the
legal entities, and the date range; the tool navigates R365, drills into the
right cells, downloads the raw SSRS export, and cleans it up.

### Flow

1. UI (`receivable-reconciliation.html`) posts to `POST /api/r365/report-viewer`
   with `legal_entities`, `start_date`, `end_date`, `show_unapproved`,
   `calendar`, `receivable_account`.
2. Log into R365, click sidebar **Reports** (→ MyReports).
3. Click the **Accounting TAB** inside MyReports (NOT the sidebar
   "Accounting" link — those go to different pages).
4. Click **Customize** on the Balance Sheet row; set:
   - Report Type = "Legal Entity Side by Side"
   - Detail Level = "Detail"
   - Account View = "Name"
   - Hide $0 Balances = "Yes"
   - Show Unapproved = user choice
   - As Of Date = `M/D/YYYY`
   - Filter (Legal Entity) = user-selected entities
5. Click **Run** → ReportViewer opens in a new tab.
6. For each entity: find the row starting with the receivable account label
   (e.g. `A/R-UberEats`), click the linked amount → new detail tab opens.
7. **Rewrite the detail tab URL** to inject correct dates
   (`re.sub(r'Start=[^&]+', f'Start={M}/{D}/{YYYY}', url)` etc.) before letting
   the page finish loading — saves one full SSRS render.
8. Export to Excel. Three strategies in order:
   1. Direct HTTP: `re.sub(r"rs:Format=[^&]+", "rs:Format=EXCELOPENXML", url)`
      then `context.request.get(export_url)`. Fastest.
   2. JS API: `$find('ReportViewerControl').exportReport('EXCELOPENXML')`, wait for `expect_download`.
   3. Click the visible `a[title="Excel"]` link, wait for download.
9. Post-process (§5.2) → save as `{filename}_processed.xlsx`.
10. SSE stream `entity_done` events, then final `done`.

### 5.1 Report Viewer selectors (`r365/report_viewer.py`)

Button-group option (Report Type / Detail Level / Hide $0 / etc.) — find the
label span (`span.spanTop`), walk up to the shared parent, click the sibling
`button.groupX` whose text equals the option.

Customize button — `document.getElementById('customizeViewer-Balance Sheet')`
or find text `"Balance Sheet"` and walk up 6 parents looking for a
`button` with text `Customize`.

RUN button — near the report title, look for `button.runBTN`; pick the
right-most one (they have multiple Run buttons stacked).

Accounting TAB (inside MyReports) — try `md-tabs li[name="Accounting"] a`,
`li[name="Accounting"][role="tab"] a`. Poll for up to 60s.

Balance Sheet drilldown — find the `<tr>` whose text (after normalizing
`&nbsp;` and whitespace) startsWith the row label, then click the first
non-empty `<a href>` in that row. Detail opens as a new browser tab
(`browser.on("page", …)`).

### 5.2 Excel post-processing (`r365/gl_excel_processor.py`)

Steps 1–5 of the reconciliation spec (steps 6+ are still manual):

1. **Header row**: scan first 25 rows for a row containing `Date`, `Debit`,
   AND `Credit` (case-insensitive). Delete all rows above it. Also capture the
   1-based column index of the `Date` cell.
2. **Leading empty columns**: delete `date_col - 1` columns from the left so
   `Date` becomes column A. (Fixed offset was assumed once, then this
   auto-detect replaced it because some exports had a different offset.)
3. **Unmerge** all merged ranges; apply `Alignment(wrap_text=True, vertical="top")` to every cell.
4. **Insert "Amount" column** immediately after `Credit`.
5. **Populate Amount** = `round(debit - credit, 2)` as a **static value**
   (not a formula — earlier version used formulas but numeric values are what
   downstream steps need).

Also sets fixed column widths (A=22 … K=13) for readable output.

Output naming: `{original_stem}_processed.xlsx` in the same directory. Raw and
processed both land in `downloads/`; retention plan is to eventually drop the
raw and keep only `_processed.xlsx`.

---

## 6. Reusable patterns (for the next automation)

### 6.1 SSE streaming from Flask

Long jobs use a worker thread + a `queue.Queue` + a generator response:
```python
q = queue.Queue()
def worker():
    try:
        for r in long_job():
            q.put({"type": "progress", **r})
        q.put({"type": "done"})
    except Exception as e:
        q.put({"type": "error", "message": str(e)})

threading.Thread(target=worker, daemon=True).start()

def stream():
    while True:
        ev = q.get(timeout=120)
        yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
        if ev['type'] in ('done', 'error'):
            break

return Response(stream(), mimetype='text/event-stream')
```
Frontend uses `EventSource(url).addEventListener('progress', …)`.

### 6.2 Playwright session reuse

- Revel: `context.storage_state(path=...)` → cheap, JSON on disk.
- R365: `browser.launch_persistent_context(profile_dir, headless=True)` →
  full browser profile, survives across processes.
- Always check the URL after `goto` — an expired session sends you back to
  the identity provider.

### 6.3 Locating things inside legacy iframes

The useful markup often lives in one of many `page.frames`. Idioms:
- Filter by URL substring: `next(f for f in page.frames if "Grid" in f.url)`
- Try main page + all frames in a loop when the target could live anywhere.

### 6.4 Interacting with Kendo grids

- **Read** via `evaluate(...)` — cheaper than Playwright locators for grids
  with dozens of columns.
- **Write** requires dispatching a real click event, waiting 800ms for the
  Kendo editor to render, then filling the `<input>` with the field name Kendo
  gave it (`name="debit"`, `name="credit"`), and pressing `Tab` to commit.
- Kendo autocomplete inputs need Enter after `.fill()` to actually select.

### 6.5 SQLite for idempotent runs

The `dss_runs` pattern generalizes: `(target, date) × status` table; a run is
skipped if the same target already succeeded for the same date. Retries are
free (nothing double-posts if the previous success is in the DB).

### 6.6 Debugging

- Every R365 run creates `logs/{safe_name}_{YYYY-MM-DD}.log` with full
  `log.info` output including a rendered ASCII of the JE grid before/after.
- Screenshots go to `/tmp/r365_*` and are served via `/screenshots/<name>`
  (login required).
- `debug_je.py` is a standalone script for poking at the JE flow without
  running the full server.

---

## 7. Deployment

Local dev:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python3 server.py             # http://localhost:5050
```

Cron for DSS:
```cron
# Daily at 04:00 (after Revel 05:00 close of business day)
0  4 * * *  cd /path/to/repo && ./.venv/bin/python3 daily_reconcile.py
# Every 30 min, retry only pending locations
*/30 * * * * cd /path/to/repo && ./.venv/bin/python3 daily_reconcile.py
```

Retries are safe by design — SQLite skips (location, date) pairs already marked success.
