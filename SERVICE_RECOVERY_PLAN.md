# Service Recovery Plan — retire orphaned port-5050 process, restore systemd ownership

**Status as of this writing:** NOT EXECUTED. Documentation only, per Phase 2.1 restrictions
(no production restart or service interruption this phase).

## Problem recap

Two completely separate process groups exist right now:

1. **The orphan** (actually serving all production traffic since 2026-08-31):
   `xvfb-run` (PID 1922321) → `Xvfb :99` (PID 1922332) → `python3 server.py` (PID 1922335).
   Not tracked by systemd at all — it predates whatever last restarted the unit and was
   never reaped.
2. **systemd's managed unit**, crash-looping every ~5 seconds (`RestartSec=5`) because every
   new `server.py` it starts immediately dies with `Address already in use` on port 5050 —
   the orphan is squatting the port. Restart counter: 502,710 on 2026-10-04 → 825,423+ now.

Net effect: the code actually running in production is whatever was on disk on 2026-08-31,
**not necessarily what's in the repo today** (8 files have local uncommitted changes, plus
this phase's `server.py`/`daily_reconcile.py` lock additions — none of that is live).

## Pre-flight checklist (before touching anything)

- [ ] Confirm current Phase 1 + Phase 2.1 backups still pass checksums (see §Backup verification below).
- [ ] Confirm no reconciliation is actively running: check `locks/` for any held lock files,
      check `/tmp/revel_session.json` mtime, check the DSS Run History page for an
      in-progress row.
- [ ] Pick a low-traffic window — the orphan currently serves the frontend; killing it drops
      service for the few seconds until systemd's next restart succeeds.
- [ ] Have this plan and the rollback step open before starting.

## Recovery steps

```bash
# 1. Snapshot the exact orphan PIDs and confirm which one owns port 5050.
ss -ltnp 2>/dev/null | grep :5050 || fuser 5050/tcp
ps -fp 1922321 1922332 1922335   # confirm these are still the same PIDs noted above

# 2. Stop systemd's restart loop FIRST, so it doesn't race the manual kill
#    with its own 5-second restart timer.
systemctl stop revel-r365.service

# 3. Terminate the orphan's process GROUP (parent xvfb-run wrapper down to the
#    python3 child and its Xvfb), gracefully first.
kill -TERM 1922321 1922332 1922335
sleep 3
ps -fp 1922321 1922332 1922335   # expect: no such process

# 3b. Only if step 3 left anything behind after a few seconds:
kill -KILL 1922321 1922332 1922335 2>/dev/null

# 4. Confirm port 5050 is now free.
ss -ltnp 2>/dev/null | grep :5050   # expect: no output

# 5. Reload unit file (picks up no changes, but is the standard hygiene step
#    after any manual intervention) and start the managed service.
systemctl daemon-reload
systemctl start revel-r365.service

# 6. Confirm it's actually up and NOT crash-looping.
sleep 3
systemctl status revel-r365.service --no-pager
journalctl -u revel-r365.service --no-pager -n 15
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:5050/login

# 7. Smoke-test the frontend in a browser: log in, open the DSS page, open
#    DSS Run History — confirm both render and the run history still shows
#    the full prior record count (compare against the DB row count you
#    checked before step 2).
```

## Verification after recovery

- [ ] `systemctl status revel-r365.service` shows `Active: active (running)` and stays that
      way for at least a few minutes (no restart-loop resuming).
* [ ] `journalctl -u revel-r365.service` shows a clean startup log line
      (`Starting Revel Fetcher server on http://localhost:5050`) with no immediately-following
      `Address already in use`.
- [ ] `sqlite3 dss_runs.db "PRAGMA integrity_check;"` still returns `ok`, and
      `SELECT COUNT(*) FROM dss_runs;` matches the pre-recovery count exactly (no data lost —
      this recovery never touches the DB, this check is just confirming nothing else did).
- [ ] Frontend login + DSS page + DSS Run History page all load correctly.
- [ ] The code now actually running is confirmed current (e.g. check a log line or behavior
      that only exists in the latest commit, since this finally replaces the Aug-31 binary).

## Rollback (if step 5 doesn't come up cleanly, or step 7 smoke-test fails)

The orphan cannot be "rolled back" — once killed in step 3 it's gone. Rollback here means:

```bash
# If the newly-started systemd-managed process is unhealthy and you need the
# service back immediately while you investigate:
systemctl stop revel-r365.service

# Restore the exact source tree that existed before recovery (if you suspect
# the uncommitted local changes are the problem, not the orphan/port issue):
tar -xzf /opt/backups/accounts-app_20261026T145945Z/source/app_source_full.tar.gz \
    -C /opt/accounts/app

systemctl start revel-r365.service
# Re-run the verification checklist above.
```

If the service still won't bind port 5050 after killing the orphan, check for a THIRD
unexpected listener (`ss -ltnp | grep :5050` again) before assuming the fix didn't work —
don't assume you've found every orphan after killing just one PID group.

## What this recovery does NOT fix by itself

- It does not change any accounting code or JE data.
- It does not retroactively know whether the orphan (running since 2026-08-31) posted any
  JEs using older logic than what's in the current repo — that's a separate audit, not part
  of this recovery.
- It does not enable the daily cron (`daily_reconcile.py`) — that stays disabled regardless.

## Explicit non-execution statement

No command in this document has been run against production during this phase. This file
exists purely so a future, explicitly-authorized recovery session has an exact, reviewed
script to execute rather than improvising live.
