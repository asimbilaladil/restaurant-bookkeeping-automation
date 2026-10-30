# R365 Daily Reconcile — Failure Diagnosis & Fix Plan

> Filed 2026-06-27. Status: **investigation done, fixes NOT yet applied.**

## Symptom
Cron run on 2026-06-27 04:00 UTC (date=2026-06-26) logged
`=== Daily reconcile DONE — date=2026-06-26: 11 balanced, 0 unbalanced, 0 errored ===`
…but **all 11 locations actually failed.** No JE was opened, filled, or approved.

## Root cause
R365 returned **`400 Bad Request: Request Header Or Cookie Too Large` (nginx)** —
see `/tmp/r365_dss_list.png`. The DSS list page never loaded, so the entity-row
selector at `r365/journal_entry.py:1179` (`entity.wait_for(timeout=15_000)`) timed
out for every location (`Could not open entity: ... Timeout 15000ms exceeded`).

### Why the cookie header is too large
Persistent profile: `~/.r365_browser_profile/Default/Cookies`
- 47 cookies for `restaurant365`, ~**11 KB** total `Cookie` header (nginx limit is 8 KB).
- **44 of 47** are transient OIDC handshake cookies that should be deleted after
  login but accumulate every run:
  - `.AspNetCore.Correlation.*`
  - `.AspNetCore.OpenIdConnect.Nonce.*`

## Second bug — failures reported as success (masking)
- `je_diff` defaults to `0.0` at `r365/journal_entry.py:1142`.
- On "could not open entity", control hits the `except` at `journal_entry.py:1236`,
  logs a warning, then **returns the untouched `je_diff = 0.0`** (line 1243).
- Caller `daily_reconcile.py:71-74` reads `je_balanced` (defaults `True`) +
  `je_difference = 0.0` → `run_status = "success"` → counted as balanced.
- Result: a hard failure is indistinguishable from a genuine `diff=0.00`.

## Fix plan
1. **Immediate (unblock):** wipe cookies in `~/.r365_browser_profile` (or the whole
   profile dir), re-login, re-run for date 2026-06-26.
2. **Durable — stop accumulation:** in `ensure_logged_in_r365` (r365/session.py),
   delete stale `.AspNetCore.Correlation.*` and `.AspNetCore.OpenIdConnect.Nonce.*`
   cookies before/after each login so the header can never bloat. *(real fix)*
3. **Durable — honest reporting:** make the `except` at `journal_entry.py:1236`
   signal a real error (e.g. return `je_diff=None` / explicit error flag) so
   `daily_reconcile.py` counts it as **errored**, not balanced.

## Quick reference
- Cron: `0 4 * * *` (root crontab, CRON_TZ=UTC) → `daily_reconcile.py`
- Log: `/opt/accounts/app/logs/daily_reconcile.log`
- Failure screenshot: `/tmp/r365_dss_list.png` (overwritten each location)
- Profile/cookies: `~/.r365_browser_profile/Default/Cookies`
