"""
server.py
=========
Flask API server that exposes the Revel fetcher over HTTP.

Endpoints:
    POST /api/fetch
        Body: { "start_date": "2026-05-30" }
        Streams SSE progress events, then final JSON results.

    GET  /api/establishments
        Returns the list of establishment IDs.

    GET  /
        Serves the frontend HTML.

Run:
    python server.py
    # then open http://localhost:5050
"""

import json
import logging
import os
import queue
import threading
from datetime import date
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, Response, send_from_directory, session, redirect, url_for, render_template_string
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

from revel import fetch_reports, DEFAULT_ESTABLISHMENTS, ESTABLISHMENT_NAMES
from r365 import open_r365_journal_entry

load_dotenv()

# ─── App setup ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".")
app.secret_key = os.environ["FLASK_SECRET_KEY"]
CORS(app)

_LOGIN_USERNAME = os.environ["LOGIN_USERNAME"]
_LOGIN_PASSWORD_HASH = os.environ["LOGIN_PASSWORD_HASH"]


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == _LOGIN_USERNAME and check_password_hash(_LOGIN_PASSWORD_HASH, password):
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Invalid username or password."
    login_html = Path("login.html").read_text()
    return render_template_string(login_html, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/daily-sales-reconciliation")
@login_required
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/establishments")
@login_required
def establishments():
    return jsonify({
        "establishments": DEFAULT_ESTABLISHMENTS,
        "names": ESTABLISHMENT_NAMES,
    })


@app.route("/api/fetch", methods=["POST"])
@login_required
def fetch():
    """
    Accepts { "start_date": "YYYY-MM-DD", "establishments": [32, 14, ...] }
    Returns Server-Sent Events stream:
      - progress events as each establishment completes
      - a final 'done' event with full results array
    """
    body = request.get_json(force=True)

    if not body or "start_date" not in body:
        return jsonify({"error": "start_date is required"}), 400

    try:
        start_date = date.fromisoformat(body["start_date"])
    except ValueError:
        return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400

    # Also include name in each result for the frontend
    raw_ests = body.get("establishments", DEFAULT_ESTABLISHMENTS)
    try:
        establishments_list = [int(e) for e in raw_ests]
    except (TypeError, ValueError):
        return jsonify({"error": "establishments must be a list of integers"}), 400

    # Use a queue to pass progress events from the fetcher thread to SSE stream
    event_queue: queue.Queue = queue.Queue()

    def progress_callback(est_id, status, result):
        event_queue.put({
            "type": "progress",
            "establishment_id": est_id,
            "status": status,
            "range_from": result.get("range_from"),
            "range_to": result.get("range_to"),
            "error": result.get("error"),
            # Send a lightweight summary — full data comes in 'done' event
            "summary": _summarise(result.get("data")),
        })

    def run_fetch():
        try:
            results = fetch_reports(start_date, establishments_list, progress_callback)
            event_queue.put({"type": "done", "results": results})
        except Exception as exc:
            log.error("fetch_reports error: %s", exc)
            event_queue.put({"type": "error", "message": str(exc)})

    thread = threading.Thread(target=run_fetch, daemon=True)
    thread.start()

    def stream():
        while True:
            try:
                event = event_queue.get(timeout=120)  # 2-min timeout per event
            except queue.Empty:
                yield "event: error\ndata: {\"message\": \"timeout\"}\n\n"
                break

            event_type = event.get("type", "message")
            data = json.dumps(event, default=str)
            yield f"event: {event_type}\ndata: {data}\n\n"

            if event_type in ("done", "error"):
                break

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/screenshots/<path:filename>")
@login_required
def screenshot(filename):
    return send_from_directory("/tmp", filename)


@app.route("/api/r365/navigate", methods=["POST"])
@login_required
def r365_navigate():
    """
    Body: { "date": "2026-05-22", "location_name": "LCF Airtex", "revel_data": {...} }
    Opens a headless R365 browser, logs in, navigates to DSS, and fills the Journal Entry.
    revel_data is the raw Revel operations report JSON for a single establishment.
    """
    body = request.get_json(force=True) or {}
    target_date = None
    if "date" in body:
        try:
            target_date = date.fromisoformat(body["date"])
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    location_name = body.get("location_name")
    revel_data = body.get("revel_data") or {}
    revel_values = _extract_revel_values(revel_data)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(open_r365_journal_entry, target_date, location_name, revel_values)
        result = future.result(timeout=300)

    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/r365/reconcile-all", methods=["POST"])
@login_required
def r365_reconcile_all():
    """
    Body: { "date": "2026-05-22", "establishments": [{"id": 32, "name": "LCF Airtex", "data": {...}}, ...] }
    Streams SSE events:
      event: r365_progress  data: {"establishment_id": 32, "status": "running"|"success"|"error", "error": "..."}
      event: r365_done      data: {}
    Processes entities sequentially (one browser session at a time).
    """
    body = request.get_json(force=True) or {}
    date_str = body.get("date")
    establishments = body.get("establishments", [])

    if not date_str:
        return jsonify({"error": "date is required"}), 400
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    event_queue: queue.Queue = queue.Queue()

    def run_all():
        for est in establishments:
            est_id = est.get("id")
            name = est.get("name")
            data = est.get("data") or {}
            revel_values = _extract_revel_values(data)

            event_queue.put({"type": "r365_progress", "establishment_id": est_id, "status": "running"})
            try:
                result = open_r365_journal_entry(target_date, name, revel_values)
                if "error" in result:
                    event_queue.put({"type": "r365_progress", "establishment_id": est_id,
                                     "status": "error", "error": result["error"]})
                else:
                    before = result.get("before_screenshot_filename")
                    after  = result.get("screenshot_filename")
                    event_queue.put({
                        "type": "r365_progress",
                        "establishment_id": est_id,
                        "status": "success",
                        "r365_url": result.get("url"),
                        "before_screenshot_url": f"/screenshots/{before}" if before else None,
                        "screenshot_url": f"/screenshots/{after}" if after else None,
                    })
            except Exception as exc:
                log.error("R365 reconcile error for est %s: %s", est_id, exc)
                event_queue.put({"type": "r365_progress", "establishment_id": est_id,
                                 "status": "error", "error": str(exc)})

        event_queue.put({"type": "r365_done"})

    thread = threading.Thread(target=run_all, daemon=True)
    thread.start()

    def stream():
        while True:
            try:
                event = event_queue.get(timeout=300)  # 5-min max per entity
            except queue.Empty:
                yield 'event: error\ndata: {"message": "timeout"}\n\n'
                break

            event_type = event.get("type", "message")
            data = json.dumps(event, default=str)
            yield f"event: {event_type}\ndata: {data}\n\n"

            if event_type in ("r365_done", "error"):
                break

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_revel_values(data: dict) -> dict:
    """
    Extract all Journal Entry field values from a Revel operations report JSON.

    Returns a dict with keys matching fill_journal_entry() expectations.
    """
    if not data:
        return {}

    sd = data.get("sales_data") or {}
    product_mix = data.get("product_mix_data") or []
    tax_data = data.get("tax_data") or []
    log.info("sales_data keys: %s", sorted(sd.keys()))
    log.info("sales_data adj/tip/fee fields: %s", {k: v for k, v in sd.items() if "adj" in k.lower() or "tip" in k.lower() or "fee" in k.lower()})

    def f(v):
        """Safe float conversion."""
        try:
            return round(float(v or 0), 2)
        except (TypeError, ValueError):
            return 0.0

    # ── Sales by Class (Gross Sales column = price) ───────────────────────────
    class_map = {
        row["product_class"]: row
        for row in product_mix
        if row.get("row_type") == "Class"
    }
    food_sales      = f(class_map.get("1. Food", {}).get("price", 0))
    beverage_sales  = f(class_map.get("2. Beverage", {}).get("price", 0))
    delivery_sales  = f(class_map.get("5. Delivery - Food", {}).get("price", 0))

    # ── Untaxed Net Sales (net_sales_untaxed) ─────────────────────────────────
    # 4000-011 Food Sales-tax exempt is always a DEBIT row with abs(net_sales_untaxed).
    # 4000-01 Food Sales credit = product_mix price + abs(net_sales_untaxed)
    # (the untaxed amount is separated into its own row so we add it back to food_sales)
    net_sales_untaxed = f(sd.get("net_sales_untaxed", 0))
    if net_sales_untaxed != 0:
        tax_exempt_amount = round(abs(net_sales_untaxed), 2)
        food_sales        = round(food_sales + tax_exempt_amount, 2)
        tax_exempt_field  = "debit"
    else:
        tax_exempt_field  = None
        tax_exempt_amount = 0.0

    # ── Tax ───────────────────────────────────────────────────────────────────
    tax_total = 0.0
    for row in tax_data:
        if row.get("row_type") == "totals_row":
            tax_total = f(row.get("tax", 0))
            break

    # ── Credit Cards AR = credit sales + credit tips - credit refunds ─────────
    credit_sales   = f(sd.get("credit_total", 0))
    credit_tips    = f(sd.get("credit_tips_total", 0))
    credit_refunds = f(sd.get("credit_refunds", 0))
    credit_cards_ar = round(credit_sales + credit_tips - credit_refunds, 2)

    # ── Marketplace payments (custom_payments dict) ───────────────────────────
    custom = sd.get("custom_payments") or {}
    uber_eats = 0.0
    doordash  = 0.0
    grubhub   = 0.0
    for payment in custom.values():
        name = (payment.get("name") or "").strip().lower()
        total = f(payment.get("total", 0))
        if "uber eats" in name or name == "uber eats":
            uber_eats += total
        elif "door dash" in name or "doordash" in name or "dd marketplace" in name:
            doordash += total
        elif "grub hub" in name or "grubhub" in name:
            grubhub += total
    uber_eats = round(uber_eats, 2)
    doordash  = round(doordash, 2)
    grubhub   = round(grubhub, 2)

    # ── Cash / Undeposited Funds ──────────────────────────────────────────────
    undeposited_funds = f(sd.get("cash_for_sales", 0))

    # ── Discounts ─────────────────────────────────────────────────────────────
    # Mapping rules (confirmed):
    #   5000-17 Employee Discount  ← "Employee $9.79 Off"
    #   4500-03 Promotions         ← any "Meal" reason (e.g. "Free 3 Finger Meal - 1",
    #                                 "3 Finger Meal - 1", "Chicken Wrap Meal - 1", etc.)
    #                                 and Loyalty total group
    #   4500-02 Comps              ← "Manager 100%"
    #   4500-01 Discounts          ← everything else (remainder: Standard group items
    #                                 minus Employee Discount and Comps)
    #
    # Strategy: use is_total rows as group subtotals to cross-check, but compute
    # each bucket by summing individual (non-total) rows.
    #
    # 4500-01 Discounts = Standard_total - employee_discount - comps
    # This ensures the three Standard-group rows always sum correctly even if
    # new discount reasons are introduced, without requiring us to enumerate them.
    discounts_data = data.get("discounts_data") or []

    COMPS_REASONS    = {"manager 100%"}
    # Match any reason starting with "employee" — covers "$9.79 Off", "50%", bare "Employee", etc.
    def _is_employee_discount(reason_lower: str) -> bool:
        return reason_lower.startswith("employee")

    # Collect group subtotals from is_total rows.
    # Promotions (4500-03) = loyalty_total directly — the Loyalty group contains
    # all named item giveaways (Free meals, sandwiches, etc.) regardless of reason name.
    # This avoids having to enumerate every possible promo reason across locations.
    standard_total = 0.0
    loyalty_total  = 0.0
    for row in discounts_data:
        if not row.get("is_total"):
            continue
        reason = (row.get("reason") or "").strip().lower()
        if reason == "standard":
            standard_total = f(row.get("amount", 0))
        elif reason == "loyalty":
            loyalty_total = f(row.get("amount", 0))

    # Sum Standard-group named buckets from individual (non-total) rows
    comps = employee_discount = app_reward = 0.0
    for row in discounts_data:
        if row.get("is_total"):
            continue
        reason = (row.get("reason") or "").strip().lower().split("\n")[0]
        amount = f(row.get("amount", 0))
        if "remake" in reason:
            continue  # manual entry, skip
        elif reason in COMPS_REASONS:
            comps += amount
        elif _is_employee_discount(reason):
            employee_discount += amount
        elif reason == "app reward":
            app_reward += amount  # R365 pre-fills this row — track but don't write

    comps             = round(comps, 2)
    employee_discount = round(employee_discount, 2)
    app_reward        = round(app_reward, 2)

    # 4500-03 Promotions = loyalty_total (the full Loyalty group subtotal).
    # All Loyalty group items are promos regardless of reason name.
    promotions = loyalty_total

    # Grand total of all Revel discounts (Standard + Loyalty).
    # journal_entry.py subtracts individual buckets + R365 promotions to get the remainder.
    revel_discounts_total = round(standard_total + loyalty_total, 2)

    # 4500-01 Discounts = Standard total − employee_discount − comps − app_reward
    # Everything else in Standard (Police/Fire, Senior, No drink, DSP, meal combos, etc.)
    item_discounts = round(max(standard_total - employee_discount - comps - app_reward, 0.0), 2)

    # ── Employee Tips ─────────────────────────────────────────────────────────
    # The read-only 2301 row = tips_total (auto-filled by R365, e.g. 24.75) — skip
    employee_tips = f(sd.get("adj_credit_tips", 0))

    # ── Cash Over/Short variance ──────────────────────────────────────────────
    # net_account_for = what Revel says should have been collected
    # total_payments  = what was actually collected
    net_account_for = f(sd.get("net_account_for", 0))
    total_payments  = f(sd.get("total_payments", 0))
    raw_variance    = round(net_account_for - total_payments, 2)
    cash_over_short = abs(raw_variance)
    # If payments > net: overage → credit Cash O/S; if payments < net: shortage → debit
    cash_over_short_sign = "credit" if raw_variance < 0 else "debit"

    return {
        "credit_card_fees":     f(sd.get("adj_credit_tips", 0)),
        "food_sales":           food_sales,
        "tax_exempt_field":     tax_exempt_field,
        "tax_exempt_amount":    tax_exempt_amount,
        "beverage_sales":       beverage_sales,
        "delivery_food_sales":  delivery_sales,
        "sales_tax":            tax_total,
        "credit_cards_ar":      credit_cards_ar,
        "uber_eats":            uber_eats,
        "doordash":             doordash,
        "grubhub":              grubhub,
        "undeposited_funds":    undeposited_funds,
        "comps":                comps,
        "item_discounts":       item_discounts,  # fallback only; journal_entry.py recalculates using R365 promotions
        "revel_discounts_total": revel_discounts_total,
        "discounts_data":        discounts_data,
        "app_reward":           app_reward,
        "employee_discount":    employee_discount,
        "promotions":           promotions,  # not written to R365 — used for reference only
        "employee_tips":        employee_tips,
        "cash_over_short":      cash_over_short,
        "cash_over_short_sign": cash_over_short_sign,
    }


def _summarise(data) -> dict | None:
    """Extract lightweight summary from operations report data for SSE progress events."""
    if data is None:
        return None
    if isinstance(data, dict):
        sd = data.get("sales_data") or {}
        return {
            "total_sales":    sd.get("total_sales") or sd.get("gross_sales"),
            "total_payments": sd.get("total_payments"),
            "total_tax":      next(
                (r.get("tax") for r in (data.get("tax_data") or []) if r.get("row_type") == "totals_row"),
                None,
            ),
        }
    return {"raw_type": type(data).__name__}


# ─── Entry ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting Revel Fetcher server on http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
