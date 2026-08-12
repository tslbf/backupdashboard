# Running this on Windows Server

One uvicorn process serves both `/api/*` and the built React bundle, so this is
a single service — no IIS site, no second terminal.

## First run

```
git clone https://github.com/tslbf/backupdashboard C:\ManagedClient\backupdashboard
cd C:\ManagedClient\backupdashboard
copy backend\.env.example backend\.env
notepad backend\.env
update-and-run.cmd
```

Then open <http://localhost:8010>.

`update-and-run.cmd` pulls, creates `backend\.venv` if missing, installs
requirements, runs `npm install && npm run build`, and starts uvicorn. It is the
supported path; the manual equivalent is:

```
cd backend
python -m pip install -r requirements.txt
python -m app.cli init-db
cd ..\frontend && npm install && npm run build
cd ..\backend && python -m uvicorn app.main:app --port 8010
```

## Evaluating without credentials

```
update-and-run.cmd demo
```

~49 fake servers with 75 nights of history, including UK servers on London time,
two chronically failing jobs, a server that has never succeeded, and one whose
job silently stopped six nights ago.

The `demo` flag points the app at `backend\demo.db` via an environment variable,
which overrides whatever `APP_DB_URL` says in `.env`. That separation is not
cosmetic: `seed-demo` **replaces** the entire estate, so running it against a
configured database would delete real collected history. Drop the flag to go
back to the real one — nothing about it is sticky.

## The app database

SQLite is fine for evaluation. For production point `APP_DB_URL` at SQL Server:

```
APP_DB_URL=mssql+pyodbc://svc_backupdash:...@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
```

Create an empty `BackupDashboard` database; `init-db` creates the tables on
startup. The account needs `db_datareader` + `db_datawriter` + `db_ddladmin` (or
just `db_owner` on its own database).

**`IM002`** on startup means ODBC Driver 18 is not installed, or `APP_DB_URL` is
still the placeholder.

## Backfilling the existing history

The three PowerShell scripts have been writing to
`BackupReporting.dbo.BackupEvents`. Import it once:

```
python -m app.cli collect legacy
python -m app.cli refresh
```

The importer converts the stored Eastern local times back to UTC (respecting
EST/EDT per row) and maps the `Source` column onto this app's sources, so
imported rows merge with collected ones instead of duplicating. It is safe to
re-run — matching is on (source, server, start time).

Once the collectors are running, disable the scheduled tasks for the three
scripts. Leave `LEGACY_INTERVAL=0` so the historical table is never re-walked on
a schedule.

## Credentials

No plaintext passwords. Every credential setting accepts `dpapi:<token>` or
`file:<path>`:

```
python -m app.cli protect --show
```

Paste the output into `backend\.env`. DPAPI tokens are decryptable only by the
account that created them, on that machine — so generate them as the account
that will run the service. `--show` echoes the typed value, because pasting into
a hidden prompt on some Windows consoles injects a `\x16` control character
(there is a check for that).

## Running as a service

With [NSSM](https://nssm.cc/):

```
nssm install BackupDashboard "C:\ManagedClient\backupdashboard\backend\.venv\Scripts\python.exe"
nssm set BackupDashboard AppParameters "-m uvicorn app.main:app --host 0.0.0.0 --port 8010"
nssm set BackupDashboard AppDirectory "C:\ManagedClient\backupdashboard\backend"
nssm set BackupDashboard AppStdout "C:\ManagedClient\backupdashboard\logs\out.log"
nssm set BackupDashboard AppStderr "C:\ManagedClient\backupdashboard\logs\err.log"
nssm start BackupDashboard
```

Run the service as the account whose DPAPI tokens are in `.env`, and open the
port in Windows Firewall only if you need to reach it from another machine.

There is **no app-level authentication** — deploy behind the firewall, and treat
IIS/Windows auth or Entra SSO as the follow-up.

## Scheduling

The in-process APScheduler runs each configured collector on its interval
(default 60 min) and refreshes the rollups at 20 past every hour. Nothing
external is needed. `SCHEDULER_ENABLED=false` turns it off if you would rather
drive collection from Task Scheduler:

```
python -m app.cli collect all
```

## A morning email

`python -m app.cli report` prints the same summary the landing page shows and
exits non-zero when anything needs attention, so it drops straight into a
scheduled task:

```
python -m app.cli report --date 2026-08-12
```

## Windows notes

- `pip install ...` can hit "Access is denied" under policy; `python -m pip
  install ...` works.
- "Python was not found" is almost always an unactivated venv or the Microsoft
  Store alias.
- `npm install` must run **before** `npm run build` after any pull that adds a
  dependency, or the build fails with "Rollup failed to resolve import".
  `update-and-run.cmd` already does both.
- Windows ships no IANA timezone database. `tzdata` is in `requirements.txt` and
  is **not optional** — without it `Europe/London` will not resolve and the
  report-day math falls back to Eastern for every server.
