"""
Thin client for the R365 Public REST API (https://docs.restaurant365.com/apidocs).

Currently used as a READ-ONLY replacement for scraping the DSS Journal Entry
grid via the DOM (see journal_entry.py's _read_je_cell_value). There is no
PATCH/approve endpoint for journal-entries in this API — only POST (create new)
and GET /accounting/v1/transactions (read any existing transaction, including
JEs). The write/approve flow still has to go through the browser.

Env vars required (see .env):
    R365_API_BASE_URL   e.g. https://api.restaurant365.com/public
    R365_API_TOKEN       bearer token
    R365_API_TENANT_ID   x-r365-context-tenant-id header value
    R365_API_SECURITY_ID x-r365-context-security-id header value
"""

import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, timedelta

log = logging.getLogger(__name__)

BASE_URL    = os.environ.get("R365_API_BASE_URL", "").rstrip("/")
TOKEN       = os.environ.get("R365_API_TOKEN", "")
TENANT_ID   = os.environ.get("R365_API_TENANT_ID", "")
SECURITY_ID = os.environ.get("R365_API_SECURITY_ID", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "x-r365-context-tenant-id": TENANT_ID,
        "x-r365-context-security-id": SECURITY_ID,
    }


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    if params:
        # urlencode with doseq=True so list-valued params (e.g. TransactionTypes)
        # become repeated ?key=a&key=b query args.
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"R365 API GET {path} failed: HTTP {e.code} — {body}") from e


def list_locations() -> list[dict]:
    """All locations. Each item: {id, name, number, timeZone, addresses}."""
    return _get("v1/core/locations").get("items", [])


def find_location_id(name_or_number: str, locations: list[dict] | None = None) -> str | None:
    """Match by exact number (e.g. 'TX11') or case-insensitive substring of name."""
    locations = locations if locations is not None else list_locations()
    for loc in locations:
        if loc.get("number") == name_or_number:
            return loc["id"]
    needle = name_or_number.lower()
    for loc in locations:
        if needle in loc.get("name", "").lower():
            return loc["id"]
    return None


def get_transactions(
    location_id: str,
    start: date,
    end: date,
    transaction_types: list[str] | None = None,
    include_details: bool = True,
    include_distributions: bool = True,
    page_size: int = 250,
) -> list[dict]:
    """
    GET /accounting/v1/transactions, paginated via nextLink/ContinuationToken.
    NOTE: a same-day start==end range has been observed to return empty even
    when data exists for that day — callers should pass a range spanning at
    least a few days and filter the result by dateOfBusiness themselves.
    """
    params = {
        "LocationId": location_id,
        "DateOfBusinessStart": start.isoformat(),
        "DateOfBusinessEnd": end.isoformat(),
        "IncludeDetails": str(include_details).lower(),
        "IncludeDistributions": str(include_distributions).lower(),
        "PageSize": page_size,
    }
    if transaction_types:
        params["TransactionTypes"] = transaction_types

    out = []
    token = None
    while True:
        if token:
            params["ContinuationToken"] = token
        data = _get("accounting/v1/transactions", params=params)
        out.extend(data.get("transactions", []) or [])
        next_link = data.get("nextLink")
        if not next_link:
            break
        # nextLink is a full URL with its own ContinuationToken; extract it.
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(next_link).query)
        token = qs.get("ContinuationToken", [None])[0]
        if not token:
            break
    return out


# Any of these account numbers appearing in a JournalEntry's detail lines marks
# it as the DSS-generated JE (as opposed to same-day payroll JEs, which post
# only to 5500-xx labor accounts, or empty placeholder JEs). Sales tax payable
# is the most reliable single marker since every DSS JE carries it.
_DSS_JE_MARKER_ACCOUNTS = {"2240-000", "4000-01", "4000-02"}


def get_je_for_date(location_id: str, target_date: date, window_days: int = 3) -> dict | None:
    """
    Return the DSS-generated JournalEntry transaction (with details) whose
    dateOfBusiness matches target_date for this location, or None if not found.
    Queries a window around target_date since narrow single-day ranges have
    returned empty in testing.

    A single date/location can have MULTIPLE JournalEntry transactions (e.g. a
    same-day payroll JE posting only to labor accounts, or an empty placeholder)
    — these are filtered out by requiring one of _DSS_JE_MARKER_ACCOUNTS among
    the detail lines. This requires details=true internally regardless of what
    the caller wants back.
    """
    start = target_date - timedelta(days=window_days)
    end = target_date + timedelta(days=window_days)
    txns = get_transactions(location_id, start, end, transaction_types=["JournalEntry"], include_details=True)
    same_day = [
        t for t in txns
        if (t.get("dateOfBusiness") or "").startswith(target_date.isoformat())
    ]
    matches = [
        t for t in same_day
        if any((d.get("glAccount") or {}).get("number") in _DSS_JE_MARKER_ACCOUNTS for d in (t.get("details") or []))
    ]
    if not matches:
        if same_day:
            log.warning(
                "No DSS-marker JE among %d same-day JournalEntry txn(s) for location=%s date=%s: %s",
                len(same_day), location_id, target_date, [t["number"] for t in same_day],
            )
        return None
    if len(matches) > 1:
        log.warning(
            "Multiple DSS-marker JournalEntry transactions found for location=%s date=%s: %s — "
            "returning most recently modified",
            location_id, target_date, [m["number"] for m in matches],
        )
        matches.sort(key=lambda t: t.get("modifiedOn") or "", reverse=True)
    return matches[0]


def je_lines_by_account(je: dict) -> dict:
    """
    Convert a transaction's `details` list into {account_number: {"debit":, "credit":, "comment":}}.
    When an account number repeats (e.g. two 4500-01 rows, one with a comment),
    later rows are kept under a compound key f"{number}|{comment}" so callers
    can still find the plain (no-comment) row via lookup(number, comment="").
    """
    out = {}
    for d in je.get("details") or []:
        acct = (d.get("glAccount") or {}).get("number", "")
        comment = (d.get("comment") or "").strip()
        key = acct if acct not in out else f"{acct}|{comment}"
        out[key] = {"debit": round(d.get("debit", 0.0), 2), "credit": round(d.get("credit", 0.0), 2), "comment": comment}
    return out


if __name__ == "__main__":
    import sys
    import json as _json

    logging.basicConfig(level=logging.INFO)
    loc_query = sys.argv[1] if len(sys.argv) > 1 else "TX11"
    date_str  = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()

    loc_id = find_location_id(loc_query)
    if not loc_id:
        print(f"Location not found: {loc_query}")
        sys.exit(1)
    je = get_je_for_date(loc_id, date.fromisoformat(date_str))
    if not je:
        print(f"No JournalEntry found for {loc_query} on {date_str}")
        sys.exit(1)
    print(f"JE {je['number']} — debit={je['debitTotal']} credit={je['creditTotal']} status={je['status']}")
    for acct, vals in je_lines_by_account(je).items():
        print(f"  {acct:30s} debit={vals['debit']:>10.2f} credit={vals['credit']:>10.2f}  {vals['comment']}")
