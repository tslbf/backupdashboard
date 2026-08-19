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

SQLite is fine for evaluation. For production point `APP_DB_URL` at SQL Server.
The empty slot before `@` means **Windows authentication** — the same thing the
PowerShell scripts do with `Integrated Security=True`:

```
APP_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
```

SQLAlchemy adds `Trusted_Connection=Yes` itself when the username is empty —
adding it by hand just duplicates it. The connection runs as whichever account
launches the app, so under NSSM it is the *service* account that needs the SQL
rights, not your login.

**This is not `BackupReporting`.** That is the legacy database the PowerShell
scripts write, and this app only ever reads it, once, for the backfill (see
`LEGACY_DB_URL`). Create a separate empty `BackupDashboard`; `init-db` creates
the tables on startup. The account needs `db_datareader` + `db_datawriter` +
`db_ddladmin`, or just `db_owner` on that one database.

Pointing both settings at `BackupReporting` does work — the app's tables land
alongside `dbo.BackupEvents` — but you then have two schemas in one database and
a messier cleanup when the old scripts are retired.

Two syntax traps:

- **SQL authentication** puts the password in the URL, so any `@ : / ?` in it
  must be percent-encoded (`@` → `%40`, `:` → `%3A`) or it is parsed as the host.
  Windows auth avoids this entirely.
- **A named instance** needs a literal backslash —
  `//@AZUSCCM01\SQLEXPRESS/...`. Percent-encoded `%5C` is *not* decoded and
  silently produces a broken server name.

**`IM002`** on startup means ODBC Driver 18 is not installed, or `APP_DB_URL` is
still the placeholder.

## Backfilling the existing history

The three PowerShell scripts have been writing to
`BackupReporting.dbo.BackupEvents`. Import it once:

```
python -m app.cli collect legacy
python -m app.cli refresh
```

with, in `.env`:

```
LEGACY_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupReporting?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
LEGACY_TABLE=dbo.BackupEvents
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

## Moving an existing install to the server

Running it on a desktop is fine for getting it working; it stops being fine the
moment the answer has to be there at 8am whether or not you are logged in.

**First, find out whether there is any data to move at all.** On the desktop:

```
type backend\.env | findstr APP_DB_URL
```

If it names SQL Server — `mssql+pyodbc://@AZUSCCM01/BackupDashboard...` — then
**nothing needs migrating**. Every event, server, rollup and collector run is
already on AZUSCCM01; the desktop was only ever a process pointing at it. Point
the server at the same URL and it picks up the whole history.

If it says `sqlite:///./backupdashboard.db`, the history is in that one file and
it does need to come across (see below).

### What does *not* travel

- **`backend\.venv`** — rebuild it. It has absolute paths and compiled wheels
  baked in. `update-and-run.cmd` creates it.
- **`frontend\node_modules`, `frontend\dist`** — rebuilt by the same command.
- **`dpapi:` tokens in `.env`** — see below. This is the one that bites.

### The steps

1. **Stop the desktop instance.** Two schedulers against one database is not
   corrupting — the upserts are idempotent — but you get two morning digests and
   interleaved collector runs, and it becomes unclear which machine collected
   what.

2. **Clone on the server.** A fresh clone lands on the right branch by itself.

   ```
   git clone https://github.com/tslbf/backupdashboard C:\ManagedClient\backupdashboard
   ```

3. **Copy `backend\.env` across**, then **re-protect every secret**. DPAPI
   tokens decrypt only for the user who created them, on the machine they were
   created on — a token from your desktop is inert on the server, and the app
   says so rather than failing quietly. For each of `VEEAM_PASSWORD`,
   `NABLE_PASSWORD`, `AZURE_CLIENT_SECRET` and `SMTP_PASSWORD`, run **as the
   account the app will run as**:

   ```
   cd /d C:\ManagedClient\backupdashboard\backend
   .venv\Scripts\python.exe -m app.cli protect --show
   ```

   Use `--machine` if a service account will run it and you are setting it up
   from your own login — that scope decrypts for any account on that box.

4. **Grant the new account SQL rights.** The connection is made by whichever
   account the *process* runs as, so the server's account is a different login
   from your desktop one. Edit the name at the top of `docs\sql\grant-access.sql`
   and run it — once for your login while testing by hand, again for the service
   account. Skipping this is the `4060` error.

5. **Run it.**

   ```
   cd /d C:\ManagedClient\backupdashboard
   update-and-run.cmd
   ```

   The Collectors page should show the same run history the desktop had, because
   it is the same database.

### If you were on SQLite

Copy `backend\backupdashboard.db` to the same path on the server and it works as
is. Better, while you are moving anyway, is to point `APP_DB_URL` at SQL Server
and re-collect: Veeam and Azure will refill the last 96 hours by themselves, the
legacy import refills everything older, and only Cove history is lost — it has no
history endpoint, so its past nights cannot be re-fetched from anywhere.

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

The in-process APScheduler runs every configured collector once a morning at
`COLLECT_TIME` (08:00 in `DISPLAY_TIMEZONE`), then rebuilds the rollups, then
sends the digest — in that order, so the email is never built from a
half-collected night. Cove also polls every four hours on top, because its API
reports only the latest session and a poll that does not happen loses that night
for good.

The rollups refresh hourly regardless: report dates roll over at different
wall-clock times for the UK and US servers, so a missed night has to be able to
appear within the hour.

Nothing external is needed. `SCHEDULER_ENABLED=false` turns it off if you would
rather drive collection from Task Scheduler:

```
python -m app.cli collect all
```

## A morning email

Built in — see [install.md](install.md#9-the-morning-digest). Set `SMTP_HOST` and
`SMTP_TO` and it goes out after the daily collection finishes. Check what it
would say without mailing anyone:

```
python -m app.cli notify --print
```

`python -m app.cli report` still prints the same summary to the console and exits
non-zero when anything needs attention, if you would rather drive it from a
scheduled task.

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
