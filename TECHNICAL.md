# Technical Reference — Browser Automation Internals

Deep-dive companion to the [README](README.md). The README covers the end-to-end
flow and the Revel→R365 field *calculations*; this doc covers **how the automation
actually drives each system** — the exact selectors, the Kendo/Angular quirks, the
gotchas that took trial-and-error to get right, and the step-by-step interaction
sequences. Read this when you need to debug or modify the Playwright code.

Source files referenced throughout:

| Area | File |
|---|---|
| R365 JE fill + navigation | `r365/journal_entry.py` |
| R365 login / persistent profile | `r365/session.py` |
| R365 Report Viewer | `r365/report_viewer.py` |
| Revel establishment switching + fetch | `revel/operations.py` |
| Revel login / session caching | `revel/session.py` |
| Establishment IDs + name overrides | `revel/establishments.py` |
| Flask API, SSE, value extraction | `server.py` |

---

## 1. Sessions & Login

Both systems use a **cached/persistent session** so we don't log in on every run.
They differ in mechanism:

### Revel — `storage_state` JSON (`revel/session.py`)

Revel is a plain (non-persistent) Chromium context whose cookies are dumped to and
restored from a JSON file:

```python
STATE_FILE = "/tmp/revel_session.json"
BASE_URL   = "https://laynes.revelup.com"

# fetch_reports():
context = (
    browser.new_context(storage_state=STATE_FILE)   # reuse cookies if present
    if os.path.exists(STATE_FILE)
    else browser.new_context()
)
```

`ensure_logged_in()` navigates to `BASE_URL`; if the URL bounces to
`authentication.revelup.com` / contains `login`, it re-runs `login_and_save()`.
Login is a **two-step** form (username submit → password submit), both on
`authentication.revelup.com`:

```python
page.fill('input[name="username"]', REVEL_USER); page.click('button[type="submit"]')
page.fill('input[name="password"]', REVEL_PASS); page.click('button[type="submit"]')
context.storage_state(path=STATE_FILE)   # persist cookies for next run
```

### R365 — persistent browser profile (`r365/session.py`)

R365 uses `launch_persistent_context(PROFILE_DIR, ...)` — a full on-disk Chrome
profile at `~/.r365_browser_profile`, so the entire login (including any
device-trust state) survives:

```python
PROFILE_DIR = os.path.expanduser("~/.r365_browser_profile")
context = p.chromium.launch_persistent_context(
    PROFILE_DIR, headless=True,
    viewport={"width": 1440, "height": 900},
    args=["--enable-features=DnsOverHttps"],
)
```

`ensure_logged_in_r365()` decides "are we logged in?" purely from the URL — if it
lands on `identity.restaurant365.com` or contains `login`/`logout`, it runs
`login_r365()` (`#Username` / `#Password` / `button[type="submit"]`).

> **Gotcha — env var names.** The R365 credentials are read from `R65_USER` /
> `R65_PASS` (note: **no `3`**), while `R365_URL` *does* have the 3. This is easy
> to get wrong in `.env`. Revel uses `REVEL_USER` / `REVEL_PASS`.

> **Gotcha — Chrome dialogs.** `_dismiss_chrome_dialogs()` clicks any
> `div[role="dialog"|"alertdialog"] button:has-text("OK")` after navigation —
> R365 throws periodic modal dialogs that otherwise block interaction.

---

## 2. How Establishment Change Works in Revel

This is the single most important Revel quirk: **the Operations Report endpoint
ignores the `?establishment=` query param.** The server returns data for whichever
location the *session* is currently pointed at. So before every fetch you must POST
to switch the session's active establishment.

### Step 1 — switch the session (`_switch_establishment`)

```python
def _switch_establishment(context, establishment_id):
    csrftoken = next((c["value"] for c in context.cookies()
                      if c["name"] == "csrftoken"), "")
    resp = context.request.post(
        f"{BASE_URL}/navigation/load_establishment_tree/",
        form={
            "establishments": str(establishment_id),
            "establishment":  str(establishment_id),
            "node_type":      "1",
            "node_id":        str(establishment_id),
            "location":       "/reports/operations/",
        },
        headers={"X-CSRFToken": csrftoken, "Referer": f"{BASE_URL}/dashboard/"},
    )
    # raises if resp.status != 200 OR resp.json()["errors"] is truthy
```

Key points:
- The **CSRF token** is pulled live from the context cookies and sent as the
  `X-CSRFToken` header — without it the POST is rejected.
- A `Referer` of `/dashboard/` is required.
- The response is JSON; an HTTP 200 with a non-empty `errors` array is still a
  failure, so both are checked.

### Step 2 — fetch the now-current establishment (`fetch_establishment_report`)

Immediately after switching (session is "sticky" to that location) we GET the JSON
report. The `establishment` param is passed but, per above, the server keys off the
session, not the param:

```python
GET /reports/operations/json/
    params: establishment=<id>, employee="", online_app*="",
            show_unpaid=1, show_irregular=1,
            range_from="MM/DD/YYYY 05:00:00", range_to="MM/DD/YYYY 05:00:00"
```

The **date range is 5 AM → 5 AM next day** (`build_date_range`) — restaurant
business day, not calendar midnight:

```python
range_from = start_date.strftime("%m/%d/%Y") + " 05:00:00"
range_to   = (start_date + timedelta(days=1)).strftime("%m/%d/%Y") + " 05:00:00"
```

While the session is still on that establishment, the same params are reused to
pull the **PDF** (`/reports/operations/data.pdf`) via `_download_establishment_pdf`,
saved to `/tmp/Revel_Operations_<name>_<date>.pdf`.

### Establishment IDs & name overrides (`revel/establishments.py`)

11 LCF locations. Three have **different display names in the R365 DSS grid** than
in Revel — these overrides are applied before R365 navigation so the right row is
clicked:

```python
R365_NAME_OVERRIDES = {
    48: "LCF Downtown",      # Revel: "LCF Downtown Houston"
    7:  "LCF Garden Oaks",   # Revel: "LCF Ella"
    20: "LCF Fairmont",      # Revel: "LCF Pasadena"
}
```

---

## 3. How We Write Into R365 (Journal Entry)

Writing into R365 is the most involved part. The DSS form is a **legacy AngularJS
app rendered inside nested iframes**, and the Journal Entry is a **Kendo UI grid**.
There is no API — everything is DOM manipulation through Playwright + injected JS.

### 3.1 Navigation to the JE grid (`go_to_daily_sales_summary`)

```
1. page.goto(DSS_URL); wait 15s   ← legacy iframe needs a long settle
   DSS_URL = .../react/sales-and-forecasting/legacy/DailySalesSummary
2. Find the DSS list iframe:  next(f for f in page.frames if "DailySalesSummariesGrid" in f.url)
3. Set the date filter (the 5th text input in that frame):
       dss_frame.locator('input[type="text"]').nth(4)
       .click(click_count=3)   ← select existing text
       .fill("M/D/YYYY")        ← note %-m/%-d/%Y, no leading zeros
       .press("Tab")
4. Click the location's Name cell (5th column) on the row whose 3rd column
   matches the (override-resolved) location name:
       table tbody tr:has(td:nth-child(3):has-text("<name>")) td:nth-child(5)
5. The entity opens in a NEW TAB (usually) — poll context.pages for up to 15s;
   fall back to same-tab if the URL turns into DailySalesSummaryForm / #/form.
6. Click the Journal Entry tab (searched across all frames of the active page):
       li[role="tab"] span.k-link  (filtered has_text "Journal Entry")
7. Screenshot "before", then call fill_journal_entry().
```

> **Gotcha — the JE lives in one of many frames.** `fill_journal_entry()` finds the
> correct frame by looking for a known label *and* checking the frame is actually
> visible (R365 keeps hidden duplicate frames around):
> ```python
> if f.locator('td:has-text("Food Delivery Sales")').count() > 0:
>     visible = f.evaluate("() => document.documentElement.offsetHeight > 0 && "
>                          "getComputedStyle(document.documentElement).visibility !== 'hidden'")
> ```

### 3.2 Locating and reading a grid cell

Rows are matched by **account text in the 2nd `<td>`** (`td[1]`), with any
`<select>` dropdowns cloned-out first so their option text doesn't pollute the
match. Columns are fixed: **`td[2]` = Debit, `td[3]` = Credit**, `td[4]` = Comment,
`td[5]` = Location.

```js
const scope = document.querySelector('#DSSJournalEntryGrid') || document;
const rows  = [...scope.querySelectorAll('tr[role="row"]')];
const row = rows.find(r => {
    const tds = r.querySelectorAll('td');
    if (tds.length < 4) return false;
    const clone = tds[1].cloneNode(true);
    clone.querySelectorAll('select').forEach(s => s.remove());  // strip dropdowns
    return clone.textContent.trim().includes(<account_text>);
});
```

`_read_je_cell_value()` reads `td[col].textContent`, strips everything but
`[0-9.-]`, and returns a rounded float (default `0.0`).

### 3.3 Writing into an existing cell (`_fill_je_cell`)

Two-phase: **(a)** inject a JS `MouseEvent('click')` on the target `td` to put the
Kendo grid into inline-edit mode; **(b)** Playwright-fill the resulting input:

```python
col_idx = 3 if field_name == "credit" else 2
# ...JS clicks row's td[col_idx], dispatching a bubbling MouseEvent...
frame.wait_for_timeout(800)                     # let Kendo open the editor
inp = frame.locator(f'input[name="{field_name}"]').first   # name = "debit"/"credit"
inp.fill(f"{value:.2f}")
inp.press("Tab")                                # Kendo commits on blur/Tab
```

The `no_comment=True` variant additionally requires the row's comment cell
(`td[4]`) to be empty — this is how we target the *plain* `4500-01 Discounts` row
without clobbering the pre-filled "Gift Card Redeemed" / named discount rows.

### 3.4 Reconcile-not-overwrite

The core write strategy is **read first, write only on mismatch** (`_reconcile`),
so re-running a partially-done day is idempotent and we never zero-out values R365
legitimately pre-filled:

```python
def _reconcile(account, field, revel_val):
    r365_val  = _read_je_cell_value(je_frame, account, field)
    revel_val = round(float(revel_val or 0), 2)
    if r365_val == revel_val:
        log.info("MATCH ...")                       # no write
    elif revel_val:
        _fill_je_cell(je_frame, account, field, revel_val)
```

### 3.5 Adding a brand-new row (combobox flow)

Some accounts aren't pre-rendered and must be **added** via the grid's "Select
Account" combobox. The interaction sequence is delicate — keyboard-driven because
the Kendo combobox + grid editor don't respond reliably to `.fill()` on the value
cells. The pattern (used for `4000-011`, `4000-07`, `8000-06`):

```python
acct_input = je_frame.locator('input[placeholder="Select Account"]').first
acct_input.scroll_into_view_if_needed()
acct_input.click()
acct_input.fill("8000-06")          # type the account code
je_frame.wait_for_timeout(1200)     # wait for the dropdown to filter
acct_input.press("Enter")           # select the highlighted account
je_frame.wait_for_timeout(500)

acct_input.press("Tab")             # focus moves → Debit field
# To land on CREDIT instead of Debit, press Tab once more to skip Debit:
if sign == "credit":
    active.keyboard.press("Tab")
active.keyboard.type(f"{value:.2f}")    # type into Debit/Credit
je_frame.wait_for_timeout(300)

# Tab across remaining money cell(s) to reach Comment, then type it:
if sign == "debit":
    active.keyboard.press("Tab")    # skip Credit
active.keyboard.press("Tab")        # → Comment
active.keyboard.type("Variance")

je_frame.locator('.k-grid-toolbar button:has-text("Add")').click()   # commit the row
je_frame.wait_for_timeout(1000)
```

Key details:
- **Tab budget = column position.** Debit is one Tab from the account input;
  Credit is two; Comment is one past the last money column. Miscounting Tabs is the
  usual cause of "amount landed in the wrong column."
- `active.keyboard` (the **page**, not the frame) is used for typing into the open
  editor — the editor steals focus at page level.
- The **Add button is scoped to `.k-grid-toolbar`** to avoid hitting other Add
  buttons on the form.
- Before adding, the code **checks the row doesn't already exist** (`_read_je_cell_value`
  + a row-count query) to avoid creating duplicates on re-runs.

> **Cash Over/Short note:** R365 auto-fills a *read-only* Over/Short row at load.
> We don't touch it — we **add a separate `8000-06` row** with comment "Variance"
> so the JE balances. Sign: shortage (`net_account_for > total_payments`) → Debit;
> overage → Credit. See `_extract_revel_values()` in `server.py`.

### 3.6 Discount reconciliation (the tricky one)

Discounts span several accounts (`4500-01/-02/-03/-04`, `5000-17`) and R365
pre-fills some. The algorithm:

1. Write the buckets we own first — `4500-02 Comps` and `5000-17 Employee
   Discount` — then **poll up to 10s** for Employee Discount to settle to target.
2. Read **all** discount-account rows with `debit > 0` from the grid.
3. `_verify_in_revel()` checks each R365 value exists in Revel's `discounts_data`
   either directly or as a **sum of up to 5 rows** (`itertools.combinations`).
4. Compare `revel_discounts_total` vs the summed R365 total → `discount_variance`.
5. Only if variance ≠ 0, write the remainder into the **plain** `4500-01` row
   (`no_comment=True`).

### 3.7 Saving + balance check

```python
je_frame.keyboard.press("Escape")     # commit any open inline editor (Kendo commits on Esc)
je_diff = _screenshot_je_grid(...)     # extracts all rows → renders own HTML table → screenshots
# Save = a <li>, NOT a button:
document.querySelector('[data-testid="saveMenuItem"]').click()
```

`_screenshot_je_grid()` doesn't screenshot the live grid (rows can be clipped by
its scroll container). It **extracts every row's data via JS, builds a standalone
HTML table, opens it as a `data:` URL in a throwaway page, and screenshots that** —
guaranteeing all rows + the footer totals + a computed "Difference (Debit −
Credit)" row are visible. The returned `je_diff` (debit total − credit total) is
the balance check; `0.00` = balanced. Per project policy an **unbalanced JE is
treated as failed**, not success.

---

## 4. How Attachments Work in R365 (`_upload_attachment`)

The source Revel `.xlsx` is attached to the DSS form *before saving* so it persists
with the JE. This is the second-trickiest piece because R365's uploader fights
synthetic events. Return values: `uploaded` / `already_present` / `failed` /
`skipped`.

### The DOM

```html
<button id="attachmentsModuleInputButton"
        ng-click="AWS_S3_Uploader.openFileDialog($event)">
<div class="hidediv">
  <input type="file" id="attachmentsModuleInput"
         r365-custom-on-change="attachmentsModuleInputChangeHandler(e)">
</div>
```

### Gotcha 1 — duplicate IDs

The form contains **two** `#attachmentsModuleInput` elements: the real DSS
attachments input (inside `#KendoSplitter`) and a "Ribbon Logo" uploader (inside
`<r365-amazon-uploader>`). A bare `getElementById` / `#attachmentsModuleInput`
locator hits the wrong one. Always resolve the real one:

```js
document.querySelector('#KendoSplitter #attachmentsModuleInput')
  || [...document.querySelectorAll('#attachmentsModuleInput')]
       .filter(i => !i.closest('r365-amazon-uploader')).pop()
  || document.getElementById('attachmentsModuleInput')
```

The element is resolved **once** to an `evaluate_handle` so `set_input_files`, the
change event, and verification all act on the *same* element (they used to diverge,
leaving the verified input with `files.length == 0`).

### Gotcha 2 — synthetic change events are dropped

`set_input_files()` delivers the File, but R365's handler refuses to push it into
`AWS_S3_Uploader` because the synthetic `change` event **isn't trusted**. The fix
is to reproduce a real user gesture — click the input to open the native chooser
and let Playwright fulfil it, producing a **trusted** change event:

```python
try:
    with active.expect_file_chooser(timeout=5_000) as fc_info:
        frame.evaluate("(inp) => inp.click()", el)   # opens native chooser
    fc_info.value.set_files(attachment_path)         # trusted change event
    via_chooser = True
except Exception:
    el.set_input_files(attachment_path)              # fallback
```

### Gotcha 3 — flushing AngularJS

On the **fallback** path, re-dispatch `change`, call
`attachmentsModuleInputChangeHandler({target: inp})` on the element's Angular
scope, then `scope.$apply()` (guarding against `$$phase` digest-in-progress). On
the trusted-chooser path the directive already ran, so only `$apply()` is needed.

### Verification

R365 moved attachment state **off `window`** onto the Angular scope. The code polls
up to **45 seconds**, checking, in order:
- `scope.AWS_S3_Uploader.files` contains the filename, or
- `scope.uploadedAttachments` contains it, or
- it appears in the DOM (`#attachmentsModule` / `.col-md-12` innerText).

On success/failure `_screenshot_attachments()` captures the attachments area as
visual proof. (An `already_present` short-circuit avoids re-uploading on re-runs.)

---

## 5. R365 Report Viewer (`r365/report_viewer.py`)

Used by the **Receivable Reconciliation** flow (separate from the DSS JE write).
`open_report_viewer()` drives R365's Report Viewer to configure and download a
report:

- **Customize panel** (`_find_customize_ctx`) — finds the right frame/context for
  the customization controls.
- **Datepickers** (`_set_datepicker`) — fills by placeholder text.
- **Button groups** (`_click_button_group`) — toggles option buttons by label.
- **Legal entity** (`_select_legal_entity`) — selects the entity in the picker.
- **Download capture** — listens for both `download` and `popup` events
  (`_on_download` / `_on_popup`) because the export can arrive either way; the
  captured file is served back through the Flask `/downloads/` route.

Progress is streamed to the UI via an `_emit(message, screenshot)` callback (SSE).

---

## 6. Flask API & Value Extraction (`server.py`)

### Key routes

| Route | Purpose |
|---|---|
| `POST /api/fetch` | Fetch Revel data for selected establishments/date; streams progress via **SSE** |
| `POST /api/r365/navigate` | Write one establishment's JE into R365 |
| `POST /api/r365/reconcile-all` | Loop all establishments → fetch + write (SSE) |
| `POST /api/r365/report-viewer` | Drive the Report Viewer download (SSE) |
| `GET  /api/dss-runs` | Run history, **server-side paginated** |
| `GET  /api/establishments` | Location list for the UI |
| `GET  /screenshots/… /logs/… /downloads/…` | Serve generated artifacts |

All long-running routes use **Server-Sent Events** — a background thread runs the
work and pushes progress events that a `stream()` generator yields to the browser.

### `_extract_revel_values()` — the Revel JSON → JE field translator

Turns the raw operations-report JSON into the dict `fill_journal_entry()` expects.
The non-obvious calculations (the *full* account→field table is in the
[README](README.md#field-calculations-revel--r365)):

- **Food Sales** = `taxable_sales + untaxable_sales` of the `"1. Food"` product
  class; **untaxed net sales** (`sales_data.net_sales_untaxed`) is folded in and
  also added as a separate `4000-011` row.
- **Credit Cards AR** = `credit_total + credit_tips_total`.
- **Marketplace** (UberEats/DoorDash/GrubHub) — matched by name out of
  `sales_data.custom_payments`.
- **Discounts** — split into buckets by reason: `manager 100%` → Comps;
  `employee*` → Employee Discount; Loyalty group total → Promotions; the Standard
  remainder → `4500-01`.
- **Cash Over/Short** = `abs(net_account_for − total_payments)`; sign credit if
  payments exceeded expected, else debit.

### Run history (`db.py` / `dss_runs.db`)

Every reconciliation is logged to SQLite via `_record_run()` →
`db.record_run(...)`, wrapped so a logging failure never breaks a run. Surfaced at
`/dss-runs` with server-side pagination.

---

## 7. Operational Gotchas Checklist

- **Restart the server after any Python edit.** Flask runs as a long-lived
  background process; edits don't hot-reload — kill and relaunch (`start_server.sh`).
- **Revel:** always `_switch_establishment` before fetching — the `establishment`
  query param is ignored.
- **R365 env vars:** `R65_USER` / `R65_PASS` (no `3`), `R365_URL` (with `3`).
- **JE columns are positional:** `td[2]`=Debit, `td[3]`=Credit. When adding rows,
  the Tab count determines which column you land in.
- **Attachments:** resolve the `#KendoSplitter` input, prefer the native file
  chooser (trusted event), allow up to 45s to land.
- **Balance:** `je_diff != 0` ⇒ run is **failed**, regardless of what was written.
- **Name overrides:** locations 48/7/20 differ between Revel and the R365 DSS grid.

---

*See the [README](README.md) for setup, the high-level flow diagram, the
establishment table, and the complete Revel→R365 field-calculation reference.*
