"""
Navigate R365: My Reports → Accounting tab → GL Account Detail Export Customize
→ set Account to 1245-12 A/R-UberEats.
"""

import logging
import time
import uuid
from datetime import date as date_type
from pathlib import Path

from playwright.sync_api import sync_playwright, Frame

from .session import PROFILE_DIR, ensure_logged_in_r365

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"

log = logging.getLogger(__name__)

MY_REPORTS_URL = "https://ayg.restaurant365.com/react/reports-management/legacy/MyReports"
TARGET_REPORT  = "GL Account Detail Export"
BTN_ID         = f"customizeViewer-{TARGET_REPORT}"


_EXCLUDE_FRAME_DOMAINS = (
    "document360.io",     # Help widget
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "fullstory.com",
    "intercom.io",
    "hotjar.com",
)


def _find_customize_ctx(page):
    """Search main page + all frames for the Customize button.
    Returns (ctx, info_dict). ctx may be a Page or Frame; None if nothing found."""
    contexts = [("main", page)]
    for f in page.frames:
        if f == page.main_frame:
            continue
        url = f.url or ""
        if any(dom in url for dom in _EXCLUDE_FRAME_DOMAINS):
            continue
        contexts.append((url[:80], f))

    for label, ctx in contexts:
        try:
            r = ctx.evaluate(f"""
                () => {{
                    const byId = !!document.getElementById({repr(BTN_ID)});
                    const btns = Array.from(document.querySelectorAll('button'))
                        .filter(b => b.textContent.trim() === 'Customize').length;
                    const links = Array.from(document.querySelectorAll('a, [role="button"]'))
                        .filter(e => e.textContent.trim() === 'Customize').length;
                    return {{ byId, btns, links }};
                }}
            """)
            if r.get("byId") or r.get("btns", 0) > 0 or r.get("links", 0) > 0:
                log.info("Customize found in [%s]: %s", label, r)
                return ctx, r
        except Exception as ex:
            log.debug("evaluate failed for [%s]: %s", label, str(ex)[:60])
    return None, {}


def _set_datepicker(ctx, page, placeholder: str, date_str: str) -> str:
    """Fill an AngularJS Material datepicker input (format: M/D/YYYY)."""
    try:
        inp = ctx.locator(f'input.md-datepicker-input[placeholder="{placeholder}"]')
        inp.click(timeout=5_000)
        inp.select_text()
        inp.type(date_str, delay=50)
        page.keyboard.press("Tab")
        page.wait_for_timeout(600)
        return f"set-{placeholder}-to-{date_str}"
    except Exception as e:
        log.warning("Datepicker [%s] failed: %s", placeholder, e)
        return f"failed: {e}"


def _click_button_group(ctx, label_text: str, option_text: str) -> str:
    """Click a specific option inside an AngularJS ButtonGroup by its label span text."""
    result = ctx.evaluate(f"""
        () => {{
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const labelSpan = spans.find(s => s.textContent.trim() === {repr(label_text)});
            if (!labelSpan) return 'label-not-found: ' + {repr(label_text)};
            let parent = labelSpan;
            for (let i = 0; i < 10; i++) {{
                parent = parent.parentElement;
                if (!parent) return 'parent-exhausted';
                const btns = Array.from(parent.querySelectorAll('button.groupX'));
                if (btns.length === 0) continue;
                const target = btns.find(b => b.textContent.trim() === {repr(option_text)});
                if (target) {{ target.click(); return 'clicked: ' + {repr(option_text)}; }}
                return 'option-not-found: ' + {repr(option_text)} + ' in [' + btns.map(b => b.textContent.trim()).join(', ') + ']';
            }}
            return 'not-found';
        }}
    """)
    log.info("ButtonGroup [%s=%s]: %s", label_text, option_text, result)
    return result


def _snap(page, prefix: str) -> str:
    """Take a screenshot, save to /tmp, return filename (empty string on failure)."""
    name = f"{prefix}_{uuid.uuid4().hex[:6]}.png"
    try:
        page.screenshot(path=f"/tmp/{name}", full_page=False)
    except Exception as e:
        log.warning("Screenshot [%s] failed: %s", prefix, e)
        return ""
    return name


def open_report_viewer(
    legal_entity: str = "LCF Airtex LLC",
    start_date: date_type | None = None,
    end_date: date_type | None = None,
    show_unapproved: str = "Yes",
    calendar: str = "Fiscal",
    progress_cb=None,
) -> dict:
    def _emit(message: str, screenshot: str = ""):
        log.info("[rv] %s", message)
        if progress_cb:
            progress_cb(message, screenshot or None)

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
            accept_downloads=True,
        )
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            ensure_logged_in_r365(page, browser)
            _emit("Logged into R365")

            # ── Step 1: wait for React sidebar ───────────────────────────────
            log.info("Waiting for React sidebar…")
            for i in range(30):
                page.wait_for_timeout(1_000)
                if "/react/" in page.url:
                    has_nav = page.evaluate(
                        "() => !!document.querySelector('aside, nav, [class*=\"sidebar\"]')"
                    )
                    if has_nav:
                        log.info("Sidebar ready after %ds at %s", i + 1, page.url)
                        break

            page.wait_for_timeout(2_000)

            # ── Step 2: navigate via sidebar link (client-side React Router) ─
            log.info("Clicking Reports sidebar link")
            nav_result = page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const byHref = links.find(a =>
                        (a.href || '').includes('reports-management') ||
                        (a.href || '').includes('MyReports')
                    );
                    if (byHref) { byHref.click(); return 'href: ' + byHref.href; }
                    const byLabel = document.querySelector('[aria-label="Reports"] a, a[aria-label="Reports"]');
                    if (byLabel) { byLabel.click(); return 'aria-label'; }
                    return 'not-found';
                }
            """)
            log.info("Sidebar nav: %s", nav_result)

            if "not-found" in str(nav_result):
                log.warning("Sidebar link not found — falling back to direct goto")
                try:
                    page.goto(MY_REPORTS_URL, timeout=60_000, wait_until="commit")
                except Exception as e:
                    log.warning("goto warning: %s", e)
            else:
                # React Router navigation is client-side — wait for URL to settle
                # rather than triggering a hard goto (which breaks AngularJS bootstrap)
                for _ in range(20):
                    page.wait_for_timeout(500)
                    if "MyReports" in page.url or "reports-management" in page.url:
                        break
                if "MyReports" not in page.url and "reports-management" not in page.url:
                    log.warning("URL didn't change after sidebar click (at %s) — waiting longer", page.url)
                    page.wait_for_timeout(3_000)

            log.info("At: %s", page.url)
            page.wait_for_timeout(5_000)

            # ── Step 3: log all frames so we can see what's available ────────
            log.info("Total frames: %d", len(page.frames))
            for f in page.frames:
                log.info("  frame: %s", (f.url or "<blank>")[:120])

            # ── Step 3a: click Accounting TAB (inside Reports page, NOT sidebar) ──
            # The Reports page has an AngularJS Material tab bar:
            #   <md-tabs>...<li aria-label="Accounting" name="Accounting">
            # We MUST avoid clicking the sidebar "Accounting" nav item which
            # navigates to /react/accounting/legacy/AllTransactions.
            log.info("Waiting for Accounting TAB (md-tabs nav, NOT sidebar)…")
            tab_clicked = False
            for i in range(60):
                page.wait_for_timeout(1_000)
                contexts = [("main", page)] + [
                    ((f.url or "<blank>")[:60], f) for f in page.frames
                    if f != page.main_frame
                    and not any(d in (f.url or "") for d in _EXCLUDE_FRAME_DOMAINS)
                ]
                for label, c in contexts:
                    try:
                        r = c.evaluate("""
                            () => {
                                // Strict selectors: AngularJS Material md-tabs only.
                                // These never match the React sidebar.
                                const candidates = [
                                    'md-tabs li[name="Accounting"] a',
                                    'md-tabs li[aria-label="Accounting"] a',
                                    'md-tabs-canvas li[name="Accounting"] a',
                                    'md-tabs-canvas li[aria-label="Accounting"] a',
                                    'li[name="Accounting"][role="tab"] a',
                                    'li[name="Accounting"]._md-nav-item a',
                                    '[role="tablist"] li[name="Accounting"] a',
                                    'ul._md-nav-bar li[name="Accounting"] a',
                                ];
                                for (const sel of candidates) {
                                    const el = document.querySelector(sel);
                                    if (el && el.offsetParent !== null) {
                                        el.scrollIntoView({block:'center'});
                                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                        el.click();
                                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                        return 'clicked: ' + sel;
                                    }
                                }
                                // Restricted fallback: only li[name="Accounting"] anywhere — never <a> alone.
                                // This still excludes the React sidebar (which uses <a> directly).
                                const li = document.querySelector('li[name="Accounting"], li[aria-label="Accounting"]');
                                if (li && li.offsetParent !== null) {
                                    const a = li.querySelector('a') || li;
                                    a.scrollIntoView({block:'center'});
                                    a.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                    a.click();
                                    a.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                    return 'clicked-li: ' + (li.outerHTML || '').slice(0, 100);
                                }
                                return null;
                            }
                        """)
                        if r:
                            log.info("Accounting TAB clicked in [%s]: %s", label, r)
                            tab_clicked = True
                            break
                    except Exception:
                        pass
                if tab_clicked:
                    break
                if i % 5 == 0:
                    log.info("Accounting TAB not yet visible (%ds) — waiting for AngularJS tabs to render", i + 1)

            if not tab_clicked:
                log.warning("Accounting TAB never appeared — proceeding without clicking (sidebar would navigate away, avoided)")
            else:
                page.wait_for_timeout(5_000)  # let the tab content render
                ts = _snap(page, "after_acct")
                log.info("After Accounting TAB screenshot: %s", ts)
                _emit("Opened Accounting tab in Report Viewer", ts)
                # Sanity check: URL must still be MyReports — if it changed to /accounting/legacy/, we hit the sidebar
                if "MyReports" not in page.url and "reports-management" not in page.url:
                    log.error("URL changed away from Reports after tab click! Now at: %s", page.url)
                    log.info("Navigating back to My Reports…")
                    try:
                        page.go_back(timeout=10_000)
                        page.wait_for_timeout(3_000)
                    except Exception:
                        pass

            # ── Step 4: poll up to 90s, searching ALL frames (excl. help widgets) ──
            log.info("Polling for Customize button for '%s'…", TARGET_REPORT)
            ctx = page  # default
            found = False
            for i in range(90):
                page.wait_for_timeout(1_000)
                found_ctx, info = _find_customize_ctx(page)
                if found_ctx is not None:
                    ctx = found_ctx
                    log.info("Customize found after %ds: %s", i + 1, info)
                    found = True
                    break
                if i % 5 == 0:
                    log.info("Poll %ds: still searching… (%d frames)", i + 1, len(page.frames))
                if i % 15 == 0 and i > 0:
                    snap = f"poll_{i}s_{uuid.uuid4().hex[:4]}.png"
                    page.screenshot(path=f"/tmp/{snap}", full_page=False)
                    log.info("Poll screenshot: %s", snap)

            if not found:
                dbg = _snap(page, "debug")
                return {"error": "Customize button never appeared", "screenshot_filename": dbg}

            _emit("GL Account Detail Export dialog opened — configuring filters…")

            # ── Step 5: click Accounting tab if GL Account Detail Export not yet visible ──
            # Try to click Customize for GL Account Detail Export directly first.
            # Only fall back to Accounting tab if it's not on the current view.
            log.info("Attempting to click Customize for '%s'", TARGET_REPORT)
            clicked = ctx.evaluate(f"""
                () => {{
                    // Try by ID first
                    const byId = document.getElementById({repr(BTN_ID)});
                    if (byId) {{ byId.click(); return 'clicked-by-id'; }}

                    // Find the report row and click its Customize button
                    const reportEls = Array.from(document.querySelectorAll('*')).filter(el =>
                        el.textContent.trim() === {repr(TARGET_REPORT)} && el.children.length === 0
                    );
                    for (const el of reportEls) {{
                        let node = el;
                        for (let j = 0; j < 6; j++) {{
                            node = node.parentElement;
                            if (!node) break;
                            const btn = Array.from(node.querySelectorAll('button'))
                                .find(b => b.textContent.trim() === 'Customize');
                            if (btn) {{ btn.click(); return 'clicked-near-report'; }}
                        }}
                    }}
                    return 'not-found-direct';
                }}
            """)
            log.info("Direct Customize click: %s", clicked)

            if "not-found" in str(clicked):
                # Need to switch to Accounting tab first
                log.info("Clicking Accounting tab to find report")
                tab = ctx.evaluate("""
                    () => {
                        // Confirmed selector from live HTML: li[name="Accounting"] a
                        // AngularJS Material tabs use li[name] and li[aria-label]
                        const exact = document.querySelector(
                            'li[name="Accounting"] a, li[aria-label="Accounting"] a'
                        );
                        if (exact) {
                            exact.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            exact.click();
                            exact.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            return 'clicked-exact: ' + exact.className;
                        }
                        // Fallback: find by trimmed text content (HTML has surrounding whitespace)
                        const byText = Array.from(document.querySelectorAll('a, button, li'))
                            .find(e => e.textContent.trim() === 'Accounting' && e.offsetParent !== null);
                        if (byText) {
                            byText.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            byText.click();
                            byText.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            return 'clicked-text: ' + byText.tagName + ' ' + byText.className;
                        }
                        return 'not-found';
                    }
                """)
                log.info("Accounting tab: %s", tab)
                page.wait_for_timeout(4_000)

                # Re-poll for Customize after tab switch
                log.info("Waiting for Customize after Accounting tab switch…")
                for i in range(30):
                    page.wait_for_timeout(1_000)
                    ctx = _get_legacy_frame(page) or page
                    try:
                        present = ctx.evaluate(f"""
                            () => {{
                                const byId = document.getElementById({repr(BTN_ID)});
                                const anyCustomize = Array.from(document.querySelectorAll('button'))
                                    .filter(b => b.textContent.trim() === 'Customize').length;
                                return {{ byId: !!byId, anyCustomize }};
                            }}
                        """)
                    except Exception:
                        present = {"byId": False, "anyCustomize": 0}
                    if present.get("byId") or present.get("anyCustomize", 0) > 0:
                        log.info("Customize ready after tab switch (%ds): %s", i + 1, present)
                        break

                clicked = ctx.evaluate(f"""
                    () => {{
                        const btn = document.getElementById({repr(BTN_ID)});
                        if (btn) {{ btn.click(); return 'clicked-by-id'; }}
                        const reportEls = Array.from(document.querySelectorAll('*')).filter(el =>
                            el.textContent.trim() === {repr(TARGET_REPORT)} && el.children.length === 0
                        );
                        for (const el of reportEls) {{
                            let node = el;
                            for (let j = 0; j < 6; j++) {{
                                node = node.parentElement;
                                if (!node) break;
                                const btn2 = Array.from(node.querySelectorAll('button'))
                                    .find(b => b.textContent.trim() === 'Customize');
                                if (btn2) {{ btn2.click(); return 'clicked-near-report'; }}
                            }}
                        }}
                        // Last resort: first Customize button
                        const any = Array.from(document.querySelectorAll('button'))
                            .find(b => b.textContent.trim() === 'Customize');
                        if (any) {{ any.click(); return 'clicked-fallback'; }}
                        return 'not-found';
                    }}
                """)
                log.info("Customize click after tab switch: %s", clicked)

            if clicked == "not-found":
                dbg = _snap(page, "debug")
                return {"error": "Customize click failed", "screenshot_filename": dbg}
            page.wait_for_timeout(3_000)

            # ── Step 8: click ACCOUNT button (not ACCOUNTS AVAILABLE) ─────────
            log.info("Clicking ACCOUNT button")
            acct = ctx.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('button')).find(b => {
                        const t = b.textContent.trim().toUpperCase();
                        return t === 'ACCOUNT' ||
                               (t.includes('ACCOUNT') && !t.includes('ACCOUNTS') && !t.includes('AVAILABLE'));
                    });
                    if (!btn) return 'not-found';
                    btn.scrollIntoView();
                    btn.click();
                    return 'clicked: ' + btn.textContent.trim();
                }
            """)
            log.info("ACCOUNT btn: %s", acct)
            page.wait_for_timeout(3_000)

            # ── Step 8.5: clear all selections via Select All toggle ─────────
            # Click Select All twice: first click selects everything, second
            # click deselects everything. This always ends at "nothing selected"
            # regardless of the initial state (none / partial / all selected).
            log.info("Clearing selections: clicking Select All twice")
            for i in (1, 2):
                res = ctx.evaluate("""
                    () => {
                        const sa = document.querySelector('md-checkbox[ng-model="selectAll"]');
                        if (!sa) return 'select-all-not-found';
                        sa.scrollIntoView({block:'center'});
                        sa.click();
                        return 'select-all-clicked aria-checked=' + sa.getAttribute('aria-checked');
                    }
                """)
                log.info("Select All click %d: %s", i, res)
                page.wait_for_timeout(1_000)

            # ── Step 9: type "1245-12 - A/R-UberEats" in the search input ─────
            SEARCH_TERM = "1245-12 - A/R-UberEats"
            log.info("Finding search input")
            try:
                if ctx != page:
                    inp = ctx.locator('input:not([disabled]):not([type="hidden"])').last
                else:
                    inp = page.locator('input:not([disabled]):not([type="hidden"])').last
                inp.click(timeout=5_000)
                inp.fill("")
                inp.type(SEARCH_TERM, delay=60)
                log.info("Typed %r via frame locator", SEARCH_TERM)
            except Exception as e:
                log.warning("Frame locator input failed (%s) — keyboard fallback", e)
                page.keyboard.type(SEARCH_TERM, delay=60)
            page.wait_for_timeout(2_500)

            # Pre-select screenshot
            pre = _snap(page, "pre_select")
            log.info("Pre-select screenshot: %s", pre)
            _emit("Searching for account 1245-12 A/R-UberEats…", pre)

            # ── Step 10: check the "1245-12 - A/R-UberEats" checkbox ─────────
            # The dialog is AngularJS Material inside the iframe (ctx). Each
            # row has a wrapper <button aria-label="1245-12 - A/R-UberEats"
            # ng-click="wantedItem(item[0], true)"> that is the canonical way
            # to toggle selection — clicking it calls the AngularJS handler
            # which sets `wanted=true` and triggers a digest cycle.
            log.info("Selecting '1245-12 - A/R-UberEats' via AngularJS wantedItem()")

            click_result = ctx.evaluate("""
                () => {
                    // Prefer the wrapper button (canonical AngularJS handler)
                    const btn = document.querySelector(
                        'button[aria-label="1245-12 - A/R-UberEats"]'
                    );
                    if (btn) {
                        btn.scrollIntoView({block:'center'});
                        btn.click();
                        return 'wrapper-button-clicked';
                    }
                    // Fallback: click the md-checkbox directly (note trailing space in aria-label)
                    const cb = document.querySelector(
                        'md-checkbox[aria-label="1245-12 - A/R-UberEats "], ' +
                        'md-checkbox[aria-label="1245-12 - A/R-UberEats"]'
                    );
                    if (cb) {
                        cb.scrollIntoView({block:'center'});
                        cb.click();
                        return 'md-checkbox-clicked aria-checked=' + cb.getAttribute('aria-checked');
                    }
                    return 'no-target-found';
                }
            """)
            log.info("Checkbox click: %s", click_result)
            page.wait_for_timeout(1_500)

            # Verify the checkbox is actually checked now
            verify = ctx.evaluate("""
                () => {
                    const cb = document.querySelector(
                        'md-checkbox[aria-label="1245-12 - A/R-UberEats "], ' +
                        'md-checkbox[aria-label="1245-12 - A/R-UberEats"]'
                    );
                    return cb ? cb.getAttribute('aria-checked') : 'not-found';
                }
            """)
            log.info("Checkbox aria-checked after click: %s", verify)

            # Screenshot after checking — should show checkbox ticked
            check_shot = _snap(page, "checked")
            log.info("After-check screenshot: %s", check_shot)
            _emit("Account 1245-12 A/R-UberEats selected", check_shot)

            # ── Step 11: click OK to confirm selection ───────────────────────
            # Use the exact ng-click selector — there are two buttons with
            # text "OK"/"Cancel" but only one has ng-click="closeDialog('OK')".
            log.info("Clicking OK to confirm (ng-click=closeDialog('OK'))")
            ok_result = ctx.evaluate(r"""
                () => {
                    // Match by ng-click attribute (most reliable)
                    const ok = document.querySelector(
                        "button[ng-click=\"closeDialog('OK')\"]"
                    );
                    if (ok) {
                        ok.scrollIntoView({block:'center'});
                        ok.click();
                        return 'ok-ng-click';
                    }
                    // Fallback: find by exact text inside md-dialog-actions
                    const actions = document.querySelector('md-dialog-actions');
                    if (actions) {
                        const btn = Array.from(actions.querySelectorAll('button'))
                            .find(b => b.textContent.trim().toUpperCase() === 'OK');
                        if (btn) { btn.click(); return 'ok-dialog-actions'; }
                    }
                    return 'ok-not-found';
                }
            """)
            log.info("OK: %s", ok_result)
            page.wait_for_timeout(2_500)

            # ── Step 12: open Legal Entity filter on Customize panel ─────────
            # Try direct "LEGAL ENTITY" button first; fall back to "FILTER BY"
            # → "Legal Entity" option (the option uses ng-click=wantedItem
            # without the `true` flag, so it's a single-select).
            log.info("Opening Legal Entity filter")
            fbtn = ctx.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button'))
                        .filter(b => b.offsetParent !== null);
                    let target = btns.find(b => b.textContent.trim().toUpperCase() === 'LEGAL ENTITY');
                    if (target) { target.scrollIntoView({block:'center'}); target.click(); return 'legal-entity-direct'; }
                    target = btns.find(b => {
                        const t = b.textContent.trim().toUpperCase();
                        return t === 'FILTER BY' || t.startsWith('FILTER BY');
                    });
                    if (target) { target.scrollIntoView({block:'center'}); target.click(); return 'filter-by-clicked'; }
                    return 'not-found';
                }
            """)
            log.info("Filter button: %s", fbtn)
            page.wait_for_timeout(2_000)

            if fbtn == "filter-by-clicked":
                log.info("Clicking 'Legal Entity' option")
                le = ctx.evaluate("""
                    () => {
                        const btn = document.querySelector('button[aria-label="Legal Entity"]');
                        if (!btn) return 'not-found';
                        btn.scrollIntoView({block:'center'});
                        btn.click();
                        return 'legal-entity-clicked';
                    }
                """)
                log.info("Legal Entity option: %s", le)
                page.wait_for_timeout(2_500)

            # ── Step 12c: open the FILTER multi-select dialog ─────────────────
            # Filter By only SETS the filter type; the actual entity selection
            # lives behind a separate "Filter" dropdown (id="Filter" on the
            # Customize panel). We must click it to open the entity dialog.
            log.info("Opening Filter (entity multi-select) dialog")
            open_filter = ctx.evaluate("""
                () => {
                    // Click the button inside the Filter parameter row
                    const filterRow = document.getElementById('Filter');
                    if (filterRow) {
                        const btn = filterRow.querySelector('button');
                        if (btn) {
                            btn.scrollIntoView({block:'center'});
                            btn.click();
                            return 'filter-button-clicked';
                        }
                        // Section fallback (parameterMenuClick)
                        const section = filterRow.querySelector('section[role="button"]');
                        if (section) { section.click(); return 'filter-section-clicked'; }
                    }
                    return 'filter-not-found';
                }
            """)
            log.info("Filter open: %s", open_filter)
            page.wait_for_timeout(2_500)

            # ── Step 13: clear any existing legal entity selections ──────────
            log.info("Clearing legal entity selections (Select All x2)")
            for i in (1, 2):
                res = ctx.evaluate("""
                    () => {
                        const sa = document.querySelector('md-checkbox[ng-model="selectAll"]');
                        if (!sa) return 'select-all-not-found';
                        sa.scrollIntoView({block:'center'});
                        sa.click();
                        return 'select-all-clicked aria-checked=' + sa.getAttribute('aria-checked');
                    }
                """)
                log.info("Entity Select All click %d: %s", i, res)
                page.wait_for_timeout(1_000)

            # ── Step 14: type the chosen legal entity in search input ─────────
            LE_SEARCH = legal_entity
            log.info("Typing %r in entity search", LE_SEARCH)
            try:
                if ctx != page:
                    inp = ctx.locator('input:not([disabled]):not([type="hidden"])').last
                else:
                    inp = page.locator('input:not([disabled]):not([type="hidden"])').last
                inp.click(timeout=5_000)
                inp.fill("")
                inp.type(LE_SEARCH, delay=60)
            except Exception as e:
                log.warning("Entity search input failed (%s) — keyboard fallback", e)
                page.keyboard.type(LE_SEARCH, delay=60)
            page.wait_for_timeout(2_500)

            # ── Step 15: click the entity's wrapper button ───────────────────
            log.info("Selecting %r", legal_entity)
            le_click = ctx.evaluate(f"""
                () => {{
                    const label = {repr(legal_entity)};
                    const btn = document.querySelector(`button[aria-label="${{label}}"]`);
                    if (btn) {{
                        btn.scrollIntoView({{block:'center'}});
                        btn.click();
                        return 'wrapper-button-clicked';
                    }}
                    const cb = document.querySelector(
                        `md-checkbox[aria-label="${{label}} "], ` +
                        `md-checkbox[aria-label="${{label}}"]`
                    );
                    if (cb) {{ cb.scrollIntoView({{block:'center'}}); cb.click(); return 'md-checkbox-clicked'; }}
                    return 'not-found';
                }}
            """)
            log.info("Entity click (%s): %s", legal_entity, le_click)
            page.wait_for_timeout(1_500)

            # ── Step 16: click OK on entity dialog ───────────────────────────
            log.info("Clicking OK on entity dialog")
            le_ok = ctx.evaluate(r"""
                () => {
                    const ok = document.querySelector("button[ng-click=\"closeDialog('OK')\"]");
                    if (ok) { ok.scrollIntoView({block:'center'}); ok.click(); return 'ok-ng-click'; }
                    const actions = document.querySelector('md-dialog-actions');
                    if (actions) {
                        const btn = Array.from(actions.querySelectorAll('button'))
                            .find(b => b.textContent.trim().toUpperCase() === 'OK');
                        if (btn) { btn.click(); return 'ok-dialog-actions'; }
                    }
                    return 'ok-not-found';
                }
            """)
            log.info("Entity OK: %s", le_ok)
            page.wait_for_timeout(2_000)
            _emit(f"Legal entity '{legal_entity}' selected", _snap(page, "entity_ok"))

            # ── Step 17: Set Start / End date range ──────────────────────────
            if start_date:
                s_str = f"{start_date.month}/{start_date.day}/{start_date.year}"
                log.info("Setting Start date to %s", s_str)
                r = _set_datepicker(ctx, page, "Start", s_str)
                log.info("Start date result: %s", r)

            if end_date:
                e_str = f"{end_date.month}/{end_date.day}/{end_date.year}"
                log.info("Setting End date to %s", e_str)
                r = _set_datepicker(ctx, page, "End", e_str)
                log.info("End date result: %s", r)

            # ── Step 18: Set Show Unapproved toggle ──────────────────────────
            log.info("Setting Show Unapproved to %r", show_unapproved)
            _click_button_group(ctx, "Show Unapproved", show_unapproved)
            page.wait_for_timeout(400)

            # ── Step 19: Set Calendar toggle ─────────────────────────────────
            log.info("Setting Calendar to %r", calendar)
            _click_button_group(ctx, "Calendar", calendar)
            page.wait_for_timeout(400)

            filters_shot = _snap(page, "filters_set")
            _emit(
                f"Filters set — dates: {start_date or 'default'} → {end_date or 'default'}, "
                f"Show Unapproved: {show_unapproved}, Calendar: {calendar}",
                filters_shot,
            )

            # ── Steps 20-23: Click RUN directly to open the SSRS ReportViewer
            # in a new tab. That viewer carries the customize-dialog's filter
            # state via its URL hash, and its built-in toolbar has a Save /
            # Export dropdown that triggers a real file download (no NRE).
            new_tabs: list = []
            _on_page = lambda p: new_tabs.append(p)
            browser.on("page", _on_page)

            log.info("Clicking RUN button (customize dialog) to open ReportViewer")
            # Click chevron in both contexts (ctx = AngularJS iframe, where
            # the customize dialog lives; page = outer React doc, where some
            # popups may render via portals). We scope to the customize
            # dialog by walking up from the visible "GL Account Detail Export"
            # title text until we find a runBTN within.
            # Click the customize-dialog's RUN button (rightmost runBTN that
            # sits next to the visible report-title text).
            FIND_AND_CLICK_RUN_JS = """
                (reportTitle) => {
                    function visible(el) {
                        if (!el || !el.offsetParent) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }
                    const titleNodes = Array.from(document.querySelectorAll('*'))
                        .filter(el => visible(el)
                            && el.children.length === 0
                            && el.textContent.trim() === reportTitle);
                    const found = new Set();
                    for (const t of titleNodes) {
                        let cur = t;
                        for (let i = 0; i < 15 && cur; i++) {
                            const rb = cur.querySelector
                                && cur.querySelector('button.runBTN');
                            if (rb && visible(rb)) { found.add(rb); break; }
                            cur = cur.parentElement;
                        }
                    }
                    const runs = [...found].sort((a,b) =>
                        b.getBoundingClientRect().x - a.getBoundingClientRect().x
                    );
                    const runBtn = runs[0];
                    if (!runBtn) return JSON.stringify({
                        result: 'run-not-found',
                        titleCount: titleNodes.length,
                    });
                    runBtn.scrollIntoView({block:'center'});
                    runBtn.click();
                    return JSON.stringify({
                        result: 'run-clicked',
                        ngClick: runBtn.getAttribute('ng-click') || '',
                    });
                }
            """
            run_result = ctx.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
            log.info("Run button (ctx): %s", run_result)
            if "run-not-found" in (run_result or ""):
                run_result = page.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                log.info("Run button (page fallback): %s", run_result)

            _emit("Clicked RUN — waiting for ReportViewer tab…",
                  _snap(page, "run_clicked"))

            # ── Wait for the ReportViewer tab the RUN button spawns ─────────
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            download_filename = None
            captured_download = {"dl": None}

            def _on_download(dl):
                if captured_download["dl"] is None:
                    captured_download["dl"] = dl
                    log.info("Download event captured: %s",
                             dl.suggested_filename)

            popup_pages: list = []

            def _on_popup(p):
                popup_pages.append(p)
                log.info("Popup page opened: %s", p.url[:120])
                try:
                    p.on("download", _on_download)
                except Exception:
                    pass

            browser.on("page", _on_popup)
            browser.on("download", _on_download)
            try:
                page.on("download", _on_download)
            except Exception:
                pass

            # Wait up to 30s for a ReportViewer tab to appear (new tabs list
            # was populated by the _on_page listener registered earlier).
            viewer_page = None
            deadline = time.time() + 30
            while time.time() < deadline and viewer_page is None:
                for p in list(browser.pages):
                    try:
                        if "ReportViewer" in (p.url or ""):
                            viewer_page = p
                            break
                    except Exception:
                        continue
                if viewer_page is None:
                    page.wait_for_timeout(500)

            if viewer_page is None:
                # Fallback: navigate the original page to /#/ReportViewer
                log.warning("No ReportViewer tab appeared — navigating in place")
                page.evaluate(
                    "() => window.location.hash = '#/ReportViewer'"
                )
                page.wait_for_timeout(3_000)
                viewer_page = page

            log.info("ReportViewer: %s", viewer_page.url[:120])
            try:
                viewer_page.bring_to_front()
                viewer_page.on("download", _on_download)
            except Exception:
                pass
            try:
                viewer_page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass

            try:
                browser.remove_listener("page", _on_page)
            except Exception as e:
                log.warning("remove_listener failed (non-fatal): %s", e)

            _emit("Opened ReportViewer — locating Save/Export…",
                  _snap(viewer_page, "report_viewer_loaded"))

            # ── Click the SSRS Save/Export dropdown, then choose Excel ──────
            # R365's ReportViewer renders an SSRS toolbar (often inside an
            # iframe). The export trigger is typically an <input type=image>
            # with title="Export drop down menu" or a button with title/
            # aria-label of "Save" or "Export". After clicking, a dropdown
            # appears containing <a>Excel</a>.
            FIND_AND_CLICK_SAVE_JS = """
                () => {
                    function visible(el) {
                        if (!el || !(el instanceof Element)) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        const cs = getComputedStyle(el);
                        if (cs.visibility === 'hidden' || cs.display === 'none')
                            return false;
                        return true;
                    }
                    const sels = [
                        'input[type="image"][title*="Export" i]',
                        'input[type="image"][title*="Save" i]',
                        'a[title*="Export" i]',
                        'button[title*="Export" i]',
                        'button[aria-label*="Export" i]',
                        '[aria-label="Save"]',
                        '[title="Save"]',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (el && visible(el)) {
                            el.scrollIntoView({block:'center'});
                            el.click();
                            return JSON.stringify({
                                clicked: true, sel,
                                title: el.getAttribute('title') || '',
                                aria: el.getAttribute('aria-label') || '',
                            });
                        }
                    }
                    return JSON.stringify({clicked: false});
                }
            """
            FIND_AND_CLICK_EXCEL_OPTION_JS = """
                () => {
                    function visible(el) {
                        if (!el || !(el instanceof Element)) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        const cs = getComputedStyle(el);
                        if (cs.visibility === 'hidden' || cs.display === 'none')
                            return false;
                        return true;
                    }
                    const cands = Array.from(document.querySelectorAll(
                        'a, button, [role="menuitem"], div'
                    )).filter(el => {
                        if (!visible(el)) return false;
                        const t = (el.textContent || '').trim();
                        return /^excel( workbook)?$/i.test(t)
                            || /excel/i.test(el.getAttribute('title') || '');
                    });
                    if (cands.length === 0) return JSON.stringify({clicked: false});
                    const el = cands[0];
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return JSON.stringify({
                        clicked: true,
                        tag: el.tagName,
                        text: (el.textContent || '').trim().slice(0, 30),
                    });
                }
            """

            def _all_contexts(p):
                """Yield (label, page-or-frame) for every queryable context."""
                yield "page", p
                for fr in p.frames:
                    if fr is not p.main_frame:
                        yield f"frame[{(fr.url or '')[:60]}]", fr

            def _click_in_any(p, js, label):
                for ctx_label, c in _all_contexts(p):
                    try:
                        r = c.evaluate(js)
                        if r and '"clicked":true' in r:
                            log.info("%s clicked in %s: %s", label, ctx_label, r)
                            return r
                    except Exception:
                        continue
                return None

            # Poll up to 20s for Save / Export toolbar trigger.
            save_clicked = None
            deadline = time.time() + 20
            while time.time() < deadline and save_clicked is None:
                save_clicked = _click_in_any(
                    viewer_page, FIND_AND_CLICK_SAVE_JS, "Save/Export"
                )
                if save_clicked is None:
                    viewer_page.wait_for_timeout(500)

            if save_clicked is None:
                log.warning("Save/Export trigger not found in ReportViewer")
                _emit("Save/Export trigger not found",
                      _snap(viewer_page, "save_missing"))
            else:
                viewer_page.wait_for_timeout(700)
                _emit("Save/Export menu opened — selecting Excel…",
                      _snap(viewer_page, "save_opened"))

                # Wrap the Excel-option click in expect_download as a primary
                # path (the click directly triggers the download in SSRS).
                excel_clicked = None
                deadline = time.time() + 10
                while time.time() < deadline and excel_clicked is None:
                    excel_clicked = _click_in_any(
                        viewer_page, FIND_AND_CLICK_EXCEL_OPTION_JS, "Excel"
                    )
                    if excel_clicked is None:
                        viewer_page.wait_for_timeout(500)

                if excel_clicked is None:
                    log.warning("Excel option not found in dropdown")
                    _emit("Excel option not found", _snap(viewer_page, "excel_missing"))
                else:
                    # Wait for context-level download event for up to 90s.
                    deadline = time.time() + 90
                    while time.time() < deadline and captured_download["dl"] is None:
                        viewer_page.wait_for_timeout(500)

                    dl = captured_download["dl"]
                    if dl is not None:
                        download_filename = (
                            dl.suggested_filename
                            or f"gl_export_{uuid.uuid4().hex[:8]}.xlsx"
                        )
                        save_path = DOWNLOADS_DIR / download_filename
                        try:
                            dl.save_as(str(save_path))
                            log.info("Saved download: %s", save_path)
                            _emit(f"Export saved: {download_filename}",
                                  _snap(viewer_page, "after_download"))
                        except Exception as se:
                            log.warning("save_as failed: %s", se)
                    else:
                        log.warning("Excel clicked but no download in 90s "
                                    "(popups seen: %d)", len(popup_pages))
                        _emit("Excel clicked — download not captured",
                              _snap(viewer_page, "after_excel"))

            try:
                browser.remove_listener("download", _on_download)
                browser.remove_listener("page", _on_popup)
            except Exception:
                pass

            screenshot_name = _snap(page, "report_viewer_final")
            log.info("Final screenshot: %s", screenshot_name)
            return {
                "url": page.url,
                "screenshot_filename": screenshot_name,
                "download_filename": download_filename,
            }

        except Exception as exc:
            log.error("Error: %s", exc)
            try:
                dbg = _snap(page, "debug")
                return {"error": str(exc), "screenshot_filename": dbg or None}
            except Exception:
                return {"error": str(exc)}
        finally:
            browser.close()
