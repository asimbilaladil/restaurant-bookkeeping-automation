"""
Navigate R365 to the Daily Sales Summary Journal Entry and fill it from Revel data.

CLI usage:
    python -m r365.journal_entry --date 2026-05-30
"""

import os
import re
import logging
from datetime import date

from playwright.sync_api import sync_playwright, Page

from .session import R365_USER, R365_PASS, PROFILE_DIR, ensure_logged_in_r365

log = logging.getLogger(__name__)

DSS_URL = "https://ayg.restaurant365.com/react/sales-and-forecasting/legacy/DailySalesSummary"


# ─── JE grid helpers ──────────────────────────────────────────────────────────

def _find_je_table(frame):
    return frame.evaluate("""
        () => {
            const byId = document.querySelector('#DSSJournalEntryGrid table[role="grid"]');
            if (byId) return 'id';
            const activeCell = document.getElementById('DSSJournalEntryGrid_active_cell');
            if (activeCell) return 'active-cell';
            const byAria = document.querySelector('[aria-activedescendant*="DSSJournalEntryGrid"]');
            if (byAria) return 'aria';
            return 'none';
        }
    """)


def _fill_je_cell(frame, account_text: str, field_name: str, value: float, no_comment: bool = False) -> bool:
    # col indices confirmed by DOM inspection: col2=debit, col3=credit
    col_idx = 3 if field_name == "credit" else 2
    no_comment_js = "true" if no_comment else "false"

    try:
        click_result = frame.evaluate(f"""
            (() => {{
                const scope = document.querySelector('#DSSJournalEntryGrid') || document;
                const rows = Array.from(scope.querySelectorAll('tr[role="row"]'));
                const noComment = {no_comment_js};
                const row = rows.find(r => {{
                    const tds = r.querySelectorAll('td');
                    if (tds.length < 4) return false;
                    const clone = tds[1].cloneNode(true);
                    clone.querySelectorAll('select').forEach(s => s.remove());
                    if (!clone.textContent.trim().includes({repr(account_text)})) return false;
                    if (noComment) {{
                        const comment = tds.length > 4 ? tds[4].textContent.trim() : '';
                        if (comment) return false;
                    }}
                    return true;
                }});
                if (!row) return 'row-not-found (searched ' + rows.length + ' rows)';
                const cell = row.querySelectorAll('td')[{col_idx}];
                if (!cell) return 'cell-not-found at col {col_idx}';
                cell.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                return 'clicked col{col_idx} in ' + rows.length + ' rows';
            }})()
        """)
        log.info("  Click '%s' col%d: %s", account_text, col_idx, click_result)
        if "not-found" in click_result:
            return False

        frame.wait_for_timeout(800)

        inp = frame.locator(f'input[name="{field_name}"]').first
        if inp.count() > 0:
            inp.fill(f"{value:.2f}")
            inp.press("Tab")
            frame.wait_for_timeout(400)
            log.info("  Filled '%s' %s = %.2f", account_text, field_name, value)
            return True

        inputs_debug = frame.evaluate(
            "() => Array.from(document.querySelectorAll('tr.k-grid-edit-row input'))"
            ".map(i => (i.name||'noname') + '=' + i.value).join(', ')"
        )
        log.warning(
            "  No input[name='%s'] for '%s' — edit-row inputs: %s",
            field_name, account_text, inputs_debug or "(none)",
        )
        return False
    except Exception as e:
        log.warning("  Could not fill '%s' %s: %s", account_text, field_name, e)
        return False


def _read_je_cell_value(frame, account_text: str, col: str) -> float:
    """Read the current debit or credit value from a JE row by account text."""
    col_idx = 3 if col == "credit" else 2
    try:
        val = frame.evaluate(f"""
            (() => {{
                const scope = document.querySelector('#DSSJournalEntryGrid') || document;
                const rows = Array.from(scope.querySelectorAll('tr[role="row"]'));
                const row = rows.find(r => {{
                    const tds = r.querySelectorAll('td');
                    if (tds.length < 4) return false;
                    const clone = tds[1].cloneNode(true);
                    clone.querySelectorAll('select').forEach(s => s.remove());
                    return clone.textContent.trim().includes({repr(account_text)});
                }});
                if (!row) return null;
                const cell = row.querySelectorAll('td')[{col_idx}];
                if (!cell) return null;
                const text = cell.textContent.replace(/[^0-9.-]/g, '').trim();
                return text ? parseFloat(text) : 0;
            }})()
        """)
        return round(float(val or 0), 2)
    except Exception as e:
        log.warning("Could not read '%s' %s: %s", account_text, col, e)
        return 0.0


def _screenshot_je_grid(frame, path: str) -> float:
    """
    Extract all JE rows from the Kendo grid, render them as a self-contained HTML
    table, and screenshot that — guarantees every row is visible regardless of
    the grid's scroll container size.
    Falls back to a plain page screenshot if extraction fails.
    Returns the debit-minus-credit difference (0.0 = balanced).
    """
    diff = 0.0
    try:
        rows = frame.evaluate("""
            () => {
                const scope = document.querySelector('#DSSJournalEntryGrid') || document;
                const totals = scope.querySelector('tr.k-footer-template');
                const result = [];
                scope.querySelectorAll('tr[role="row"]').forEach(r => {
                    const tds = r.querySelectorAll('td');
                    if (tds.length < 4) return;
                    const clone = tds[1].cloneNode(true);
                    clone.querySelectorAll('select').forEach(s => s.remove());
                    const acct    = clone.textContent.trim();
                    const debit   = tds[2].textContent.trim();
                    const credit  = tds[3].textContent.trim();
                    const comment = tds.length > 4 ? tds[4].textContent.trim() : '';
                    const loc     = tds.length > 5 ? tds[5].textContent.trim() : '';
                    if (acct) result.push({acct, debit, credit, comment, loc});
                });
                // grab footer totals row
                if (totals) {
                    const tds = totals.querySelectorAll('td');
                    result.push({
                        acct: 'TOTAL',
                        debit:  tds[2] ? tds[2].textContent.trim() : '',
                        credit: tds[3] ? tds[3].textContent.trim() : '',
                        comment: '', loc: '', _total: true
                    });
                }
                return result;
            }
        """) or []

        if not rows:
            raise ValueError("No rows extracted from JE grid")

        # Log as ASCII table for the entity log
        log.info("JE grid rows (%d):", len([r for r in rows if not r.get("_total")]))
        log.info("  %-45s %12s %12s  %s", "Account", "Debit", "Credit", "Comment")
        log.info("  %s", "-" * 85)
        for r in rows:
            marker = "→ TOTAL" if r.get("_total") else ""
            log.info("  %-45s %12s %12s  %s %s",
                     r["acct"], r["debit"], r["credit"], r["comment"], marker)

        # Build HTML table
        rows_html = ""
        for r in rows:
            bg = "#f0f4ff" if r.get("_total") else ""
            fw = "bold"    if r.get("_total") else "normal"
            rows_html += (
                f'<tr style="background:{bg};font-weight:{fw}">'
                f'<td>{r["acct"]}</td>'
                f'<td class="num">{r["debit"]}</td>'
                f'<td class="num">{r["credit"]}</td>'
                f'<td>{r["comment"]}</td>'
                f'<td>{r["loc"]}</td>'
                f'</tr>\n'
            )

        # Compute diff and append Difference row (Debit total − Credit total)
        total_row = next((r for r in rows if r.get("_total")), None)
        if total_row:
            try:
                def _parse_total(s):
                    return float(re.sub(r"[^0-9.\-]", "", s or "0") or 0)
                diff = round(_parse_total(total_row["debit"]) - _parse_total(total_row["credit"]), 2)
                log.info("JE balance difference: %.2f (%s)", diff, "BALANCED" if diff == 0 else "UNBALANCED")
                diff_color = "#c0392b" if diff != 0 else "#27ae60"
                diff_str   = f"{diff:,.2f}" if diff != 0 else "0.00  ✓ Balanced"
                rows_html += (
                    f'<tr style="background:#fff8e1;font-weight:bold;color:{diff_color}'
                    f';border-top:2px solid #bbb">'
                    f'<td>Difference (Debit − Credit)</td>'
                    f'<td class="num">{diff_str}</td>'
                    f'<td class="num"></td>'
                    f'<td></td><td></td>'
                    f'</tr>\n'
                )
            except Exception:
                pass

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 13px; margin: 16px; }}
  h2   {{ margin-bottom: 8px; color: #333; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th   {{ background: #2c5f8a; color: #fff; padding: 6px 10px; text-align: left; }}
  td   {{ padding: 5px 10px; border-bottom: 1px solid #ddd; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:hover td {{ background: #f9f9f9; }}
</style></head><body>
<h2>R365 Journal Entry — {path}</h2>
<table>
  <thead><tr><th>Account</th><th class="num">Debit</th><th class="num">Credit</th>
  <th>Comment</th><th>Location</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table></body></html>"""

        # Render via a data: URL in a new page and screenshot it
        ctx  = frame.page.context
        tmp  = ctx.new_page()
        tmp.set_viewport_size({"width": 1200, "height": 800})
        tmp.goto(f"data:text/html;charset=utf-8,{html.replace('#', '%23')}", wait_until="load")
        tmp.wait_for_timeout(300)
        tmp.locator("table").screenshot(path=path)
        tmp.close()
        log.info("Full JE table screenshot saved: %s", path)

    except Exception as e:
        log.warning("JE table screenshot failed (%s) — falling back to page screenshot", e)
        try:
            frame.page.screenshot(path=path)
        except Exception:
            pass
    return diff


def _upload_attachment(active, attachment_path: str, screenshot_path: str | None = None) -> str:
    """
    Upload a file to the DSS Attachments module.

    The Angular HTML is:
        <button id="attachmentsModuleInputButton" ng-click="AWS_S3_Uploader.openFileDialog($event)">
        <div class="hidediv">
          <input type="file" id="attachmentsModuleInput"
                 r365-custom-on-change="attachmentsModuleInputChangeHandler(e)">
        </div>

    The button is a thin wrapper that programmatically clicks the hidden
    input. We skip the button entirely and:
      1. set_input_files() directly on #attachmentsModuleInput
      2. dispatch a native 'change' event so the r365-custom-on-change
         directive fires attachmentsModuleInputChangeHandler
      3. invoke Angular $apply() to push the result into the digest cycle
         (the directive wraps the handler, but belt-and-suspenders)
      4. poll for the filename to land in AWS_S3_Uploader.files

    Returns one of: 'uploaded', 'already_present', 'failed', 'skipped'.
    """
    if not attachment_path or not os.path.exists(attachment_path):
        log.warning("Attachment skipped — path missing or not found: %s", attachment_path)
        return "skipped"

    filename = os.path.basename(attachment_path)

    # The DSS form now contains TWO inputs sharing id="attachmentsModuleInput":
    # the new "Upload Ribbon Logo" uploader (inside <r365-amazon-uploader>) and the
    # real DSS attachments input (inside #KendoSplitter). document.getElementById and a
    # bare '#attachmentsModuleInput' locator hit the wrong/ambiguous one. This JS
    # expression always resolves the real DSS input, skipping the ribbon-logo uploader.
    _INPUT_EXPR = (
        "(document.querySelector('#KendoSplitter #attachmentsModuleInput')"
        " || [...document.querySelectorAll('#attachmentsModuleInput')]"
        ".filter(i => !i.closest('r365-amazon-uploader')).pop()"
        " || document.getElementById('attachmentsModuleInput'))"
    )

    for frame in active.frames + [active]:
        try:
            has_input = frame.evaluate(f"() => !!{_INPUT_EXPR}")
            if not has_input:
                continue
            log.info("Attachments input found in frame: %s", getattr(frame, "url", ""))

            already = frame.evaluate(f"""
                () => {{
                    if (window.AWS_S3_Uploader && Array.isArray(AWS_S3_Uploader.files)) {{
                        if (AWS_S3_Uploader.files.some(f =>
                            (f.name || f.fileName || '').includes({repr(filename)}))) return true;
                    }}
                    const root = document.querySelector('#attachmentsModule, .col-md-12') || document.body;
                    return root.innerText.includes({repr(filename)});
                }}
            """)
            if already:
                log.info("✅ Attachment '%s' already present — skipping upload", filename)
                if screenshot_path:
                    _screenshot_attachments(frame, screenshot_path)
                return "already_present"

            # Resolve the DSS attachments input ONCE to a single element handle: the
            # #attachmentsModuleInput that is NOT the ribbon-logo uploader (which lives
            # inside <r365-amazon-uploader>). set_input_files, the change event and
            # verification then all act on the SAME element — previously they diverged
            # and the verified input had files==0 (see ATTACH DIAGNOSTIC).
            input_handle = frame.evaluate_handle("""
                () => document.querySelector('#KendoSplitter #attachmentsModuleInput')
                    || [...document.querySelectorAll('#attachmentsModuleInput')]
                        .filter(i => !i.closest('r365-amazon-uploader')).pop()
                    || document.getElementById('attachmentsModuleInput')
            """)
            el = input_handle.as_element()
            if el is None:
                log.warning("DSS attachments input handle not resolvable in this frame")
                continue

            # set_input_files delivers the File to R365's change handler, but the handler
            # never pushes it to scope.AWS_S3_Uploader (uploaderFiles stays [], status
            # False) — the synthetic change event isn't trusted, so R365 drops it.
            # Reproduce a real user gesture: click the input to open the native file
            # chooser and let Playwright fulfil it, producing a TRUSTED change event the
            # uploader accepts. Fall back to set_input_files if no chooser appears.
            via_chooser = False
            try:
                with active.expect_file_chooser(timeout=5_000) as fc_info:
                    frame.evaluate("(inp) => inp.click()", el)
                fc_info.value.set_files(attachment_path)
                via_chooser = True
                log.info("Native file chooser fulfilled: %s", attachment_path)
            except Exception as fc_err:
                log.warning("File chooser path failed (%s) — falling back to set_input_files", fc_err)
                el.set_input_files(attachment_path)

            files_count = frame.evaluate("(inp) => inp && inp.files ? inp.files.length : -1", el)
            log.info("Files set on DSS #attachmentsModuleInput: %s (input.files=%s, chooser=%s)",
                     attachment_path, files_count, via_chooser)

            # Flush Angular. When the native chooser fired a trusted change, R365's
            # r365-custom-on-change directive already ran the handler — only $apply is
            # needed. On the fallback path (no chooser) re-dispatch change and invoke
            # attachmentsModuleInputChangeHandler explicitly.
            fire_result = frame.evaluate("""
                ([inp, viaChooser]) => {
                    if (!inp) return 'no-input';
                    if (!viaChooser) {
                        const evt = new Event('change', {bubbles: true, cancelable: true});
                        inp.dispatchEvent(evt);
                    }
                    if (window.angular) {
                        try {
                            const scope = angular.element(inp).scope();
                            if (!viaChooser && scope && typeof scope.attachmentsModuleInputChangeHandler === 'function') {
                                scope.attachmentsModuleInputChangeHandler({target: inp});
                            }
                            if (scope && scope.$apply) {
                                if (scope.$root && scope.$root.$$phase) return 'change+handler+digest-in-progress';
                                scope.$apply();
                                return 'change+handler+apply';
                            }
                        } catch (e) { return 'change+apply-error: ' + e.message; }
                    }
                    return 'change-only';
                }
            """, [el, via_chooser])
            log.info("Change fired: %s", fire_result)

            # Poll for the file to land. R365 now tracks attachments on the Angular
            # scope (scope.AWS_S3_Uploader.files / scope.uploadedAttachments), not a
            # window global — check both, plus the DOM.
            for i in range(45):
                frame.wait_for_timeout(1_000)
                landed = frame.evaluate(f"""
                    (inp) => {{
                        const fn = {repr(filename)};
                        let scope = null;
                        try {{ scope = window.angular ? angular.element(inp).scope() : null; }} catch (e) {{}}
                        if (scope) {{
                            const up = scope.AWS_S3_Uploader;
                            if (up && Array.isArray(up.files) && up.files.some(f =>
                                (f.name || f.fileName || '').includes(fn))) return 'scope-aws-files';
                            const ua = scope.uploadedAttachments;
                            if (Array.isArray(ua) && ua.some(a =>
                                JSON.stringify(a).includes(fn))) return 'scope-uploadedAttachments';
                        }}
                        const root = document.querySelector('#attachmentsModule, .col-md-12') || document.body;
                        return root.innerText.includes(fn) ? 'dom' : null;
                    }}
                """, el)
                if landed:
                    log.info("✅ Attachment confirmed (%s) after %ds: %s", landed, i + 1, filename)
                    if screenshot_path:
                        _screenshot_attachments(frame, screenshot_path)
                    return "uploaded"

            log.warning("⚠️  Attachment '%s' not in scope uploader or DOM after 45s", filename)
            # Dump the SCOPE uploader state for diagnosis (R365 moved it off window).
            try:
                state = frame.evaluate("""
                    (inp) => {
                        let scope = null;
                        try { scope = window.angular ? angular.element(inp).scope() : null; } catch (e) {}
                        if (!scope) return 'no-scope';
                        const up = scope.AWS_S3_Uploader;
                        return {
                            inputFiles: inp && inp.files ? inp.files.length : null,
                            uploaderStatus: up ? up.status : 'no scope.AWS_S3_Uploader',
                            uploaderFiles: up && Array.isArray(up.files)
                                ? up.files.map(f => f.name || f.fileName || JSON.stringify(f)) : null,
                            uploadedAttachments: Array.isArray(scope.uploadedAttachments)
                                ? scope.uploadedAttachments.map(a => JSON.stringify(a).slice(0, 120)) : scope.uploadedAttachments,
                            disableUploadButton: scope.disableUploadButton,
                        };
                    }
                """, el)
                log.warning("Scope uploader state: %s", state)
            except Exception:
                pass

            if screenshot_path:
                _screenshot_attachments(frame, screenshot_path)
            return "failed"
        except Exception as e:
            log.warning("Attachment frame attempt failed (%s): %s", getattr(frame, "url", ""), e)
            continue

    log.warning("Could not locate #attachmentsModuleInput in any frame")
    return "failed"


def _screenshot_attachments(frame, path: str) -> None:
    """Screenshot the attachments module area for visual proof."""
    try:
        # The button + thumbnail row both live under the same col-md-12 wrapper
        loc = frame.locator(
            '#attachmentsModule, .col-md-12:has(#attachmentsModuleInputButton)'
        ).first
        if loc.count() > 0:
            loc.scroll_into_view_if_needed()
            loc.screenshot(path=path)
        else:
            frame.locator('#attachmentsModuleInputButton').first.screenshot(path=path)
        log.info("Attachments screenshot saved: %s", path)
    except Exception as e:
        log.warning("Could not screenshot attachments area: %s", e)


# ── Unmapped payment-type auto-assignment ────────────────────────────────────
# When the DSS journal entry shows a '-' (unassigned) row, R365's "Payment Type
# Account" window needs a GL Account and a Payment Group, then Save. The GL
# account is the Comps account; the Payment Group is still pending confirmation
# from accounting. While PAYMENT_TYPE_PAYMENT_GROUP is None the auto-assign is a
# safe no-op — the entry just stays unbalanced (and unapproved), as before.
PAYMENT_TYPE_GL_ACCOUNT = "4500-02 - Comps"
PAYMENT_TYPE_PAYMENT_GROUP = None  # TODO: set once accounting confirms the group


def _detect_unassigned_je_rows(je_frame) -> list[dict]:
    """Return JE grid rows that have no account ('-') and a non-zero amount.

    Such a row means a payment-type item (e.g. a coupon/comp) is not mapped to a
    GL account, which unbalances the journal entry.
    """
    try:
        return je_frame.evaluate(r"""
            () => {
                const out = [];
                const grid = document.querySelector('#DSSJournalEntryGrid table[role="grid"]')
                          || document.querySelector('table[role="grid"]');
                if (!grid) return out;
                grid.querySelectorAll('tbody tr[role="row"]').forEach(r => {
                    const tds = r.querySelectorAll('td');
                    if (tds.length < 4) return;
                    const clone = tds[1].cloneNode(true);
                    clone.querySelectorAll('select').forEach(s => s.remove());
                    const acct = clone.textContent.trim();
                    const num = (s) => parseFloat((s || '').replace(/[^0-9.\-]/g, '')) || 0;
                    const debit = num(tds[2].textContent);
                    const credit = num(tds[3].textContent);
                    const comment = tds.length > 4 ? tds[4].textContent.trim() : '';
                    if ((acct === '-' || acct === '') && (debit || credit)) {
                        out.push({comment, debit, credit});
                    }
                });
                return out;
            }
        """) or []
    except Exception as e:
        log.warning("Detect unassigned JE rows failed: %s", e)
        return []


def _assign_missing_payment_types(active, je_frame) -> list[str]:
    """If the JE has unassigned ('-') rows, open the ASSIGN PAYMENT TYPE tab,
    tick every unassigned item and click Update. R365 remembers the account
    mapping, so Update reassigns the item without needing to pick an account.
    Returns the list of item names that were assigned (empty if nothing to do).
    """
    missing = _detect_unassigned_je_rows(je_frame)
    if not missing:
        return []
    names = [m.get("comment", "") for m in missing]
    log.info("Found %d unassigned payment-type item(s): %s", len(missing), names)

    if not PAYMENT_TYPE_PAYMENT_GROUP:
        log.warning(
            "Unassigned payment-type item(s) %s found, but PAYMENT_TYPE_PAYMENT_GROUP "
            "is not configured yet (pending accountant) — skipping auto-assign.", names)
        return []

    # The tab strip + assign grid live in the form iframe — find the frame that
    # actually has the ASSIGN PAYMENT TYPE tab.
    target = None
    for f in list(active.frames) + [active]:
        try:
            if f.locator('li[role="tab"] span.k-link', has_text="ASSIGN PAYMENT TYPE").count() > 0:
                target = f
                break
        except Exception:
            continue
    if target is None:
        log.warning("ASSIGN PAYMENT TYPE tab not found — cannot auto-assign")
        return []

    tab = target.locator('li[role="tab"] span.k-link', has_text="ASSIGN PAYMENT TYPE").first
    try:
        tab.scroll_into_view_if_needed()
        tab.click()
    except Exception as e:
        log.warning("Clicking ASSIGN PAYMENT TYPE tab failed: %s", e)
        return []
    target.wait_for_timeout(2_000)
    try:
        active.screenshot(path="/tmp/r365_assign_tab.png")
    except Exception:
        pass

    # Tick every unassigned row's checkbox.
    boxes = target.evaluate("""
        () => {
            const boxes = Array.from(document.querySelectorAll('input.checkbox.select-row'));
            let n = 0;
            boxes.forEach(b => { if (!b.checked) { b.click(); n++; } });
            return {total: boxes.length, clicked: n};
        }
    """)
    log.info("Assign grid checkboxes: %s", boxes)
    target.wait_for_timeout(1_000)

    # Click the toolbar Update button — opens the "Payment Type Account" window.
    upd = target.evaluate("""
        () => {
            const btn = Array.from(document.querySelectorAll('button')).find(b =>
                /openUpdatePaymentTypeAccounteWindow/.test(b.getAttribute('ng-click') || '') ||
                b.textContent.trim() === 'Update');
            if (!btn) return 'update-not-found';
            btn.click();
            return 'update-clicked';
        }
    """)
    log.info("Assign Update click: %s", upd)
    active.wait_for_timeout(2_500)
    try:
        active.screenshot(path="/tmp/r365_assign_window.png")
    except Exception:
        pass

    # Fill the window: GL Account + Payment Group (both Kendo comboboxes — type
    # the value then press Enter to select the match), then click Save.
    def _set_combo(input_name, value):
        try:
            inp = target.locator(f'input[name="{input_name}"]').first
            inp.click(timeout=5_000)
            inp.fill("")
            inp.type(value, delay=40)
            target.wait_for_timeout(1_200)
            target.keyboard.press("Enter")
            target.wait_for_timeout(600)
            return True
        except Exception as e:
            log.warning("Set combo %s=%r failed: %s", input_name, value, e)
            return False

    _set_combo("PaymentTypeAccountGLAccount_input", PAYMENT_TYPE_GL_ACCOUNT)
    _set_combo("PaymentTypeAccountPaymentGroup_input", PAYMENT_TYPE_PAYMENT_GROUP)

    saved = target.evaluate("""
        () => {
            const btn = document.querySelector('button[ng-click*="windowMethods.save()"]')
                || Array.from(document.querySelectorAll('.k-window button'))
                    .find(b => b.textContent.trim() === 'Save');
            if (!btn) return 'save-not-found';
            btn.click();
            return 'save-clicked';
        }
    """)
    log.info("Assign window Save: %s", saved)
    active.wait_for_timeout(3_000)
    try:
        active.screenshot(path="/tmp/r365_assign_done.png")
    except Exception:
        pass
    return names


def _recreate_journal_entry(active) -> None:
    """Force R365 to regenerate the JE (Action → Recreate Journal Entry) so it
    picks up a freshly-assigned payment-type mapping. Confirms any dialog."""
    res = active.evaluate("""
        () => {
            const el = document.querySelector('[data-testid="recreateJournalEntryMenuItem"]');
            if (!el) return 'recreate-not-found';
            el.click();
            return 'recreate-clicked';
        }
    """)
    log.info("Recreate JE: %s", res)
    active.wait_for_timeout(2_000)
    active.evaluate("""
        () => {
            const wins = Array.from(document.querySelectorAll('.k-window, .modal, [role="dialog"]'))
                .filter(w => w.offsetParent !== null);
            const win = wins[wins.length - 1];
            if (!win) return 'no-confirm';
            const want = ['yes', 'ok', 'recreate', 'confirm', 'continue'];
            const btn = Array.from(win.querySelectorAll('button'))
                .find(b => want.includes(b.textContent.trim().toLowerCase()));
            if (btn) { btn.click(); return 'confirmed'; }
            return 'no-btn';
        }
    """)
    active.wait_for_timeout(4_000)


def fill_journal_entry(
    active,
    revel_values: dict,
    screenshot_path: str = "/tmp/r365_je_filled.png",
    attachment_path: str | None = None,
    attachment_screenshot_path: str | None = None,
) -> tuple[float, str, str]:
    """
    Fill R365 Journal Entry from Revel data.

    Expected revel_values keys:
        food_sales              → 4000-01 Food Sales (Credit)
        beverage_sales          → 4000-02 Beverage Sales (Credit)
        delivery_food_sales     → 4000-08 Food Delivery Sales (Credit)
        other_sales             → 4000-07 Other Sales (Credit, add row if non-zero)
        sales_tax               → 2240-000 Sales Tax Payable (Credit)
        credit_cards_ar         → 1200-000 A/R Credit Cards Receivable (Debit)
        uber_eats               → 1245-12 A/R-UberEats (Debit)
        doordash                → 1245-03 A/R-DoorDash (Debit)
        grubhub                 → 1245-08 A/R-GrubHub (Debit)
        undeposited_funds       → 1255 Undeposited Funds (Debit)
        comps                   → 4500-02 Comps (Debit)
        item_discounts          → 4500-01 Discounts (Debit)
        employee_discount       → 5000-17 Employee Discount (Debit)
        promotions              → 4500-03 Promotions (Debit)
        employee_tips           → 2301 Employee Tips Payable (Credit)
        cash_over_short         → 8000-06 Cash Over/Short (Debit if negative, Credit if positive)
        cash_over_short_sign    → 'debit' or 'credit'
    """
    active.wait_for_timeout(3_000)

    je_frame = None
    for f in active.frames:
        try:
            if f.locator('td:has-text("Food Delivery Sales")').count() > 0:
                visible = f.evaluate(
                    "() => document.documentElement.offsetHeight > 0 && "
                    "window.getComputedStyle(document.documentElement).visibility !== 'hidden'"
                )
                if visible:
                    je_frame = f
                    log.info("JE frame found (visible): %s", f.url)
                    break
        except Exception:
            continue
    if je_frame is None:
        je_frame = active
        log.warning("JE frame not found — trying main page")

    try:
        je_frame.locator('tr[role="row"]').first.wait_for(state="attached", timeout=15_000)
        log.info("JE grid rows detected")
    except Exception as e:
        log.warning("JE grid rows not detected within 15s: %s", e)

    # ── Self-heal a previously-saved tax-exempt row on the wrong side ──────────
    # Earlier versions booked 4000-011 (a revenue account) as a DEBIT. If a saved
    # JE still has it on the debit side (or with the wrong amount), the dedup
    # guard further down refuses to touch it and the entry stays mis-booked.
    # Recreating the JE wipes our manual rows back to R365's native generation so
    # the corrected credit-side fill can apply cleanly. Done before the fill so it
    # self-heals on re-run rather than getting stuck.
    heal_amount = round(float(revel_values.get("tax_exempt_amount") or 0), 2)
    if heal_amount:
        stale_debit  = _read_je_cell_value(je_frame, "4000-011", "debit")
        stale_credit = _read_je_cell_value(je_frame, "4000-011", "credit")
        if stale_debit or (stale_credit and stale_credit != heal_amount):
            log.warning(
                "Stale 4000-011 row (debit=%.2f credit=%.2f, want credit=%.2f) — recreating JE to reset",
                stale_debit, stale_credit, heal_amount,
            )
            _recreate_journal_entry(active)
            # Switch back to the Journal Entry tab and re-detect the grid frame.
            for f in list(active.frames) + [active]:
                try:
                    je_tab = f.locator('li[role="tab"] span.k-link', has_text="Journal Entry").first
                    if je_tab.count() > 0:
                        je_tab.scroll_into_view_if_needed()
                        je_tab.click()
                        active.wait_for_timeout(4_000)
                        break
                except Exception:
                    continue
            for f in active.frames:
                try:
                    if f.locator('td:has-text("Food Delivery Sales")').count() > 0:
                        je_frame = f
                        break
                except Exception:
                    continue
            try:
                je_frame.locator('tr[role="row"]').first.wait_for(state="attached", timeout=15_000)
                log.info("JE grid re-detected after stale-row recreate")
            except Exception as e:
                log.warning("JE grid not re-detected after stale-row recreate: %s", e)

    # ── Auto-assign unassigned payment-type items (rows showing '-') ──────────
    # A '-' account row is an unmapped item (e.g. a coupon/comp) that unbalances
    # the JE. R365 remembers the mapping, so ticking it + Update reassigns it.
    # Done before filling so the recreated JE is mapped and can balance — this
    # makes both the cron run and the manual rerun self-heal.
    try:
        assigned = _assign_missing_payment_types(active, je_frame)
    except Exception as e:
        assigned = []
        log.warning("Payment-type auto-assign step failed: %s", e)
    if assigned:
        log.info("Auto-assigned %d payment type(s): %s — recreating JE", len(assigned), assigned)
        _recreate_journal_entry(active)
        # Switch back to the Journal Entry tab (we navigated to ASSIGN PAYMENT TYPE).
        for f in list(active.frames) + [active]:
            try:
                je_tab = f.locator('li[role="tab"] span.k-link', has_text="Journal Entry").first
                if je_tab.count() > 0:
                    je_tab.scroll_into_view_if_needed()
                    je_tab.click()
                    active.wait_for_timeout(4_000)
                    break
            except Exception:
                continue
        # Re-find the refreshed JE grid frame and re-open its rows.
        for f in active.frames:
            try:
                if f.locator('td:has-text("Food Delivery Sales")').count() > 0:
                    je_frame = f
                    break
            except Exception:
                continue
        try:
            je_frame.locator('tr[role="row"]').first.wait_for(state="attached", timeout=15_000)
            log.info("JE grid re-detected after recreate")
        except Exception as e:
            log.warning("JE grid not re-detected after recreate: %s", e)

    # ── Read all current R365 values first, then verify and write if different ──
    def _reconcile(account, field, revel_val):
        """Read R365 value, compare to Revel. Write only if different."""
        r365_val = _read_je_cell_value(je_frame, account, field)
        revel_val = round(float(revel_val or 0), 2)
        if r365_val == revel_val:
            log.info("  MATCH    %-45s %s = %.2f (no write needed)", account, field, r365_val)
        else:
            log.info("  MISMATCH %-45s %s: R365=%.2f Revel=%.2f → writing", account, field, r365_val, revel_val)
            if revel_val:
                _fill_je_cell(je_frame, account, field, revel_val)

    # Read marketplace + promotions values (R365 pre-fills from platform data)
    r365_promotions = _read_je_cell_value(je_frame, "4500-03 - Promotions", "debit")
    r365_uber_eats  = _read_je_cell_value(je_frame, "1245-12 - A/R-UberEats", "debit")
    r365_doordash   = _read_je_cell_value(je_frame, "1245-03 - A/R-DoorDash", "debit")
    r365_grubhub    = _read_je_cell_value(je_frame, "1245-08 - A/R-GrubHub", "debit")
    log.info("R365 current — Promotions: %.2f, UberEats: %.2f, DoorDash: %.2f, GrubHub: %.2f",
             r365_promotions, r365_uber_eats, r365_doordash, r365_grubhub)

    # Verify marketplace values against Revel — write if mismatch
    _reconcile("1245-12 - A/R-UberEats", "debit", revel_values.get("uber_eats", 0))
    _reconcile("1245-03 - A/R-DoorDash", "debit", revel_values.get("doordash", 0))
    _reconcile("1245-08 - A/R-GrubHub",  "debit", revel_values.get("grubhub", 0))

    # ── Discount reconciliation ───────────────────────────────────────────────
    # Strategy:
    # 1. Write employee discount and comps first (we own these) and wait to settle
    # 2. THEN read ALL R365 discount rows (now includes our written values)
    # 3. Sum and compare to Revel total
    # 4. If variance → write remainder to plain 4500-01 row

    # Step 1: Write emp/comps and wait for R365 to reflect them
    _reconcile("4500-02 - Comps",             "debit", revel_values.get("comps"))
    _reconcile("5000-17 - Employee Discount", "debit", revel_values.get("employee_discount"))
    emp_disc_target = round(float(revel_values.get("employee_discount") or 0), 2)
    for _ in range(20):  # poll up to 10s
        je_frame.wait_for_timeout(500)
        current = _read_je_cell_value(je_frame, "5000-17 - Employee Discount", "debit")
        if current == emp_disc_target:
            log.info("✅ Employee Discount settled at %.2f", current)
            break
        log.info("Waiting for Employee Discount to settle: current=%.2f target=%.2f", current, emp_disc_target)
    else:
        log.warning("Employee Discount did not settle to %.2f after 10s", emp_disc_target)

    revel_discounts_total = revel_values.get("revel_discounts_total", 0.0)
    revel_discounts_data  = revel_values.get("discounts_data", [])

    # Build Revel amounts list for verification (non-total rows only)
    revel_amounts = [
        round(float(r.get("amount", 0)), 2)
        for r in revel_discounts_data
        if not r.get("is_total")
    ]

    def _verify_in_revel(label: str, val: float) -> bool:
        """Verify an R365 value exists in Revel discounts_data (single or sum of rows)."""
        val = round(val, 2)
        if val == 0.0:
            return True
        if val in revel_amounts:
            log.info("  VERIFIED   %-35s %.2f — direct match in Revel", label, val)
            return True
        from itertools import combinations
        for n in range(2, min(len(revel_amounts) + 1, 6)):
            for combo in combinations(revel_amounts, n):
                if round(sum(combo), 2) == val:
                    log.info("  VERIFIED   %-35s %.2f = sum%s in Revel", label, val, combo)
                    return True
        log.warning("  UNVERIFIED %-35s %.2f NOT found in Revel discounts", label, val)
        return False

    # Read ALL R365 discount-related rows and their current values
    r365_discount_rows = je_frame.evaluate("""
        (() => {
            const scope = document.querySelector('#DSSJournalEntryGrid') || document;
            const rows = Array.from(scope.querySelectorAll('tr[role="row"]'));
            const result = [];
            const DISCOUNT_ACCOUNTS = ['4500-01', '4500-02', '4500-03', '4500-04', '5000-17'];
            rows.forEach(r => {
                const tds = r.querySelectorAll('td');
                if (tds.length < 4) return;
                const clone = tds[1].cloneNode(true);
                clone.querySelectorAll('select').forEach(s => s.remove());
                const acct = clone.textContent.trim();
                if (DISCOUNT_ACCOUNTS.some(a => acct.includes(a))) {
                    const debit = parseFloat(tds[2].textContent.replace(/[^0-9.-]/g, '')) || 0;
                    const comment = tds.length > 4 ? tds[4].textContent.trim() : '';
                    const isPlain4500_01 = acct.includes('4500-01') && !comment;
                    if (debit > 0) result.push({
                        account: acct,
                        comment: comment,
                        value: Math.round(debit * 100) / 100,
                        isPlain: isPlain4500_01
                    });
                }
            });
            return result;
        })()
    """) or []

    log.info("R365 discount rows read: %s", r365_discount_rows)

    # Verify each row against Revel and sum total
    # Track only the FIRST plain 4500-01 row — _fill_je_cell also targets the first match
    r365_discount_total = 0.0
    plain_4500_01_value = 0.0
    plain_4500_01_found = False
    for row in r365_discount_rows:
        val     = round(float(row.get("value", 0)), 2)
        label   = f"{row.get('account','')} {row.get('comment','')}".strip()
        _verify_in_revel(label, val)
        r365_discount_total += val
        if row.get("isPlain") and not plain_4500_01_found:
            plain_4500_01_value = val
            plain_4500_01_found = True
    r365_discount_total = round(r365_discount_total, 2)

    # Compare totals
    discount_variance = round(revel_discounts_total - r365_discount_total, 2)
    log.info(
        "Discount totals — Revel: %.2f  R365: %.2f  Variance: %.2f",
        revel_discounts_total, r365_discount_total, discount_variance
    )

    # Only write to plain 4500-01 if there's a variance
    if discount_variance == 0.0:
        log.info("✅ Discounts match — no writes needed to discount fields")
        item_discounts = 0.0  # signal: don't write
    else:
        item_discounts = round(plain_4500_01_value + discount_variance, 2)
        log.info(
            "⚠️  Discount variance %.2f → updating 4500-01 from %.2f to %.2f",
            discount_variance, plain_4500_01_value, item_discounts
        )

    log.info("Filling Journal Entry — values: %s", revel_values)

    # ── Credits ──────────────────────────────────────────────────────────────
    _reconcile("4000-01 - Food Sales",           "credit", revel_values.get("food_sales"))

    # ── Untaxed Food Sales (4000-011) ─────────────────────────────────────────
    # Add a new row via the Select Account box if net_sales_untaxed != 0
    tax_exempt_field  = revel_values.get("tax_exempt_field")
    tax_exempt_amount = revel_values.get("tax_exempt_amount", 0.0)
    if tax_exempt_field and tax_exempt_amount:
        try:
            # Check if row already exists (catches duplicate-add across re-runs if save persisted)
            existing = _read_je_cell_value(je_frame, "4000-011", tax_exempt_field)
            log.info("4000-011 current R365 value: %.2f (want %.2f)", existing, tax_exempt_amount)
            if existing == round(tax_exempt_amount, 2):
                log.info("✅ 4000-011 Food Sales-tax exempt already correct: %.2f", tax_exempt_amount)
            else:
                # Count how many 4000-011 rows already exist to detect duplicates
                row_count = je_frame.evaluate("""
                    () => {
                        const scope = document.querySelector('#DSSJournalEntryGrid') || document;
                        return Array.from(scope.querySelectorAll('tr[role="row"]')).filter(r => {
                            const tds = r.querySelectorAll('td');
                            if (tds.length < 2) return false;
                            const clone = tds[1].cloneNode(true);
                            clone.querySelectorAll('select').forEach(s => s.remove());
                            return clone.textContent.trim().includes('4000-011');
                        }).length;
                    }
                """)
                if row_count > 0:
                    log.warning("⚠️  4000-011 already has %d row(s) but value mismatch — skipping add to avoid duplicates", row_count)
                else:
                    log.info("Adding 4000-011 Food Sales-tax exempt: %.2f (%s)", tax_exempt_amount, tax_exempt_field)
                    # Use same pattern as 8000-06: type, Enter to select, Tab to field
                    acct_input = je_frame.locator('input[placeholder="Select Account"]').first
                    acct_input.scroll_into_view_if_needed()
                    acct_input.click()
                    acct_input.fill("4000-011")
                    je_frame.wait_for_timeout(1200)
                    acct_input.press("Enter")
                    je_frame.wait_for_timeout(500)
                    # 4000-011 is a revenue account → tax_exempt_field is "credit".
                    # Column order is Account → Debit → Credit → Comment, so Tab
                    # past Debit into the Credit field before typing the amount.
                    acct_input.press("Tab")        # → Debit field (skip)
                    active.keyboard.press("Tab")   # → Credit field
                    active.keyboard.type(f"{tax_exempt_amount:.2f}")
                    je_frame.wait_for_timeout(300)
                    # Tab to Comment and fill
                    active.keyboard.press("Tab")   # → Comment
                    active.keyboard.type("Untaxed Net Sales")
                    je_frame.wait_for_timeout(300)
                    # Click Add — scoped to grid toolbar
                    je_frame.locator('.k-grid-toolbar button:has-text("Add")').click()
                    je_frame.wait_for_timeout(1000)
                    log.info("✅ 4000-011 row added: %.2f credit 'Untaxed Net Sales'", tax_exempt_amount)
        except Exception as e:
            log.warning("Could not add 4000-011 tax-exempt row: %s", e)

    _reconcile("4000-02 - Beverage Sales",        "credit", revel_values.get("beverage_sales"))
    _reconcile("4000-08 - Food Delivery Sales",   "credit", revel_values.get("delivery_food_sales"))

    # ── Other Sales (4000-07) ─────────────────────────────────────────────────
    # Add a new row when Revel "Unknown Class" taxable_sales is non-zero.
    other_sales = round(float(revel_values.get("other_sales") or 0), 2)
    if other_sales:
        try:
            existing = _read_je_cell_value(je_frame, "4000-07", "credit")
            log.info("4000-07 current R365 value: %.2f (want %.2f)", existing, other_sales)
            if existing == other_sales:
                log.info("✅ 4000-07 Other Sales already correct: %.2f", other_sales)
            elif existing:
                # Row exists but value differs — update in-place
                _fill_je_cell(je_frame, "4000-07", "credit", other_sales)
                log.info("✅ 4000-07 Other Sales updated: %.2f", other_sales)
            else:
                # Row not in grid yet — add via Select Account input
                log.info("Adding 4000-07 Other Sales: %.2f (credit)", other_sales)
                acct_input = je_frame.locator('input[placeholder="Select Account"]').first
                acct_input.scroll_into_view_if_needed()
                acct_input.click()
                acct_input.fill("4000-07")
                je_frame.wait_for_timeout(1200)
                acct_input.press("Enter")
                je_frame.wait_for_timeout(500)
                acct_input.press("Tab")   # → Debit (skip)
                active.keyboard.press("Tab")  # → Credit field
                active.keyboard.type(f"{other_sales:.2f}")
                je_frame.wait_for_timeout(300)
                # Tab past Comment (leave empty) then click Add
                active.keyboard.press("Tab")
                je_frame.locator('.k-grid-toolbar button:has-text("Add")').click()
                je_frame.wait_for_timeout(1000)
                log.info("✅ 4000-07 Other Sales row added: %.2f credit", other_sales)
        except Exception as e:
            log.warning("Could not add 4000-07 Other Sales row: %s", e)

    _reconcile("2240-000 - Sales Tax Payable",    "credit", revel_values.get("sales_tax"))

    # ── Debits ───────────────────────────────────────────────────────────────
    _reconcile("70250 - Credit Card Fees",                  "credit", revel_values.get("credit_card_fees"))
    _reconcile("1200-000 - A/R Credit Cards Receivable", "debit", revel_values.get("credit_cards_ar"))
    # Discount fields — only write if variance exists (item_discounts > 0)
    # Must target the plain (no-comment) 4500-01 row to avoid overwriting "Gift Card Redeemed" etc.
    if item_discounts:
        r365_plain = _read_je_cell_value(je_frame, "4500-01 - Discounts", "debit")
        if r365_plain != item_discounts:
            log.info("  MISMATCH %-45s debit: R365=%.2f Revel=%.2f → writing (no_comment)", "4500-01 - Discounts", r365_plain, item_discounts)
            _fill_je_cell(je_frame, "4500-01 - Discounts", "debit", item_discounts, no_comment=True)
        else:
            log.info("  MATCH    %-45s debit = %.2f (no write needed)", "4500-01 - Discounts", r365_plain)
    # 4500-02 Comps and 5000-17 Employee Discount already written above (before discount read)
    # 4500-03 Promotions — R365 pre-fills, not written by us
    _reconcile("2301 - Employee Tips Payable",              "debit",  revel_values.get("employee_tips"))

    # ── Cash Over/Short ───────────────────────────────────────────────────────
    # R365 auto-fills an "Over / Short" row at load time — it is read-only.
    # We ADD a separate "variance" row via the add-row combobox so the JE balances.
    cash_over_short      = revel_values.get("cash_over_short", 0.0)
    cash_over_short_sign = revel_values.get("cash_over_short_sign", "credit")

    if cash_over_short and cash_over_short > 0:
        existing_cos = _read_je_cell_value(je_frame, "8000-06", cash_over_short_sign)
        if existing_cos == cash_over_short:
            log.info("✅ 8000-06 Cash Over/Short already correct: %.2f", cash_over_short)
        else:
            log.info("Adding 8000-06 Cash Over/Short: %.2f (%s)", cash_over_short, cash_over_short_sign)
            try:
                acct_input = je_frame.locator('input[placeholder="Select Account"]').first
                acct_input.scroll_into_view_if_needed()
                acct_input.click()
                acct_input.fill("8000-06")
                je_frame.wait_for_timeout(1200)
                acct_input.press("Enter")
                je_frame.wait_for_timeout(500)

                acct_input.press("Tab")           # → Debit field
                if cash_over_short_sign == "credit":
                    active.keyboard.press("Tab")  # skip Debit → Credit field
                active.keyboard.type(f"{cash_over_short:.2f}")
                je_frame.wait_for_timeout(300)

                if cash_over_short_sign == "debit":
                    active.keyboard.press("Tab")  # skip Credit field
                active.keyboard.press("Tab")      # → Comment field
                active.keyboard.type("Variance")
                je_frame.wait_for_timeout(300)

                je_frame.locator('.k-grid-toolbar button:has-text("Add")').click()
                je_frame.wait_for_timeout(1000)
                log.info("8000-06 Cash Over/Short added: %.2f (%s) with comment 'Variance'",
                         cash_over_short, cash_over_short_sign)
            except Exception as e:
                log.warning("Could not add 8000-06 Cash Over/Short: %s", e)

    # Commit any open Kendo grid row-edit before saving — pressing Escape in the
    # active frame closes the inline editor without discarding the typed value
    # (Kendo commits on Tab/Enter/Escape when leaving the row).
    try:
        je_frame.keyboard.press("Escape")
        je_frame.wait_for_timeout(500)
        log.info("Pressed Escape to commit open row edit before save")
    except Exception:
        pass

    je_diff = _screenshot_je_grid(je_frame, screenshot_path)
    log.info("Journal Entry fields filled — screenshot saved: %s", screenshot_path)

    # Whether the JE balances (debit == credit) — matches the downstream
    # definition. Both the attachment upload and the Approve step are gated on
    # this: an unbalanced entry gets neither the file nor approval.
    je_balanced = round(je_diff or 0.0, 2) == 0.0

    # Approval outcome: "skipped" (unbalanced, never attempted), "approved", or
    # "failed". Surfaced to the caller so a saved-but-not-approved JE is not
    # mistaken for a fully successful reconcile.
    approved = "skipped"

    # Upload Revel xlsx attachment before saving (so it persists with the JE) —
    # but only when balanced; an unbalanced entry should not get the file.
    attachment_status = "skipped"
    if attachment_path and je_balanced:
        try:
            attachment_status = _upload_attachment(
                active, attachment_path,
                screenshot_path=attachment_screenshot_path,
            )
        except Exception as e:
            log.warning("Attachment upload error: %s", e)
            attachment_status = "failed"
    elif attachment_path and not je_balanced:
        log.info("Skipping attachment upload — JE not balanced (diff=%.2f)", je_diff or 0.0)

    # Save the DSS form — Save toolbar is a <li data-testid="saveMenuItem">, not a <button>
    try:
        saved = active.evaluate("""
            () => {
                const el = document.querySelector('[data-testid="saveMenuItem"]');
                if (!el) return 'saveMenuItem not found';
                el.click();
                return 'clicked';
            }
        """)
        log.info("Save JS click result: %s", saved)
        active.wait_for_timeout(4_000)

        # Verify save actually persisted by checking form title / dirty indicator
        dirty = active.evaluate("""
            () => {
                const title = document.title || '';
                const dirty = document.querySelector('.k-state-dirty, [data-dirty], .unsaved-indicator');
                return {title: title.slice(0, 80), dirty: !!dirty};
            }
        """)
        log.info("Post-save check — title: %s | dirty indicator: %s", dirty.get("title"), dirty.get("dirty"))
        log.info("DSS form saved")
    except Exception as e:
        log.warning("Save failed: %s", e)

    # Approve the DSS form — but ONLY when the JE balances (debit == credit).
    # An unbalanced entry must not be approved. The Approve toolbar item is a
    # <li data-testid="approveMenuItem"> with an ng-click handler, same pattern
    # as saveMenuItem (clicking the <li> fires the handler without opening the
    # dropdown). "balanced" matches the downstream definition: round(diff,2)==0.
    if je_balanced:
        try:
            # After Save, R365 re-renders the toolbar — the approveMenuItem <li>
            # is briefly absent from the DOM, so a fixed sleep + immediate click
            # races the rebuild and finds nothing ("approveMenuItem not found").
            # Wait for the item to reappear before clicking. It lives inside a
            # collapsed dropdown (visible:false), but a JS .click() on the hidden
            # <li> still fires its ng-click handler (same as saveMenuItem).
            active.wait_for_function(
                "() => !!document.querySelector('[data-testid=\"approveMenuItem\"]')",
                timeout=15_000,
            )
            # Clicking Approve fires a POST to ServiceStack/SaveTransaction. Wait
            # for that response instead of a fixed sleep — once it returns OK the
            # approval has landed and we're good to move on to the next entity.
            with active.expect_response(
                lambda r: "ServiceStack/SaveTransaction" in r.url
                          and r.request.method == "POST",
                timeout=20_000,
            ) as resp_info:
                clicked = active.evaluate("""
                    () => {
                        const el = document.querySelector('[data-testid="approveMenuItem"]');
                        if (!el) return 'approveMenuItem not found';
                        el.click();
                        return 'clicked';
                    }
                """)
                log.info("Approve JS click result: %s", clicked)
            resp = resp_info.value
            if resp.ok:
                approved = "approved"
                log.info("DSS form approved — SaveTransaction HTTP %s (JE balanced, diff=%.2f)",
                         resp.status, je_diff or 0.0)
            else:
                approved = "failed"
                log.warning("Approve SaveTransaction returned HTTP %s — approval may have failed",
                            resp.status)
        except Exception as e:
            approved = "failed"
            log.warning("Approve failed (no SaveTransaction success): %s", e)
    else:
        log.info("Skipping Approve — JE not balanced (diff=%.2f)", je_diff or 0.0)

    return je_diff, attachment_status, approved


# ─── Navigation ───────────────────────────────────────────────────────────────

def go_to_daily_sales_summary(
    page: Page,
    target_date: date | None = None,
    context=None,
    location_name: str | None = None,
    revel_values: dict | None = None,
    screenshot_path: str = "/tmp/r365_je_after.png",
    before_screenshot_path: str = "/tmp/r365_je_before.png",
    attachment_path: str | None = None,
    attachment_screenshot_path: str | None = None,
) -> tuple[float, str, str]:
    log.info("Navigating directly to Daily Sales Summary...")
    je_diff = 0.0
    attachment_status = "skipped"
    approved = "skipped"
    page.goto(DSS_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(15_000)  # let legacy iframe fully settle
    log.info("DSS page loaded — at: %s", page.url)

    dss_frame = next(
        (f for f in page.frames if "DailySalesSummariesGrid" in f.url),
        None,
    )
    if not dss_frame:
        # The DSS grid iframe never loaded — almost always because the page is
        # still on the SSO/signin-oidc redirect and never reached the real Daily
        # Sales Summary. No JE was opened, filled, saved, or approved. This is a
        # hard failure, NOT a balanced/successful reconcile: returning here with
        # je_diff=0.0 would otherwise be mistaken for "balanced". Raise so the
        # caller records it as an error with a clear reason.
        log.warning("DSS iframe not found (page at %s) — saving screenshot", page.url)
        page.screenshot(path="/tmp/r365_dss_list.png")
        raise RuntimeError(
            f"DSS grid iframe not found — page never left {page.url} "
            "(SSO redirect did not complete to Daily Sales Summary)"
        )

    if target_date:
        date_str = target_date.strftime("%-m/%-d/%Y")
        log.info("Setting date filter to: %s", date_str)

        date_filter = dss_frame.locator('input[type="text"]').nth(4)
        date_filter.click(click_count=3)
        date_filter.fill(date_str)
        date_filter.press("Tab")
        page.wait_for_timeout(3_000)
        log.info("Date filter applied")
        page.screenshot(path="/tmp/r365_dss_filtered.png")

        if location_name:
            log.info("Clicking entity for location: %s", location_name)
            entity = dss_frame.locator(
                f'table tbody tr:has(td:nth-child(3):has-text("{location_name}")) td:nth-child(5)'
            ).first
        else:
            log.info("Clicking first entity Name cell...")
            entity = dss_frame.locator('table tbody tr td:nth-child(5)').first

        try:
            entity.wait_for(timeout=15_000)

            pages_before = len(context.pages) if context else 1
            entity.click()

            active = None
            for _ in range(15):  # poll up to 15 seconds
                page.wait_for_timeout(1_000)
                if context and len(context.pages) > pages_before:
                    active = context.pages[-1]
                    active.wait_for_load_state("domcontentloaded", timeout=30_000)
                    active.wait_for_timeout(2_000)
                    log.info("Entity opened in new tab — at: %s", active.url)
                    break
                if "DailySalesSummaryForm" in page.url or "#/form" in page.url:
                    active = page
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(2_000)
                    log.info("Entity opened in same tab — at: %s", page.url)
                    break
            if active is None:
                active = page
                log.warning("Entity navigation unclear after 15s — at: %s", page.url)

            active.screenshot(path="/tmp/r365_entity.png")
            active.wait_for_timeout(8_000)

            log.info("Looking for Journal Entry tab...")
            found_je = False
            for frame in active.frames + [active]:
                try:
                    je = frame.locator('li[role="tab"] span.k-link').filter(has_text="Journal Entry").first
                    je.wait_for(timeout=3_000)
                    je.scroll_into_view_if_needed()
                    je.click()
                    active.wait_for_timeout(5_000)
                    log.info("Journal Entry tab clicked — frame: %s", frame.url)
                    active.screenshot(path=before_screenshot_path)
                    found_je = True

                    if revel_values and found_je:
                        je_diff, attachment_status, approved = fill_journal_entry(
                            active, revel_values,
                            screenshot_path=screenshot_path,
                            attachment_path=attachment_path,
                            attachment_screenshot_path=attachment_screenshot_path,
                        )
                        je_diff = je_diff or 0.0

                    break
                except Exception:
                    continue

            if not found_je:
                log.warning("Journal Entry tab not found — saving screenshot for diagnosis")
                active.screenshot(path="/tmp/r365_entity_no_je.png")

        except Exception as e:
            log.warning("Could not open entity: %s", e)
            try:
                page.screenshot(path="/tmp/r365_dss_list.png")
            except Exception:
                pass

    return je_diff, attachment_status, approved


# ─── Main entry (used by server.py) ──────────────────────────────────────────

def open_r365_journal_entry(
    target_date: date | None = None,
    location_name: str | None = None,
    revel_values: dict | None = None,
    attachment_path: str | None = None,
) -> dict:
    """
    Launch a headless browser using a persistent profile (so login is remembered),
    navigate to the DSS entity for the given date/location, open Journal Entry,
    and fill in values from Revel.
    """
    if not R365_USER or not R365_PASS:
        return {"error": "R65_USER and R65_PASS must be set in .env"}

    os.makedirs(PROFILE_DIR, exist_ok=True)

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", location_name or "unknown")
    date_str = target_date.strftime("%Y-%m-%d") if target_date else "nodate"
    before_filename     = f"r365_je_{safe_name}_{date_str}_before.png"
    after_filename      = f"r365_je_{safe_name}_{date_str}_after.png"
    attachment_filename = f"r365_attach_{safe_name}_{date_str}.png"

    # Remove any screenshots left over from a previous run for this same
    # location/date. The /tmp paths are reused across runs, so if this run aborts
    # before regenerating them (e.g. the DSS page fails to load), the UI would
    # otherwise display a stale image from an earlier run alongside this run's
    # status — exactly the "success badge + old unbalanced screenshot" mismatch.
    for _stale in (before_filename, after_filename, attachment_filename):
        try:
            os.remove(f"/tmp/{_stale}")
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove stale screenshot /tmp/%s: %s", _stale, e)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=True,
                viewport={"width": 1440, "height": 900},
                args=["--enable-features=DnsOverHttps"],
            )

            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in_r365(page, context)
            je_diff, attachment_status, approved = go_to_daily_sales_summary(
                page, target_date, context, location_name, revel_values,
                screenshot_path=f"/tmp/{after_filename}",
                before_screenshot_path=f"/tmp/{before_filename}",
                attachment_path=attachment_path,
                attachment_screenshot_path=f"/tmp/{attachment_filename}",
            )

            active_pages = context.pages
            url = active_pages[-1].url if active_pages else DSS_URL
            je_balanced = (je_diff or 0.0) == 0.0
            return {
                "status": "ok",
                "url": url,
                "before_screenshot_filename": before_filename,
                "screenshot_filename": after_filename,
                "attachment_screenshot_filename": attachment_filename if attachment_status != "skipped" else None,
                "attachment_status": attachment_status,
                "je_difference": round(je_diff or 0.0, 2),
                "je_balanced": je_balanced,
                "approved": approved,
                # A reconcile is only fully done when the JE balances AND R365
                # actually accepted the approval. Saved-but-not-approved is NOT
                # success — it leaves the DSS in "Unapproved" state.
                "approved_ok": je_balanced and approved == "approved",
            }

    except Exception as exc:
        log.error("R365 navigation error: %s", exc)
        return {"error": str(exc)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Open R365 Daily Sales Summary Journal Entry")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    result = open_r365_journal_entry(target)
    print(result)
