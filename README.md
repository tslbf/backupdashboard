# Backup Status Dashboard

One view of every backup across Veeam, N-able Cove and Azure Backup — what
failed last night, what silently didn't run, and how any of it is trending.

Replaces three scheduled PowerShell scripts that each pulled one source into
`BackupReporting.dbo.BackupEvents` and emailed a CSV. Same APIs, ported to
Python collectors on a scheduler, plus the history those scripts never kept.

## What it answers

- **Last night** — the landing page. Which servers failed, which never ran at
  all, how many nights each has been broken, and when each last had a good
  backup. Sorted worst first.
- **Servers** — one row per machine, one cell per night, so a job that has been
  quietly degrading for two weeks is visible as a pattern rather than a number.
- **History** — nightly outcomes and backup-window duration over 14/30/90 days.
- **Collectors** — whether the data you are looking at is current.

## The part that isn't obvious

Cove protects the UK estate. A UK server backing up at 22:00 London finishes at
**17:00 Eastern** — before an Eastern "last night" window would even open, so a
naive dashboard drops it with no error and no empty row. It just looks like a
clean night.

So events aren't filed by wall-clock time. Each one is stamped with the **backup
night it belongs to**, computed in *that server's own* timezone: noon-to-noon,
locally. A UK job at 23:00 London and a Pittsburgh job at 23:00 Eastern land on
the same report date and appear in the same morning review.

Full explanation, including the two weeks a year when the US/UK gap is 4 hours
instead of 5: **[docs/timezones.md](docs/timezones.md)**.

## Architecture

```
Veeam VBR ──── REST :9419 ──┐
N-able Cove ── JSON-RPC ────┼── collectors ──► report-day ──► SQL Server ──► FastAPI ──► React UI
Azure Backup ─ ARM REST ────┤   (APScheduler)   stamping       (app DB)       /api/*
BackupReporting ─ SQL ──────┘   (one-time backfill)
```

- **Collectors** (`backend/app/collectors/`) each pull one source; a failing
  source records the error on its run row and never takes the others down.
- **`timeframes.py`** is the report-day math — the piece worth reading first.
- **`rollups.py`** materializes a row for every (night, server, source) that
  *should* have run, so a job that stopped running becomes a queryable
  `missed` instead of nothing at all.
- **`outcomes.py`** normalizes three vendors' result vocabularies ("Warning",
  status code 8, "CompletedWithWarnings") into one set of counts, keeping the
  raw vendor string alongside.

## Quick start (demo data, no credentials)

```bash
cd backend
python -m pip install -r requirements.txt
APP_DB_URL=sqlite:///./demo.db python -m app.cli seed-demo

cd ../frontend
npm install && npm run build

cd ../backend
APP_DB_URL=sqlite:///./demo.db python -m uvicorn app.main:app --port 8010
```

Open <http://localhost:8010>. On Windows this is one command:

```bat
update-and-run.cmd demo
```

That pulls, installs, builds, seeds the demo estate into its own database, and
launches. Drop `demo` once your real sources are configured.

The demo estate is ~49 servers over 75 nights, shaped to exercise the cases that
matter — UK servers on London time, two chronically failing jobs, a server that
has never once succeeded, and one whose backup silently stopped six nights ago.

## Wiring up the real sources

1. `copy backend\.env.example backend\.env` and fill in what you have. Anything
   left blank is disabled.
2. Protect the passwords: `python -m app.cli protect --show` produces a
   `dpapi:` token to paste into `.env`.
3. Validate each one by hand before trusting the schedule:
   ```
   python -m app.cli collect veeam
   python -m app.cli collect nable
   python -m app.cli collect azure
   ```
4. Backfill the existing history once, then rebuild the nights:
   ```
   python -m app.cli collect legacy
   python -m app.cli refresh
   ```
5. Start the app. Every configured source runs once a morning at `COLLECT_TIME`
   (08:00 in `DISPLAY_TIMEZONE` by default), and **Run now** on the Collectors
   page kicks any of them off by hand. The rollups refresh hourly, which is
   what turns a night with no backup into a visible "No backup" as the report
   date rolls over — different wall-clock moments for the UK and US servers.

Rolling this up into another dashboard? **[docs/integration.md](docs/integration.md)**
documents `/api/summary` and has a drop-in tile.

Starting from nothing on a fresh box? **[docs/install.md](docs/install.md)** is
the ordered runbook — prerequisites through first real collection, with the
failures we actually hit listed at the end. See
**[docs/deployment-windows.md](docs/deployment-windows.md)** for SQL Server,
running as a service, and the legacy import.

## CLI

```
python -m app.cli init-db            create tables + seed source config
python -m app.cli collect <source>   run one collector (or `all`)
python -m app.cli refresh            rebuild every night from stored events
python -m app.cli recompute          re-stamp report dates, then refresh
python -m app.cli report [--date]    print a night's summary; non-zero if problems
python -m app.cli seed-demo          load the demo estate
python -m app.cli purge <source>     delete one source's events
python -m app.cli protect            encrypt a secret for .env (Windows DPAPI)
python -m app.cli probe veeam        diagnose a Veeam connection: TCP, TLS, REST
python -m app.cli probe veeam --sessions --find NAME
                                     what Veeam's session log says about a machine
python -m app.cli probe azure        survey what is in the vaults; stores nothing
python -m app.cli notify [--print]   send the morning digest, or just show it
```

## Tests

```bash
cd backend && python -m pytest tests/
```

250 tests. The bulk of them are on the report-day math and the missed-backup
detection, because those are the two places where being wrong produces a
confident, clean-looking, incorrect dashboard.

## Stack

Python 3.11+, FastAPI, SQLAlchemy 2.0, APScheduler · React 18 + TypeScript +
Vite, plain CSS · SQLite for dev, SQL Server via pyodbc in production ·
credentials via Windows DPAPI.

The UI wears the **LB Foster Infrastructure dark ops theme**, vendored under
`frontend/src/theme/`. Brand crimson is chrome only — the logo tile, the active
nav underline, focus rings — and never a chart mark, row tint or status colour.
Same backend stack as the asset-health dashboard, so the two roll up together.

## Known limitations

- **Veeam reports one result per session**, and a session can protect several
  VMs, so every machine in a job inherits that job's verdict. Per-object results
  need task-session endpoints this API version doesn't expose. A Veeam failure
  is a pointer to the job, not proof about each guest.
- **Cove reports current state, not session history.** The collector can only
  ever see each device's latest run, so Cove history accumulates from the day it
  starts polling; anything older comes from the legacy import.
- **No app-level authentication.** Firewall-only today.
