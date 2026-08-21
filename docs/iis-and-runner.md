# Hosting on IIS, with auto-updates from a GitHub runner

This is the production path for the Windows box: IIS on **HTTPS/443** as the
front door, the app hosted by **HttpPlatformHandler**, and a **GitHub Actions
self-hosted runner** that redeploys on every push to `main`.

For the simple "just run it in a console" path, see
[deployment-windows.md](deployment-windows.md). This document is the layer on
top of that for a server that has to answer at 8am on its own.

---

## First, the error you hit

```
.venv\Scripts\python.exe : The module '.venv' could not be loaded.
```

That is not a Python or module problem. **`backend\.venv` does not exist yet.**
When PowerShell can't find `.venv\Scripts\python.exe`, it falls back to trying
to auto-load a *module* named `.venv`, and reports that instead - a confusing
message for a missing file.

The virtual environment is created the first time the app is built. You never
got that far, so nothing under `.venv\` exists. Fix it by doing the build once:

```powershell
cd C:\AIProjects\BackupsDashboard
.\update-and-run.cmd demo
```

That creates `backend\.venv`, installs requirements, builds the UI, and starts
the app against throwaway demo data (your real DB is untouched). Once it prints
`Open http://localhost:8010`, the venv exists and your original commands work:

```powershell
cd C:\AIProjects\BackupsDashboard\backend
.\.venv\Scripts\python.exe -m app.cli protect --show
.\.venv\Scripts\python.exe -m app.cli probe veeam
```

> The leading `.\` matters in PowerShell - it won't run an executable from the
> current directory without it. `.\.venv\Scripts\python.exe`, not
> `.venv\Scripts\python.exe`.

Under IIS you won't type that path by hand again - but `protect` and `probe`
are exactly the by-hand steps you still run once to set up credentials and
confirm the vendor endpoints answer, so it's worth having working.

---

## The architecture

```
                          :443 (TLS)
  browser  ───────────────────────────►  IIS site "BackupDashboard"
                                            │  HttpPlatformHandler
                                            │  launches + owns the process
                                            ▼
                                   .venv\Scripts\python.exe
                                   -m uvicorn app.main:app
                                   --host 127.0.0.1 --port %HTTP_PLATFORM_PORT%
                                            │
                                   one process: /api/* + React bundle
                                   + APScheduler (08:00 collect, hourly rollup)
```

IIS assigns a private loopback port, sets `%HTTP_PLATFORM_PORT%`, launches the
process, and reverse-proxies 443 to it. There is no second web server and no
NSSM service - IIS is the process manager.

**Why the site root points at `backend\`:** `config.py` loads `.env` by a
*relative* path, and the SQLite dev DB is `sqlite:///./...`. Both only resolve
when the process's working directory is `backend\`, and HttpPlatformHandler
runs the child with its CWD set to the site's physical path. So the site
physical path is `...\backend`, and `web.config` lives there. The React bundle
is still found by absolute path, so the SPA is served fine.

**Keeping the 8am scheduler alive:** HttpPlatformHandler ties the Python
process to the IIS worker. A worker with no traffic idles out by default - which
would silently kill the scheduler and skip the morning collect and digest.
`Install-IIS.ps1` prevents that by setting the app pool to `AlwaysRunning`,
`idleTimeout=0`, no periodic recycle, and `preloadEnabled` so IIS starts it at
boot. **Don't undo those** - they're the difference between "answers at 8am" and
"answers the first time someone opens the page after 8am".

---

## Prerequisites on the box

| Need | Notes |
|---|---|
| IIS | With the **HttpPlatformHandler** module. It is a **separate ~1 MB download**, not part of a default IIS install: <https://www.iis.net/downloads/microsoft/httpplatformhandler> |
| Python 3.11+ | From python.org, "Add to PATH" ticked. Turn off the Microsoft Store `python.exe` aliases (Settings > Apps > Advanced app settings > App execution aliases). |
| Node.js 18+ | For the frontend build. |
| ODBC Driver 18 for SQL Server | Only if `APP_DB_URL` points at SQL Server. `IM002` at startup = it's missing. |
| A TLS certificate | In `LocalMachine\My`. The installer can generate a self-signed one if you don't have a real cert yet. |

---

## Stand it up (once)

From an **elevated** PowerShell:

```powershell
cd C:\AIProjects\BackupsDashboard\deploy

# List certs to find the thumbprint you want on 443:
Get-ChildItem Cert:\LocalMachine\My | Format-Table Subject, Thumbprint

.\Install-IIS.ps1 `
    -CertThumbprint AABBCCDDEEFF00112233... `
    -ServiceAccount LBFOSTERCO\svc_backupdash
```

The script verifies IIS + HttpPlatformHandler, builds the venv and UI if they're
missing, creates the app pool (with the keep-alive settings above) and the site,
binds your cert on 443, grants the identity file rights, and starts it. Then:

```
https://<server>/            the dashboard
https://<server>/api/meta    health check (200 = app up and reached its DB)
```

Omit `-CertThumbprint` to have it mint a self-signed cert for the box's name
(fine behind the firewall; re-run with a real `-CertThumbprint` later).

Omit `-ServiceAccount` to run as `ApplicationPoolIdentity` - but then the DPAPI
secrets in `.env` must be created with `protect --machine`, because a
per-user DPAPI token can't be decrypted by a machine account. See below.

### The credential gotcha (this is the one that bites)

DPAPI tokens in `.env` decrypt **only for the account that created them, on that
machine**. The app runs as the **app-pool identity**, not as your login. So
create the tokens as that identity, or with machine scope:

```powershell
cd C:\AIProjects\BackupsDashboard\backend
# as the service account, OR add --machine so any account on this box can read them:
.\.venv\Scripts\python.exe -m app.cli protect --show --machine
```

Paste each token into `backend\.env`, then recycle: `Restart-WebAppPool BackupDashboard`.
If a secret was protected under the wrong account, the app logs that it can't
decrypt it rather than failing silently.

### SQL rights

The connection is made by the app-pool identity, so grant SQL rights to *that*
login (not just yours). Edit the name at the top of
[sql/grant-access.sql](sql/grant-access.sql) and run it. Skipping this is the
`4060` login error.

---

## Auto-update with the GitHub runner

The runner lives on the server and redeploys on every push to `main`. Flow:

```
push to main ──► GitHub queues the "Deploy to IIS" workflow
             ──► self-hosted runner on the box picks it up
             ──► runs deploy\redeploy.ps1:
                   git reset --hard origin/main
                   pip install -r requirements.txt
                   npm install && npm run build
                   app.cli init-db
                   Restart-WebAppPool  +  poll https://localhost/api/meta
             ──► green check in GitHub, or red if the build/health check failed
```

### Install the runner (once)

1. In GitHub: **repo > Settings > Actions > Runners > New self-hosted runner
   (Windows)**. Copy the registration token (the value after `--token`; it
   expires in ~1 hour).

2. Elevated PowerShell on the box:

   ```powershell
   cd C:\AIProjects\BackupsDashboard\deploy
   .\Install-Runner.ps1 -Token <paste-token> -RunAsAccount LBFOSTERCO\svc_ghrunner
   ```

   It downloads the runner, registers it with the **`backupdashboard`** label
   the workflow targets, and installs it as an auto-start service. The runner
   account must be able to run `redeploy.ps1` and recycle the app pool, so make
   it a local admin (or a dedicated admin service account).

3. Tell the workflow where the app is checked out. In GitHub, **Settings >
   Secrets and variables > Actions > Variables**, add a repository variable:

   ```
   DEPLOY_DIR = C:\AIProjects\BackupsDashboard
   ```

   (If you skip this it defaults to `C:\ManagedClient\backupdashboard`, which
   is *not* your path - so set it.)

That's it. Push to `main`, or use **Actions > Deploy to IIS > Run workflow** to
redeploy on demand. A failed build or health check turns the run red and leaves
the previous version serving, because the app pool is only recycled after the
build succeeds.

### Why deploy-in-place, not a fresh checkout

`redeploy.ps1` does `git reset --hard origin/main` in the existing checkout
rather than pulling the code into the runner's own workspace. That keeps `.env`
and the local database exactly where IIS expects them (both are gitignored, so
the hard reset never touches them). It's the same checkout you stood the site up
against.

---

## Everyday operations

```powershell
Restart-WebAppPool BackupDashboard          # restart the app (reloads .env)
Get-Content C:\AIProjects\BackupsDashboard\logs\httpplatform.log -Tail 50 -Wait   # uvicorn stdout

# Redeploy by hand (same as the runner does):
C:\AIProjects\BackupsDashboard\deploy\redeploy.ps1 -RepoRoot C:\AIProjects\BackupsDashboard
```

The live log in the UI's **Collectors** page is a 600-line in-memory tail and
resets when the pool recycles; `logs\httpplatform.log` and the `collector_runs`
table are the durable records.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `The module '.venv' could not be loaded` | The venv doesn't exist yet. Build once (top of this doc). Use the leading `.\`. |
| **502.3 / 500.19** right after install | HttpPlatformHandler not installed, or `web.config` unreadable by the identity. Install the module; check ACLs. |
| **502** and `logs\httpplatform.log` shows `IM002` | ODBC Driver 18 missing, or `APP_DB_URL` still the placeholder. |
| App up but every secret "cannot decrypt" | DPAPI tokens made under the wrong account. Re-run `protect --machine` (or as the pool identity), paste, recycle. |
| SQL `4060` / login failed | The app-pool identity has no rights on the DB. Run `grant-access.sql` for that login. |
| Scheduler never runs at 08:00 | App pool idling out. Confirm `startMode=AlwaysRunning`, `idleTimeout=00:00:00`, `preloadEnabled=true` - `Install-IIS.ps1` sets these. |
| Runner shows "Offline" | The runner service stopped. `Get-Service actions.runner.*`; start it. |
| Deploy runs but nothing changes | `DEPLOY_DIR` variable points somewhere other than the real checkout. |
