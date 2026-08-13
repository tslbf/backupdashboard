# Installing on a new machine

The ordered runbook. Everything here has been run at least once; the
troubleshooting table at the end is the list of things that actually went wrong.

Assumes Windows Server. For the service-account and NSSM details see
[deployment-windows.md](deployment-windows.md).

---

## 1. Prerequisites

Install these first — `update-and-run.cmd` checks for them and names the missing
one, but it can't install them for you.

| | Why | Check |
|---|---|---|
| **Python 3.11+** | the backend | `python --version` |
| **Node 18+** | builds the web UI | `node --version` |
| **Git** | pulls the code | `git --version` |
| **ODBC Driver 18 for SQL Server** | only if using SQL Server | see below |

```bat
py -3 --version
node --version
git --version
```

### The Python one is worth reading

If `python --version` answers with

> Python was not found; run without arguments to install from the Microsoft
> Store, or disable this shortcut from Settings > Apps > Advanced app settings >
> App execution aliases

then Python is not so much missing as **shadowed**. Windows ships a stub called
`python.exe` in `WindowsApps` whose only function is to advertise the Store, and
it sits ahead of a real install on `PATH`. It exists, so `where python` finds it;
it just doesn't work. Two fixes, do both:

1. **Settings → Apps → Advanced app settings → App execution aliases** — switch
   off both `python.exe` entries.
2. Install Python 3.11+ from [python.org](https://www.python.org/downloads/) with
   **"Add python.exe to PATH"** ticked.

Then **open a new terminal**. A `PATH` change does not reach windows that are
already open, so re-running in the same console fails identically and looks like
the fix didn't take.

`py -3 --version` is the more reliable check of the two: the `py` launcher is
registered by every python.org install and the Store alias cannot shadow it.
`update-and-run.cmd` tries `py -3` first for the same reason, and proves each
candidate by running it rather than trusting `where`.

ODBC Driver 18 ([download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)) —
verify it registered:

```bat
powershell -c "Get-OdbcDriver -Name '*SQL Server*' | Select-Object Name"
```

Skip it entirely if you're evaluating on SQLite.

---

## 2. Get the code

```bat
git clone https://github.com/tslbf/backupdashboard C:\ManagedClient\backupdashboard
cd /d C:\ManagedClient\backupdashboard
```

---

## 3. Prove the stack works, before any credentials

Do this first. It builds the venv, installs everything, compiles the UI, and
loads a fake estate into its own SQLite file. If this works, every later problem
is configuration rather than installation — which is worth knowing before you
start chasing vendor APIs.

```bat
update-and-run.cmd demo
```

Open <http://localhost:8010>. You should see ~49 servers over 75 nights.
Ctrl+C to stop.

The demo data lives in `backend\demo.db` and is reached through an environment
variable that overrides `.env`, so it can never touch a real database.

---

## 4. The database

Skip this section to stay on SQLite — the app defaults to
`backend\backupdashboard.db` and needs nothing.

For SQL Server, in **SSMS against your server**:

1. Run **`docs\sql\create-database.sql`** — creates `BackupDashboard`, its
   schema, and a service account.
2. Edit the account name at the top of **`docs\sql\grant-access.sql`** and run
   it **once per account that will run the app**:
   - your own login, while you're testing by hand
   - the service account, once it runs under NSSM

Step 2 is the one people skip. With Windows authentication the connection is
made by whichever account the *process* runs as, so creating a service account
grants nothing to you sitting at the console.

`BackupDashboard` and `BackupReporting` are **different databases**. The second
is where the PowerShell scripts write; this app only ever reads it, once, for
the history import.

---

## 5. Configure

```bat
copy backend\.env.example backend\.env
notepad backend\.env
```

Anything left blank is disabled, so you can wire up one source at a time.

For SQL Server, the empty slot before `@` means Windows auth — the same thing
the PowerShell scripts do with `Integrated Security=True`:

```
APP_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
LEGACY_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupReporting?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
LEGACY_TABLE=dbo.BackupEvents
```

Then the vendor settings — `VEEAM_SERVERS`, `NABLE_PARTNER`, `AZURE_TENANT_ID`
and friends. Leave the passwords blank for now; the next step fills them in.

---

## 6. Protect the credentials

No plaintext passwords. Run this **as the account that will run the app**, once
per secret:

```bat
cd /d C:\ManagedClient\backupdashboard\backend
.venv\Scripts\python.exe -m app.cli protect --show
```

Paste each `dpapi:...` token into `.env`:

```
VEEAM_PASSWORD=dpapi:AQAAANCMnd8...
NABLE_PASSWORD=dpapi:AQAAANCMnd8...
AZURE_CLIENT_SECRET=dpapi:AQAAANCMnd8...
```

DPAPI tokens decrypt only for the user who created them, on that machine — so
generating them as yourself and then running as a service account will fail.
`--machine` scope makes a token work for any account on that box, which is what
you want if you'll switch to a service account later.

Use `--show`, not the hidden prompt: pasting into a hidden prompt on some
Windows consoles injects a `\x16` control character. There's a check for it, but
`--show` avoids the round trip.

**Migrating a Veeam `.cred` file?** It can't be copied — `Export-Clixml` is
DPAPI-bound to its original user and machine. On the box where it works:

```powershell
$cred = Import-Clixml C:\Scripts\veeam-api.cred
$cred.UserName
$cred.GetNetworkCredential().Password
```

Then feed those into `protect` above.

---

## 7. Validate each source by hand

One at a time, before trusting the schedule. Each prints a status line and a
record count; a failure names the call that broke.

```bat
cd /d C:\ManagedClient\backupdashboard\backend
.venv\Scripts\python.exe -m app.cli collect veeam
.venv\Scripts\python.exe -m app.cli collect nable
.venv\Scripts\python.exe -m app.cli collect azure
```

Azure is slow the first time — ARM pages backup jobs roughly one record per
request, so a 96-hour window is thousands of round trips. It logs progress per
vault. If it drags, lower `AZURE_LOOKBACK_HOURS`.

---

## 8. Backfill the existing history

Once at least one collector works:

```bat
.venv\Scripts\python.exe -m app.cli collect legacy
.venv\Scripts\python.exe -m app.cli refresh
```

The importer converts the stored Eastern local times back to UTC — respecting
whether each row was EST or EDT — and merges with collected rows rather than
duplicating. Safe to re-run.

---

## 9. The morning digest

Optional, and the thing the PowerShell scripts did that a dashboard alone does
not: it reaches you on the day you don't open it. Add to `.env`:

```
SMTP_HOST=smtp.lbfosterco.com
SMTP_TO=tscott@lbfoster.com
SMTP_FROM=backups@lbfoster.com
DASHBOARD_URL=http://AZUSCCM01:8010
```

See what it would say, without mailing anyone:

```bat
.venv\Scripts\python.exe -m app.cli notify --print
```

Then send one for real:

```bat
.venv\Scripts\python.exe -m app.cli notify
```

It goes out after the daily collection finishes — collect, rebuild, then send,
in that order. A digest built while Azure is still paging reports a half-
collected night, and every server whose result hasn't arrived reads as "No
backup": the app crying wolf at 8am, by email.

By default it sends **every** morning, clean or not. That is deliberate — an
email that only arrives when something is wrong can't be told apart from a mail
system that has quietly died. `NOTIFY_WHEN=problems` opts out.

---

## 10. Run it

```bat
cd /d C:\ManagedClient\backupdashboard
update-and-run.cmd
```

<http://localhost:8010>. From now on that one command pulls, rebuilds and
launches. Every configured source runs once a morning at `COLLECT_TIME` — 08:00
in `DISPLAY_TIMEZONE` by default, so it stays 8am through both DST changes — and
**Run now** on the Collectors page kicks any of them off by hand. The rollups
refresh hourly regardless, which is what turns a night with no backup into a
visible "No backup" as the report date rolls over; that happens at different
wall-clock moments for the UK and US servers, so it cannot be a daily job.

The **Collectors** page has a live log — use it to watch the first real runs.

To run as a service instead, see [deployment-windows.md](deployment-windows.md).
Remember to run `grant-access.sql` again for the service account.

---

## Troubleshooting

Everything below has actually happened.

**`Login failed for user 'DOMAIN\you'` + `Cannot open database ... (4060)`**
The login reached the server but has no user inside that database. Run
`grant-access.sql` for *that* account. The `18456` alone is deliberately vague;
the `4060` beside it is the real message. A wrong password gives `18456` with no
`4060`.

**`IM002` / "Data source name not found"**
ODBC Driver 18 isn't installed, or `APP_DB_URL` is still the placeholder.

**`[WinError 10054] An existing connection was forcibly closed` from Veeam**
The far end hung up, and the message says nothing about why. Stop guessing and
measure it:

```bat
.venv\Scripts\python.exe -m app.cli probe veeam
```

That walks the layers in order — a plain TCP connect, then each candidate TLS
handshake, then an unauthenticated REST call — and prints what each one did, so
the output names the layer that is broken:

- **TCP connect failed** — nothing is listening. Check the Veeam RESTful API
  service is running and the firewall allows 9419.
- **every handshake failed** — TLS. If even "OpenSSL defaults" fails, whatever
  is on that port may not be speaking TLS at all.
- **the collector's line failed but another succeeded** — `tls_context()` needs
  to match the line that worked; send the output on.
- **`REST service HTTP 401`** — a pass. The API answered; the problem is
  credentials or permissions, not the connection.

Usually this is TLS. Python 3.11 links OpenSSL 3.x, which offers TLS 1.3 by
default and will not offer TLS 1.0/1.1 at all; an older Windows TLS stack resets
rather than negotiating with a hello it cannot parse. The collector offers a
single protocol, the way `ServicePointManager.SecurityProtocol = Tls12` did for
the PowerShell — `VEEAM_TLS_VERSION`, default `1.2`. The probe tries every
version and prints the setting to use:

```
[FAIL] TLS 1.2                    VEEAM_TLS_VERSION=1.2
[ok  ] TLS 1.0                    VEEAM_TLS_VERSION=1.0
       TLSv1  AES256-SHA

>>> Put this in backend\.env:   VEEAM_TLS_VERSION=1.0
```

**Read the exception type, not the fact of the failure.** It carries the whole
conclusion:

| What the probe prints | What it means |
|---|---|
| `SSLError: ...ALERT_PROTOCOL_VERSION` | the server read the hello and objected — a real TLS mismatch, fixable with `VEEAM_TLS_VERSION` |
| `SSLCertVerificationError` | the handshake worked; only trust failed — `VEEAM_VERIFY_TLS` |
| `SSLError: WRONG_VERSION_NUMBER` | that port is not TLS |
| `ConnectionResetError` **on every attempt** | the server never sent a TLS byte. **Not a TLS problem** |

That last row is the one that wastes days. A server that dislikes a ClientHello
answers with an *alert* — it has to read the hello to object to it. A reset
means nothing was ever negotiated, so no combination of versions or ciphers will
help. Something is accepting the TCP connection and killing it:

1. **A firewall in the path.** Many complete the TCP handshake themselves and
   reset once real data arrives — exactly this shape. A port that **times out**
   rather than being refused is more evidence: a host with nothing listening
   refuses immediately, while silence means the packets are being dropped.
2. The Veeam RESTful API service is not running, and something else in the path
   is completing the connection.
3. The service is running but only accepts certain sources.

The probe prints a scan of Veeam's known ports in this case, and the decisive
test is to **run the same probe from the machine the PowerShell script runs
on**. Working there and not here makes it the network path, not the code — and
the fix is to extend that host's firewall rule to this one.

**Azure runs for many minutes, or the record count runs into the tens of thousands**
Sixty servers should produce a few hundred jobs in a 96-hour window, not 30,000.
See what is actually in the vaults, without storing any of it:

```bat
.venv\Scripts\python.exe -m app.cli probe azure
```

Two things cause it, and the survey tells them apart:

- **`outside window` with a large count** — ARM ignored the `$filter` and served
  the vault's entire retained job history. This endpoint's filter is not
  ordinary OData and gives no sign when it is wrong: `eq` expresses the *range*
  (`startTime eq X and endTime eq Y` means "between X and Y") and the timestamps
  are 12-hour with AM/PM, `2026-08-09 01:30:00 PM`. An ISO-8601 `ge`/`le` filter
  returns HTTP 200 and everything. Fixed since; the window is also enforced
  client-side, and paging now stops once it is past the window.
- **A `Log` row with a huge count.** SQL Server and SAP HANA inside a VM back
  their transaction logs up every 15 minutes, per database, and ARM reports each
  one as `operation: Backup` — no server-side filter separates them from the
  nightly run. One database contributes ~384 of them to a 96-hour window. These
  are skipped unless `AZURE_INCLUDE_LOG_BACKUPS=true`.

If **`by operation`** shows anything other than `Backup`, none of the filter is
being parsed at all.

Some slowness is inherent regardless: ARM pages backupJobs by a per-job cursor,
so a busy vault can return roughly one record per round trip. Watch the
per-vault progress in the live log, and lower `AZURE_LOOKBACK_HOURS` to shorten
the first run.

To clear what an earlier run stored and start over:

```bat
.venv\Scripts\python.exe -m app.cli purge azure
.venv\Scripts\python.exe -m app.cli collect azure
.venv\Scripts\python.exe -m app.cli refresh
```

`purge` also drops servers left with no history, which is what removes the
per-database entries a log-backup run created.

**`Violation of UNIQUE KEY constraint 'uq_event_source_server_start'`**
Fixed — pull and re-run. SQL Server's `DATETIME` keeps only 1/300 of a second
and rounds to it, so a run collected at `20:10:38.239838` was stored as
`...38.240`; the next collection looked for the value it had, found nothing, and
tried to insert the same run again. Runs are now keyed by their start time to
the second, and rows written before that rule are matched and converged as their
servers are re-collected. Nothing needs cleaning up by hand.

**A collector stuck on "running"**
It was in flight when the app restarted, so its outcome was never written.
Startup now settles these as "Interrupted"; it is not evidence of a failed
backup.

**"Rollup failed to resolve import" when building**
`npm install` needs to run before `npm run build` after a pull that adds a
dependency. `update-and-run.cmd` already does both.

**`pip install` says "Access is denied"**
Use `python -m pip install ...`.

**"Python was not found; run without arguments to install from the Microsoft Store"**
The Store alias is shadowing a real install, or there isn't one. See
[prerequisites](#the-python-one-is-worth-reading) — and open a new terminal
afterwards.

**UK servers missing from a night, or on the wrong one**
`tzdata` must be installed — Windows ships no IANA database, and without it
`Europe/London` silently resolves to Eastern. It's in `requirements.txt`. See
[timezones.md](timezones.md).

**Everything is the wrong size**
The whole type scale is one block of token overrides at the top of
`frontend/src/styles.css`. Change the numbers there and rebuild.
