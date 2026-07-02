"""
Navigate R365: My Reports → Accounting tab → Balance Sheet → Customize
→ set entity filter, date, and other options, then run and export.
"""

import json
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
TARGET_REPORT  = "Balance Sheet"
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


def _select_entity_via_autocomplete(viewer_page, entity: str) -> None:
    """Change the Legal Entity filter on the ReportViewer customize panel.

    The ReportViewer uses an md-autocomplete (not a modal dialog) for the
    Filter parameter. We click the autocomplete input, clear it, type the
    entity name, wait for the dropdown, then click the matching option.
    """
    log.info("Selecting entity %r via autocomplete", entity)

    # Click the Filter autocomplete input — the label "Filter" (not "Filter By")
    # lives in a div[style*="min-width"] left-side label column.
    click_result = viewer_page.evaluate("""
        () => {
            // Find the Filter label (not Filter By) to locate the right row
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterLabel = spans.find(s =>
                s.textContent.trim() === 'Filter' &&
                s.closest('div[style*="min-width"]')
            );
            if (!filterLabel) return 'label-not-found';
            let node = filterLabel;
            for (let i = 0; i < 10; i++) {
                node = node.parentElement;
                if (!node) break;
                if (node.tagName === 'SECTION') {
                    const inp = node.querySelector('input[type="search"]');
                    if (inp) {
                        inp.click();
                        inp.select();
                        return 'input-clicked:' + inp.id;
                    }
                }
            }
            return 'input-not-found';
        }
    """)
    log.info("Filter autocomplete click: %s", click_result)
    viewer_page.wait_for_timeout(500)

    # Clear the current value and type the new entity
    try:
        inp = viewer_page.locator('input[type="search"]').nth(3)  # input-4 = 4th search input
        inp.click(timeout=3_000)
        inp.select_text()
        inp.fill("")
        inp.type(entity, delay=60)
        log.info("Typed %r into filter autocomplete", entity)
    except Exception as e:
        log.warning("Autocomplete locator failed (%s) — keyboard fallback", e)
        viewer_page.keyboard.select_all()
        viewer_page.keyboard.type(entity, delay=60)
    viewer_page.wait_for_timeout(2_000)

    # Click the matching item in the autocomplete dropdown
    select_result = viewer_page.evaluate(f"""
        () => {{
            const label = {repr(entity)};
            // md-autocomplete dropdown items
            const candidates = [
                ...document.querySelectorAll('li[md-autocomplete-list-item], md-autocomplete-parent-scope li'),
                ...document.querySelectorAll('ul[role="presentation"] li'),
            ];
            const match = candidates.find(li => li.textContent.trim().includes(label));
            if (match) {{ match.click(); return 'dropdown-item-clicked: ' + match.textContent.trim().slice(0,50); }}
            // Broader fallback: any visible li containing the entity text
            const anyLi = Array.from(document.querySelectorAll('li')).find(li =>
                li.offsetParent !== null && li.textContent.trim().includes(label)
            );
            if (anyLi) {{ anyLi.click(); return 'li-fallback: ' + anyLi.textContent.trim().slice(0,50); }}
            return 'not-found';
        }}
    """)
    log.info("Entity autocomplete select (%s): %s", entity, select_result)
    viewer_page.wait_for_timeout(1_500)

    # Verify the filter input now shows the selected entity
    verify = viewer_page.evaluate(f"""
        () => {{
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterLabel = spans.find(s =>
                s.textContent.trim() === 'Filter' &&
                s.closest('div[style*="min-width"]')
            );
            if (!filterLabel) return 'label-not-found';
            let node = filterLabel;
            for (let i = 0; i < 10; i++) {{
                node = node.parentElement;
                if (!node) break;
                if (node.tagName === 'SECTION') {{
                    const inp = node.querySelector('input[type="search"]');
                    return inp ? 'filter-value: ' + inp.value : 'input-not-found';
                }}
            }}
            return 'section-not-found';
        }}
    """)
    log.info("Filter value after select: %s", verify)


def _select_legal_entity(ctx, page, entity: str, skip_filter_type: bool = False) -> None:
    """Open Legal Entity filter dialog, clear existing selections, select entity, confirm OK.

    skip_filter_type: set True when Filter By is already set to 'Legal Entity'
    (e.g. subsequent entities on the ReportViewer page) so we don't re-click
    FILTER BY, which would reset the current filter value to empty.
    """

    if not skip_filter_type:
        # ── Step 12: open Legal Entity filter on Customize panel ─────────
        # Try direct "LEGAL ENTITY" button first; fall back to "FILTER BY"
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
    else:
        log.info("Skipping Filter By step (already set to Legal Entity on viewer page)")

    # ── Step 12c: open the FILTER multi-select dialog ─────────────────
    log.info("Opening Filter (entity multi-select) dialog")
    open_filter = ctx.evaluate("""
        () => {
            // Try by id first
            const filterRow = document.getElementById('Filter');
            if (filterRow) {
                const btn = filterRow.querySelector('button');
                if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); return 'by-id'; }
                const section = filterRow.querySelector('section[role="button"]');
                if (section) { section.scrollIntoView({block:'center'}); section.click(); return 'by-id-section'; }
            }
            // Find the "Filter" label (not "Filter By") via spanTop and walk up
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterSpan = spans.find(s => s.textContent.trim() === 'Filter');
            if (filterSpan) {
                let node = filterSpan;
                for (let i = 0; i < 12; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    // Click the section[role=button] or button in this row
                    const sect = node.querySelector('section[role="button"]');
                    if (sect) { sect.scrollIntoView({block:'center'}); sect.click(); return 'span-section'; }
                    const btn2 = node.querySelector('button');
                    if (btn2) { btn2.scrollIntoView({block:'center'}); btn2.click(); return 'span-btn:' + (btn2.textContent||'').trim().slice(0,30); }
                }
            }
            return 'not-found';
        }
    """)
    log.info("Filter open: %s", open_filter)
    page.wait_for_timeout(2_500)

    # ── Step 13: clear any existing legal entity selections ──────────
    log.info("Clearing legal entity selections")
    cleared = False
    # Try "Select All" checkbox via several attribute selectors
    for sel in [
        'md-checkbox[ng-model="selectAll"]',
        'md-checkbox[ng-model*="selectAll"]',
        'md-checkbox[aria-label="Select All"]',
        'md-checkbox[aria-label*="select all" i]',
    ]:
        for i in range(1, 5):
            page.wait_for_timeout(500)
            state = ctx.evaluate(f"""
                () => {{
                    const sa = document.querySelector({repr(sel)});
                    return sa ? sa.getAttribute('aria-checked') : 'not-found';
                }}
            """)
            log.info("Select All (%s) state %d: %s", sel, i, state)
            if state == "false":
                log.info("All deselected via Select All")
                cleared = True
                break
            if state == "not-found":
                break
            ctx.evaluate(f"""
                () => {{
                    const sa = document.querySelector({repr(sel)});
                    if (!sa) return;
                    sa.scrollIntoView({{block:'center'}});
                    sa.click();
                }}
            """)
            page.wait_for_timeout(1_200)
        if cleared:
            break

    if not cleared:
        # Fallback: individually uncheck every currently-checked md-checkbox
        n = ctx.evaluate("""
            () => {
                const checked = Array.from(
                    document.querySelectorAll('md-checkbox[aria-checked="true"]')
                );
                checked.forEach(cb => { cb.scrollIntoView({block:'center'}); cb.click(); });
                return checked.length;
            }
        """)
        log.info("Fallback: individually unchecked %s items", n)
        page.wait_for_timeout(1_000)

    # ── Step 14: type the chosen legal entity in search input ─────────
    log.info("Typing %r in entity search", entity)
    try:
        if ctx != page:
            inp = ctx.locator('input:not([disabled]):not([type="hidden"])').last
        else:
            inp = page.locator('input:not([disabled]):not([type="hidden"])').last
        inp.click(timeout=5_000)
        inp.fill("")
        inp.type(entity, delay=60)
    except Exception as e:
        log.warning("Entity search input failed (%s) — keyboard fallback", e)
        page.keyboard.type(entity, delay=60)
    page.wait_for_timeout(2_500)

    # ── Step 15: click the entity's wrapper button ───────────────────
    log.info("Selecting %r", entity)
    le_click = ctx.evaluate(f"""
        () => {{
            const label = {repr(entity)};
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
    log.info("Entity click (%s): %s", entity, le_click)
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


def _set_report_type_bs(ctx, page, option: str = "Legal Entity Side by Side") -> str:
    """Click the REPORT TYPE dropdown next to the 'Report Type' label and select option."""
    # Step 1: find the "Report Type" label row and click the clickable element in it
    open_result = ctx.evaluate("""
        () => {
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const label = spans.find(s => s.textContent.trim() === 'Report Type');
            if (!label) return 'label-not-found';
            // Walk up to find the row container, then find the clickable trigger
            let node = label;
            for (let i = 0; i < 12; i++) {
                node = node.parentElement;
                if (!node) break;
                // Look for the dropdown trigger — button, section[role=button], or md-select
                const trigger = node.querySelector(
                    'button, section[role="button"], md-select, [role="combobox"]'
                );
                if (trigger) {
                    trigger.scrollIntoView({block:'center'});
                    trigger.click();
                    return 'opened:' + trigger.tagName + '/' + (trigger.textContent||'').trim().slice(0,40);
                }
            }
            return 'trigger-not-found';
        }
    """)
    log.info("Report Type dropdown open: %s", open_result)
    if "not-found" in open_result:
        return f"report-type-{open_result}"

    page.wait_for_timeout(1_500)

    # Step 2: the Report Type trigger opens an md-dialog modal containing
    # options laid out as rows inside md-virtual-repeat-container. The text
    # itself is a DIV leaf with no click handler — ng-click lives on the
    # wrapper button (which has aria-label=optionText). Pattern mirrors
    # _select_legal_entity. Try button[aria-label] first, then walk up
    # from the matching text leaf to find a clickable ancestor.
    pick = ctx.evaluate(f"""
        (option) => {{
            // Find the open md-dialog. There may be multiple in the DOM —
            // pick the visible one whose title is 'Report Type'.
            const dialogs = Array.from(document.querySelectorAll('md-dialog'))
                .filter(d => d.offsetParent !== null);
            let dialog = dialogs.find(d => {{
                const titles = Array.from(d.querySelectorAll('h1,h2,h3,h4,h5,.md-title,.md-toolbar-tools'));
                return titles.some(t => (t.textContent||'').trim().startsWith('Report Type'));
            }}) || dialogs[dialogs.length - 1] || null;
            if (!dialog) return 'no-dialog';

            // Preferred: wrapper button with aria-label exactly matching option
            const btn = dialog.querySelector('button[aria-label="' + option + '"]');
            if (btn) {{
                btn.scrollIntoView({{block:'center'}});
                btn.click();
                return 'aria-btn';
            }}

            // Fallback: find text leaf inside dialog and walk to clickable ancestor
            const leaves = Array.from(dialog.querySelectorAll('*'))
                .filter(e => e.offsetParent !== null && e.children.length === 0);
            const leaf = leaves.find(e => (e.textContent||'').trim() === option)
                       || leaves.find(e => (e.textContent||'').trim().includes(option));
            if (leaf) {{
                let cur = leaf;
                for (let i = 0; i < 10 && cur; i++) {{
                    if (cur === dialog) break;
                    const tag = cur.tagName;
                    if (tag === 'BUTTON' || tag === 'A' || tag === 'MD-OPTION'
                        || tag === 'MD-MENU-ITEM' || tag === 'MD-LIST-ITEM') {{
                        cur.scrollIntoView({{block:'center'}});
                        cur.click();
                        return 'walked:' + tag;
                    }}
                    if (cur.getAttribute) {{
                        if (cur.getAttribute('ng-click')) {{
                            cur.scrollIntoView({{block:'center'}});
                            cur.click();
                            return 'ng-click:' + tag;
                        }}
                        const role = cur.getAttribute('role');
                        if (role === 'option' || role === 'menuitem' || role === 'button') {{
                            cur.scrollIntoView({{block:'center'}});
                            cur.click();
                            return 'role-' + role + ':' + tag;
                        }}
                    }}
                    cur = cur.parentElement;
                }}
                // Last resort: click the leaf itself
                leaf.scrollIntoView({{block:'center'}});
                leaf.click();
                return 'leaf-click:' + leaf.tagName;
            }}

            // Debug: list options inside dialog
            const items = leaves.map(e => (e.textContent||'').trim())
                .filter(Boolean).slice(0, 30);
            return 'not-found|leaves:' + items.join(' | ');
        }}
    """, option)
    log.info("Report Type option pick: %s", pick)
    page.wait_for_timeout(1_500)

    # Some R365 modal pickers require an explicit OK click to confirm even
    # after the option row is clicked. If the dialog is still open, click OK.
    ok_result = ctx.evaluate("""
        () => {
            const dialogs = Array.from(document.querySelectorAll('md-dialog'))
                .filter(d => d.offsetParent !== null);
            if (dialogs.length === 0) return 'closed';
            const dialog = dialogs[dialogs.length - 1];
            const ok = dialog.querySelector("button[ng-click=\\"closeDialog('OK')\\"]");
            if (ok) { ok.click(); return 'ok-clicked'; }
            const actions = dialog.querySelector('md-dialog-actions');
            if (actions) {
                const btn = Array.from(actions.querySelectorAll('button'))
                    .find(b => b.textContent.trim().toUpperCase() === 'OK');
                if (btn) { btn.click(); return 'ok-actions'; }
            }
            return 'still-open-no-ok';
        }
    """)
    log.info("Report Type dialog after pick: %s", ok_result)
    page.wait_for_timeout(800)
    return pick


def _set_as_of_date(ctx, page, date_str: str) -> str:
    """Set the As Of date field on the Balance Sheet customize panel."""
    # Try md-datepicker-input with placeholder variations
    for placeholder in ("As Of", "as of", "Date", ""):
        try:
            sel = f'input.md-datepicker-input[placeholder="{placeholder}"]' if placeholder else "input.md-datepicker-input"
            inp = ctx.locator(sel).first
            if inp.count() > 0:
                inp.click(timeout=4_000)
                inp.select_text()
                inp.type(date_str, delay=50)
                page.keyboard.press("Tab")
                page.wait_for_timeout(600)
                log.info("As Of date set via placeholder=%r: %s", placeholder, date_str)
                return f"set:{date_str}"
        except Exception:
            pass

    # JS fallback: find As Of label row and set the date input
    result = ctx.evaluate(f"""
        (dateStr) => {{
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const label = spans.find(s => s.textContent.trim() === 'As Of');
            if (!label) return 'label-not-found';
            let node = label;
            for (let i = 0; i < 10; i++) {{
                node = node.parentElement;
                if (!node) break;
                const inp = node.querySelector('input.md-datepicker-input, input[type="text"]');
                if (inp) {{
                    inp.focus();
                    inp.select();
                    inp.value = dateStr;
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return 'js-set:' + dateStr;
                }}
            }}
            return 'input-not-found';
        }}
    """, date_str)
    log.info("As Of date JS result: %s", result)
    return result


def _select_filter_entity(ctx, page, entity: str) -> str:
    """
    Set the Filter (entity) autocomplete field on the Balance Sheet customize panel.
    Works on the initial My Reports customize panel (ctx) and the ReportViewer panel.
    """
    log.info("Setting Filter entity to %r", entity)

    # Click the FILTER button / input to open the dropdown
    open_result = ctx.evaluate("""
        () => {
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterLabel = spans.find(s => s.textContent.trim() === 'Filter');
            if (!filterLabel) return 'label-not-found';
            let node = filterLabel;
            for (let i = 0; i < 10; i++) {
                node = node.parentElement;
                if (!node) break;
                // Click input[type="search"] directly
                const inp = node.querySelector('input[type="search"]');
                if (inp) { inp.click(); return 'input-clicked:' + inp.id; }
                // Or click section[role="button"]
                if (node.tagName === 'SECTION' && node.getAttribute('role') === 'button') {
                    node.click(); return 'section-clicked';
                }
            }
            // Fallback: click any visible FILTER button (not FILTER BY)
            const btns = Array.from(document.querySelectorAll('button'));
            const btn = btns.find(b => {
                const t = b.textContent.trim().toUpperCase();
                return (t === 'FILTER' || t.startsWith('FILTER ')) && !t.includes('BY');
            });
            if (btn) { btn.click(); return 'filter-btn-clicked'; }
            return 'not-found';
        }
    """)
    log.info("Filter open: %s", open_result)
    page.wait_for_timeout(600)

    # Type the entity name into the search input
    try:
        inp = ctx.locator('input[type="search"]').last
        inp.click(timeout=3_000)
        inp.select_text()
        inp.fill("")
        inp.type(entity, delay=60)
    except Exception as e:
        log.warning("Filter input type failed (%s) — keyboard fallback", e)
        page.keyboard.select_all()
        page.keyboard.type(entity, delay=60)
    page.wait_for_timeout(2_000)

    # Click matching autocomplete dropdown item
    pick = ctx.evaluate(f"""
        () => {{
            const needle = {repr(entity)};
            const candidates = [
                ...document.querySelectorAll('li[md-autocomplete-list-item]'),
                ...document.querySelectorAll('md-autocomplete-parent-scope li'),
                ...document.querySelectorAll('ul[role="presentation"] li'),
            ];
            const match = candidates.find(li => li.textContent.trim().includes(needle));
            if (match) {{ match.click(); return 'picked:' + match.textContent.trim().slice(0,60); }}
            const anyLi = Array.from(document.querySelectorAll('li'))
                .find(li => li.offsetParent !== null && li.textContent.trim().includes(needle));
            if (anyLi) {{ anyLi.click(); return 'li-fallback:' + anyLi.textContent.trim().slice(0,60); }}
            return 'not-found';
        }}
    """)
    log.info("Filter entity pick: %s", pick)
    page.wait_for_timeout(1_500)
    return pick


def _click_view_report_and_export(detail_page, entity: str, start_date, end_date, emit_fn=None) -> str | None:
    """Click View Report on the SSRS GL Account Detail page, wait for render, export to Excel."""

    def _all_ctx(page):
        yield page
        for f in page.frames:
            if f is not page.main_frame:
                yield f

    # Click View Report
    for ctx in _all_ctx(detail_page):
        try:
            r = ctx.evaluate("""
                () => {
                    const btn = document.getElementById('ReportViewerControl_ctl04_ctl00');
                    if (btn) { btn.click(); return 'clicked-id'; }
                    const sub = document.querySelector('td.SubmitButtonCell input[type="submit"]');
                    if (sub) { sub.click(); return 'clicked-submit-cell'; }
                    const any = Array.from(document.querySelectorAll('input[type="submit"],button'))
                        .find(b => (b.value||b.textContent||'').trim() === 'View Report');
                    if (any) { any.click(); return 'clicked-fallback'; }
                    return 'not-found';
                }
            """)
            log.info("View Report click: %s", r)
            if r != "not-found":
                break
        except Exception as e:
            log.debug("View Report frame error: %s", e)

    detail_page.wait_for_timeout(2_000)
    try:
        detail_page.wait_for_load_state("networkidle", timeout=60_000)
    except Exception:
        pass
    detail_page.wait_for_timeout(3_000)

    # Build download filename
    entity_slug = entity.replace(" ", "_").replace("/", "-")
    s_str = f"{start_date.month}-{start_date.day}-{start_date.year}" if start_date else "nostart"
    e_str = f"{end_date.month}-{end_date.day}-{end_date.year}" if end_date else "noend"
    filename = f"{entity_slug}_{s_str}_to_{e_str}.xlsx"
    dest = DOWNLOADS_DIR / filename
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with detail_page.expect_download(timeout=60_000) as dl_info:
            # Open the export dropdown
            for ctx in _all_ctx(detail_page):
                try:
                    r = ctx.evaluate("""
                        () => {
                            const btn = document.getElementById(
                                'ReportViewerControl_ctl05_ctl04_ctl00_ButtonLink');
                            if (btn) { btn.click(); return 'dropdown-opened'; }
                            // Fallback: any export/save glyph button
                            const glyph = document.querySelector('.glyphui-save');
                            if (glyph) {
                                const a = glyph.closest('a');
                                if (a) { a.click(); return 'glyph-clicked'; }
                            }
                            return 'not-found';
                        }
                    """)
                    log.info("Export dropdown: %s", r)
                    if r != "not-found":
                        break
                except Exception as e:
                    log.debug("Export dropdown frame error: %s", e)

            detail_page.wait_for_timeout(800)

            # Click the Excel option
            for ctx in _all_ctx(detail_page):
                try:
                    r = ctx.evaluate("""
                        () => {
                            const a = document.querySelector('a[title="Excel"]');
                            if (a) { a.click(); return 'excel-clicked'; }
                            if (typeof $find !== 'undefined') {
                                const rv = $find('ReportViewerControl');
                                if (rv) { rv.exportReport('EXCELOPENXML'); return 'exportReport-called'; }
                            }
                            return 'not-found';
                        }
                    """)
                    log.info("Excel export click: %s", r)
                    if r != "not-found":
                        break
                except Exception as e:
                    log.debug("Excel export frame error: %s", e)

        download = dl_info.value
        download.save_as(dest)
        log.info("Excel saved: %s", dest)
        if emit_fn:
            emit_fn(f"Excel saved: {filename}")
        return str(dest)

    except Exception as e:
        log.warning("Excel export failed: %s", e)
        return None


def _click_drilldown_amount(viewer_page, entity: str, row_label: str = "A/R-UberEats") -> str:
    """
    Inside the SSRS iframe, find the <tr> whose first cell contains row_label,
    then click the <a> link in that row (the drilldown opens target=_blank).
    Returns 'clicked:<amount>' on success or 'not-found' / diagnostic string.
    """
    log.info("Drilling into %r × %r", entity, row_label)

    contexts = [("page", viewer_page)]
    for f in viewer_page.frames:
        if f is not viewer_page.main_frame:
            contexts.append(((f.url or "<blank>")[:80], f))

    js = f"""
        () => {{
            const rowLabel = {repr(row_label)};

            // Find the <tr> whose text starts with the row label
            const rows = Array.from(document.querySelectorAll('tr'));
            const targetRow = rows.find(r => {{
                const txt = (r.textContent || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                return txt.startsWith(rowLabel);
            }});

            if (!targetRow) {{
                // Fallback: any element whose text IS the row label
                const anyEl = Array.from(document.querySelectorAll('span,div,td'))
                    .find(e => (e.textContent||'').trim() === rowLabel);
                if (!anyEl) return 'row-not-found';
                // Walk up to the row, then find the <a> in it
                let node = anyEl;
                for (let i = 0; i < 10 && node; i++) {{
                    if (node.tagName === 'TR') {{
                        const link = node.querySelector('a[href]');
                        if (link) {{
                            link.scrollIntoView({{block:'center'}});
                            link.click();
                            return 'clicked-walkup:' + (link.textContent||'').replace(/\\s+/g,' ').trim().slice(0,40);
                        }}
                        return 'row-found-no-link-in-walkup';
                    }}
                    node = node.parentElement;
                }}
                return 'walkup-exhausted';
            }}

            // Find all <a href> links in this row with non-empty text
            const links = Array.from(targetRow.querySelectorAll('a[href]'))
                .filter(a => (a.textContent||'').replace(/\\u00a0/g,'').trim().length > 0);

            if (links.length === 0) return 'row-found-no-links';

            // If multiple columns, prefer the link whose URL contains the entity GUID or whose
            // column header matches. For now: if only 1 link, click it directly.
            // If >1 links, try to find one associated with the entity column header.
            let chosen = links[0];
            if (links.length > 1) {{
                // Walk up from each link to find column index, then match with header
                const allRows = Array.from(document.querySelectorAll('tr'));
                const headerRow = allRows.find(r => (r.textContent||'').includes({repr(entity)}));
                if (headerRow) {{
                    const hcells = Array.from(headerRow.querySelectorAll('td,th'));
                    const entityIdx = hcells.findIndex(c => (c.textContent||'').trim().includes({repr(entity)}));
                    if (entityIdx >= 0) {{
                        const dcells = Array.from(targetRow.querySelectorAll('td,th'));
                        const cell = dcells[entityIdx];
                        if (cell) {{
                            const cellLink = cell.querySelector('a[href]');
                            if (cellLink) chosen = cellLink;
                        }}
                    }}
                }}
            }}

            chosen.scrollIntoView({{block:'center'}});
            chosen.click();
            return 'clicked:' + (chosen.textContent||'').replace(/\\u00a0/g,'').replace(/\\s+/g,' ').trim().slice(0,40);
        }}
    """

    for label, c in contexts:
        try:
            result = c.evaluate(js)
            log.info("Drilldown probe [%s]: %s", label, result)
            if result and result.startswith("clicked"):
                log.info("Drilldown click succeeded in [%s]: %s", label, result)
                return result
        except Exception as e:
            log.info("Drilldown JS error [%s]: %s", label, e)

    # Nothing clicked — dump diagnostic info
    log.warning("Drilldown: no click succeeded. Dumping diagnostics…")
    for label, c in contexts:
        try:
            dump = c.evaluate(f"""
                () => {{
                    const rowLabel = {repr(row_label)};
                    const rows = Array.from(document.querySelectorAll('tr'));
                    const matching = rows.filter(r =>
                        (r.textContent||'').replace(/\\u00a0/g,' ').replace(/\\s+/g,' ').trim().startsWith(rowLabel)
                    ).map(r => (r.textContent||'').replace(/\\s+/g,' ').trim().slice(0,120));
                    const allLinks = Array.from(document.querySelectorAll('a[href]'))
                        .filter(a => (a.textContent||'').replace(/\\u00a0/g,'').trim().length > 0)
                        .map(a => (a.textContent||'').trim().slice(0,40));
                    const sampleText = Array.from(document.querySelectorAll('span,td,div'))
                        .filter(e => {{const t=(e.textContent||'').trim(); return t.length>1 && t.length<60;}})
                        .map(e => (e.textContent||'').trim())
                        .filter((v,i,a) => a.indexOf(v)===i)
                        .slice(0,60);
                    return {{ matchingRows: matching, allLinks: allLinks.slice(0,20), sampleText: sampleText.slice(0,40) }};
                }}
            """)
            log.warning("Drilldown debug [%s]: matchingRows=%s links=%s sample=%s",
                        label, dump.get("matchingRows"), dump.get("allLinks"), dump.get("sampleText"))
        except Exception as de:
            log.warning("Drilldown debug dump failed [%s]: %s", label, de)

    return "not-found"


def open_report_viewer(
    legal_entities: list = None,
    start_date: date_type | None = None,
    end_date: date_type | None = None,
    show_unapproved: str = "Yes",
    calendar: str = "Fiscal",
    account: str | None = None,
    progress_cb=None,
    entity_cb=None,
) -> dict:
    # Back-compat: allow single string passed positionally
    if isinstance(legal_entities, str):
        legal_entities = [legal_entities]
    if not legal_entities:
        legal_entities = ["LCF Airtex LLC"]

    def _emit(message: str, screenshot: str = ""):
        log.info("[rv] %s", message)
        if progress_cb:
            progress_cb(message, screenshot or None)

    results = []

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

            _emit("Balance Sheet Customize open — configuring options…")

            # ── Step 5: click Accounting tab if Balance Sheet not yet visible ──
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
                    found_ctx, _ = _find_customize_ctx(page)
                    ctx = found_ctx or page
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

            # ── Derive row label from account string ─────────────────────────
            # e.g. "1245-12 - A/R-UberEats" → "A/R-UberEats"
            row_label = "A/R-UberEats"
            if account:
                parts = account.split(" - ", 1)
                if len(parts) == 2:
                    row_label = parts[1].strip()
            log.info("Drilldown row label: %r (account=%r)", row_label, account)

            # ── Balance Sheet options (set once) ─────────────────────────────
            # Note: button textContent is title-case (CSS does uppercase styling)
            # Report Type: Legal Entity Side by Side
            _set_report_type_bs(ctx, page, "Legal Entity Side by Side")

            # Detail Level: Detail
            _click_button_group(ctx, "Detail Level", "Detail")
            page.wait_for_timeout(300)

            # Account View: Name
            _click_button_group(ctx, "Account View", "Name")
            page.wait_for_timeout(300)

            # Hide $0 Balances: Yes
            _click_button_group(ctx, "Hide $0 Balances", "Yes")
            page.wait_for_timeout(300)

            # Rounding: No Rounding
            _click_button_group(ctx, "Rounding", "No Rounding")
            page.wait_for_timeout(300)

            # Show Unapproved: No
            _click_button_group(ctx, "Show Unapproved", "No")
            page.wait_for_timeout(300)

            # As Of date (Balance Sheet uses single date, not start/end)
            if end_date:
                e_str = f"{end_date.month}/{end_date.day}/{end_date.year}"
                log.info("Setting As Of date to %s", e_str)
                r = _set_as_of_date(ctx, page, e_str)
                log.info("As Of date result: %s", r)

            filters_shot = _snap(page, "bs_options_set")
            _emit(f"Balance Sheet options set — As Of: {end_date or 'default'}", filters_shot)

            # ── FIND_AND_CLICK_RUN_JS shared across iterations ───────────────
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

            def _contexts(p):
                yield "page", p
                for fr in p.frames:
                    if fr is not p.main_frame:
                        yield f"frame[{(fr.url or '')[:60]}]", fr

            # ── Per-entity loop ───────────────────────────────────────────────
            viewer_page = None

            for i, entity in enumerate(legal_entities):
                _emit(f"Processing entity {i + 1}/{len(legal_entities)}: {entity}…")

                if i == 0:
                    # First entity: open Filter modal and select entity.
                    # skip_filter_type=True because Filter By is already "Legal Entity" on Balance Sheet.
                    _select_legal_entity(ctx, page, entity, skip_filter_type=True)
                    _emit(f"Legal entity '{entity}' selected", _snap(page, "entity_ok"))

                    # ── Step 20: Click RUN to open ReportViewer ───────────────
                    new_tabs: list = []
                    _on_page = lambda p: new_tabs.append(p)
                    browser.on("page", _on_page)

                    log.info("Clicking RUN button (customize dialog) to open ReportViewer")
                    run_result = ctx.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                    log.info("Run button (ctx): %s", run_result)
                    if "run-not-found" in (run_result or ""):
                        run_result = page.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                        log.info("Run button (page fallback): %s", run_result)

                    _emit("Clicked RUN — waiting for ReportViewer tab…",
                          _snap(page, "run_clicked"))

                    page.wait_for_timeout(3_000)
                    REPORT_VIEWER_URL = "https://ayg.restaurant365.com/#/ReportViewer"

                    # Prefer a tab R365 spawned itself; otherwise open one ourselves.
                    for p in list(browser.pages):
                        try:
                            if "ReportViewer" in (p.url or ""):
                                viewer_page = p
                                break
                        except Exception:
                            continue

                    if viewer_page is None:
                        log.info("Opening ReportViewer in new tab: %s", REPORT_VIEWER_URL)
                        viewer_page = browser.new_page()
                        try:
                            viewer_page.goto(REPORT_VIEWER_URL, wait_until="domcontentloaded")
                        except Exception as ge:
                            log.warning("ReportViewer goto warning: %s", ge)

                    log.info("ReportViewer: %s", viewer_page.url[:120])
                    try:
                        viewer_page.bring_to_front()
                    except Exception:
                        pass
                    try:
                        viewer_page.wait_for_load_state("networkidle", timeout=60_000)
                    except Exception:
                        pass

                    try:
                        browser.remove_listener("page", _on_page)
                    except Exception as e:
                        log.warning("remove_listener failed (non-fatal): %s", e)

                else:
                    # Subsequent entities: return to the Balance Sheet tab first,
                    # then open Customize and change the entity filter.
                    log.info("Bringing viewer_page to front for entity %r", entity)
                    try:
                        viewer_page.bring_to_front()
                    except Exception:
                        pass
                    viewer_page.wait_for_timeout(1_000)

                    # Click Customize
                    log.info("Clicking Customize on ReportViewer for entity %r", entity)
                    cust_clicked = False
                    try:
                        cust_btn = viewer_page.locator('button[ng-click="toggleLeft()"]').first
                        if cust_btn.count() > 0 and cust_btn.is_visible(timeout=3_000):
                            cust_btn.click()
                            cust_clicked = True
                            log.info("Customize clicked via ng-click selector")
                    except Exception as ce:
                        log.warning("Customize ng-click selector failed: %s", ce)

                    if not cust_clicked:
                        try:
                            cust_result = viewer_page.evaluate("""
                                () => {
                                    const btn = Array.from(document.querySelectorAll('button'))
                                        .find(b => b.textContent.trim() === 'Customize');
                                    if (btn) { btn.click(); return 'clicked-text'; }
                                    return 'not-found';
                                }
                            """)
                            log.info("Customize fallback: %s", cust_result)
                            cust_clicked = cust_result != "not-found"
                        except Exception as fe:
                            log.warning("Customize fallback failed: %s", fe)

                    viewer_page.wait_for_timeout(2_000)

                    # Change the Filter (Legal Entity) using the modal multi-select dialog.
                    # On the viewer page, skip_filter_type=True because Filter By is
                    # already "Legal Entity" from the first run.
                    _select_legal_entity(viewer_page, viewer_page, entity, skip_filter_type=True)
                    _emit(f"Legal entity '{entity}' selected", _snap(viewer_page, "entity_ok"))

                    # Click Run on viewer_page
                    log.info("Clicking RUN on ReportViewer for entity %r", entity)
                    run_result = viewer_page.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                    log.info("Run button (viewer_page): %s", run_result)
                    if "run-not-found" in (run_result or ""):
                        for _flabel, _fc in _contexts(viewer_page):
                            try:
                                run_result = _fc.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                                if "run-clicked" in (run_result or ""):
                                    log.info("Run button in %s: %s", _flabel, run_result)
                                    break
                            except Exception:
                                continue

                    viewer_page.wait_for_timeout(3_000)
                    try:
                        viewer_page.wait_for_load_state("networkidle", timeout=60_000)
                    except Exception:
                        pass

                rv_loaded_snap = _snap(viewer_page, "report_viewer_loaded")
                _emit("Opened ReportViewer — waiting for report render…", rv_loaded_snap)
                log.info("ReportViewer URL after RUN: %s", viewer_page.url)

                # ── Wait for the row_label row to appear in the SSRS iframe ──
                render_deadline = time.time() + 90
                row_ready = False
                while time.time() < render_deadline:
                    for _lbl, _c in _contexts(viewer_page):
                        try:
                            present = _c.evaluate("""
                                (label) => Array.from(document.querySelectorAll('*'))
                                    .some(e => e.offsetParent !== null
                                            && e.children.length === 0
                                            && (e.textContent||'').trim().endsWith(label))
                            """, row_label)
                            if present:
                                log.info("Row %r found in [%s]", row_label, _lbl)
                                row_ready = True
                                break
                        except Exception:
                            continue
                    if row_ready:
                        break
                    viewer_page.wait_for_timeout(1_000)

                pre_snap = _snap(viewer_page, "before_drilldown")
                if not row_ready:
                    log.warning("%r row not found within 90s — trying drilldown anyway", row_label)
                    _emit(f"WARNING: {row_label!r} not found after 90s", pre_snap)
                else:
                    _emit(f"Found {row_label!r} — drilling into {entity}…", pre_snap)

                # ── Drill into (entity × row_label) cell ─────────────────────
                # The amount link has target="_blank" so it opens a NEW TAB.
                # Listen for the new page before clicking.
                excel_path: str | None = None
                detail_pages: list = []
                _on_detail = lambda p: detail_pages.append(p)
                browser.on("page", _on_detail)

                drill = _click_drilldown_amount(viewer_page, entity, row_label)
                log.info("Drilldown click result: %s", drill)

                if drill.startswith("clicked"):
                    # Wait up to 15s for the new tab to appear
                    deadline_dt = time.time() + 15
                    while time.time() < deadline_dt and not detail_pages:
                        viewer_page.wait_for_timeout(500)

                    detail_page = detail_pages[-1] if detail_pages else viewer_page
                    if detail_pages:
                        log.info("Detail page opened: %s", detail_page.url[:120])
                        try:
                            detail_page.bring_to_front()
                            detail_page.wait_for_load_state("networkidle", timeout=60_000)
                        except Exception:
                            pass
                    else:
                        log.info("No new tab — using viewer_page: %s", viewer_page.url)

                    # ── Rewrite URL with correct dates and reload ─────────────
                    # The drilldown URL has Start=M/D/YYYY&End=M/D/YYYY params.
                    # Swapping them in the URL is more reliable than DOM input manipulation.
                    if start_date or end_date:
                        import re as _re
                        current_url = detail_page.url
                        log.info("Detail page URL before date fix: %s", current_url)

                        new_url = current_url
                        if start_date:
                            s_url = f"{start_date.month}/{start_date.day}/{start_date.year}"
                            new_url = _re.sub(r'Start=[^&]+', f'Start={s_url}', new_url)
                            log.info("Start → %s", s_url)
                        if end_date:
                            e_url = f"{end_date.month}/{end_date.day}/{end_date.year}"
                            new_url = _re.sub(r'End=[^&]+', f'End={e_url}', new_url)
                            log.info("End   → %s", e_url)

                        if new_url != current_url:
                            log.info("Navigating detail page to date-fixed URL")
                            _emit(f"Updating dates to {s_url if start_date else '?'} → {e_url if end_date else '?'}…")
                            try:
                                detail_page.goto(new_url, wait_until="domcontentloaded", timeout=60_000)
                            except Exception as ge:
                                log.warning("detail_page.goto warning: %s", ge)
                            try:
                                detail_page.wait_for_load_state("networkidle", timeout=90_000)
                            except Exception:
                                pass
                            detail_page.wait_for_timeout(3_000)
                            log.info("Detail page URL after date fix: %s", detail_page.url)
                        else:
                            log.warning("URL had no Start=/End= params to replace: %s", current_url)

                    # ── Click View Report, then export to Excel ───────────────
                    _emit(f"Clicking View Report for {entity}…")
                    excel_path = _click_view_report_and_export(
                        detail_page, entity, start_date, end_date, _emit
                    )

                    after_snap = _snap(detail_page, "drilldown_opened")
                    _emit(f"Detail report ready for {entity}", after_snap)
                    log.info("Detail report final URL: %s", detail_page.url[:200])
                    log.info("Excel export path: %s", excel_path)
                else:
                    fail_snap = _snap(viewer_page, "drilldown_missing")
                    log.warning("Drilldown FAILED (%s) for %r × %r", drill, entity, row_label)
                    _emit(f"Drilldown failed ({drill}) — {row_label!r} amount not clickable for {entity}",
                          fail_snap)

                try:
                    browser.remove_listener("page", _on_detail)
                except Exception:
                    pass

                results.append({
                    "entity": entity,
                    "drilldown": drill,
                    "excel": excel_path if drill.startswith("clicked") else None,
                })
                if entity_cb:
                    try:
                        entity_cb(entity, None)
                    except Exception as ecb_err:
                        log.warning("entity_cb raised: %s", ecb_err)

            screenshot_name = _snap(viewer_page or page, "report_viewer_final")
            log.info("Final screenshot: %s", screenshot_name)
            return {
                "results": results,
                "screenshot_filename": screenshot_name,
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
