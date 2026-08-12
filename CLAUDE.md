# Backup Status Dashboard — working notes

Replaces three scheduled PowerShell scripts (Veeam / N-able Cove / Azure) that
pulled backup results into `BackupReporting.dbo.BackupEvents` and emailed a CSV.
Same APIs, ported to Python collectors, plus the history those scripts never
kept and a UI whose landing page is "what do I have to fix this morning".

- Branch for all work: `claude/backup-status-dashboard-txjg9o`
- Owner works US Eastern; the N-able Cove estate is in the UK
- Runs on Windows Server alongside the asset-health dashboard

## Stack

Deliberately identical to `tslbf/assetdashboard` so the two roll up together.

| Layer | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0, APScheduler |
| DB | SQLite for dev (`sqlite:///./backupdashboard.db`), SQL Server via pyodbc in prod |
| Frontend | React 18 + TypeScript + Vite, plain CSS (no component library), react-router-dom |
| Theme | LB Foster Infrastructure dark ops theme, vendored in `frontend/src/theme/` |
| Secrets | Windows DPAPI (`secrets.py`, shared verbatim with assetdashboard) |

One uvicorn process serves both `/api/*` and the built React bundle (SPA
fallback in `main.py`). `update-and-run.cmd` is the supported launch path;
`npm run dev` on :5173 is only for live UI editing.

## The invariant everything else serves

**A backup event is filed by the night it belongs to, in the server's own
timezone — never by wall-clock time.**

```
report_date = local_date_of(end_time_in_server_tz + (24 - cutoff_hour))
```

Default cutoff 12, so a night runs noon-to-noon locally. A UK job finishing
23:00 London (17:00 ET) and a Pittsburgh job finishing 23:00 ET both land on the
same report date.

Break this and the dashboard does not error — it shows a **clean night with the
UK servers silently absent**. That is why `test_timeframes.py` is the largest
test file and why the model is documented at length in `docs/timezones.md`.

Related rules that are easy to undo by accident:

- **`current_report_date` is the viewer's local calendar date**, deliberately
  *not* `report_date(now)`. The latter jumps "last night" forward to an empty
  future night every day at lunchtime. It rolls over at local midnight.
- **`report_date` is denormalized onto every event row** for indexed lookups, so
  changing a server's timezone or cutoff must re-stamp its events
  (`ingest.restamp_server`) and rebuild its nights. `update_server` does this
  inside the request; `cli recompute` does it estate-wide.
- **Ordinary overnight jobs don't move when a timezone changes** — 23:00 Eastern
  is 04:00 London and both land on the same night. There's a test asserting the
  0-restamp case precisely because it looks like a bug. Daytime jobs *do* move.
- **`tzdata` is not optional.** Windows ships no IANA database; without it
  `Europe/London` silently resolves to the Eastern fallback.

## Absence is the signal

A failed job is loud. A job that *stopped running* produces nothing, and nothing
is indistinguishable from a healthy server nobody has queried. `rollups.py`
materializes a `ServerDay` row for every (night, server, source) that should
have run, turning silence into a queryable `missed`.

Guardrails on that, all of which exist to stop the dashboard crying wolf:

- A server counts as expected on a night only if it has some event within
  `stale_server_days` (14) **on either side** of it. Looking forward too is what
  stops a backfill from painting a server's pre-import weeks red.
- `expected=False` and `hidden=True` both remove a server from missed-detection.
- `NON_EXPECTING_SOURCES = {legacy}` — the historical import stopped being
  written the day the collectors took over; every night after would be a miss.
- Retention prunes both events and server-days together.

## Layout

```
backend/app/
  timeframes.py   report-day math + tz resolution — read this first
  outcomes.py     three vendor vocabularies -> one set of canonical outcomes
  ingest.py       server identity, tz resolution, event upsert, restamp
  rollups.py      ServerDay materialization incl. missed detection
  api.py          every REST endpoint
  collectors/     one module per source; each fails independently
  demo_data.py    ~49 servers x 75 nights, shaped to exercise the edge cases
frontend/src/
  api.ts          typed client
  components.tsx  status system, charts (DayColumns, DurationChart, HeatStrip), sort, sidebar
  styles.css      assetdashboard's tokens verbatim + a backup-outcome section
  pages/          Overview, Servers, ServerDetail, History, Collectors
public/lbf-mark.png  brand mark; sits on a light tile, never recoloured
docs/             timezones.md, deployment-windows.md
```

## Outcome vocabulary

Canonical: `success warning failed missed running unknown`. Severity order
(worst first) lives in `outcomes.SEVERITY` and drives every "roll several jobs up
to one verdict" decision — a server with one good and one failed job surfaces as
**failed**, never hidden behind the success.

`warning` counts as protected (a job that completed with errors still produced
restore points). `missed` is synthetic and only ever comes from `rollups.py`.

Unknown vendor strings fall through substring rules that check "warn" before
"success", so a new `CompletedWithSomethingNew` never reads as clean by accident.

## Frontend rules

The UI is built on the **LB Foster Infrastructure dark ops theme**
(`lbfoster-ops-design` skill), vendored under `frontend/src/theme/` and imported
unchanged; `styles.css` is only the component layer on top. Do not edit the
vendored token files — re-sync them from the skill instead.

The theme's own five rules, and how this app honours them:

- **Crimson `#c22832` is identity, never data.** It appears on the logo tile, the
  active nav underline, focus rings and primary-button borders — nowhere else.
  No chart mark, row tint or status chip is ever crimson. Row hovers use
  `--tint-hover`. The duration lines are neutral ink (`--color-text` /
  `--color-neutral-500`) for the same reason: a highlighted line is not a status.
- **The ground is gray; only state-reporting things are saturated.** Surfaces
  step in lightness, never chroma.
- **Status is never colour alone** — every chip and cell carries a glyph and a
  word.
- **Elevation is `--edge` plus ambient darkness**, never a stacked drop shadow.
- **Motion is one clock** (`useIntro`), so two numbers can't disagree mid-animation.

Contrast floors from the theme that are easy to undo: accent *text* must use
`--color-accent-300` (the base crimson is 2.86:1); accent *borders* use
`--color-accent-400`; small uppercase labels never go below
`--color-neutral-600`. `--color-idle` itself is 2.41:1 on the surface, so the
readable idle step `--color-idle-300` is what `--o-other` maps to.

Layout: page root is `width:100%; min-width: var(--page-min-width)`. Never leave
a flexible grid track unfloored — a bare `1fr` absorbs the whole width deficit
when its siblings are fixed, so every track is `minmax(0, Nfr)`.

Backup-specific decisions on top of the theme:

- Charts carry **five** classes, not six: `running` and `unknown` fold into one
  neutral "other" band (`chartCounts`). Both want to be neutral, and no palette
  separates a neutral from itself. They stay fully distinct in chips and tables,
  where a written label carries the meaning.
- **`missed` is the lighter step of the crit ramp (`#ffa79c`), not a hue of its
  own.** The theme offers three chromatic status hues; a fifth invented hue would
  either collide with warn/busy or drift off-brand, and crimson is off-limits.
  Light rose next to `--color-crit` reads as "the other kind of bad", which is
  exactly what "expected, never ran" is. Validated against the surface: worst
  adjacent pair ΔE 10.2 deutan, 15.8 normal vision, all five ≥ 3:1.
- No dual-axis charts. The duration card is two **small multiples** because the
  longest job and the median differ by an order of magnitude; sharing an axis
  flattens the median into the baseline and a second axis would invent a
  correlation.
- Thin marks: bars cap at 16px, heat cells at 16px. 2px gaps between stacked
  segments are surface showing through, not borders. Gridlines are solid
  hairlines, never dashed.
- Every chart has a table-view twin (History has an explicit toggle; the
  landing page's failure list *is* the table).
- The hero figure uses proportional figures, not `tabular-nums` — equal-width
  digits look loose at display sizes.
- Times that could be misread across regions render as `11:01 PM → 04:01 London`
  (`.tz-pill.alt`). The arrow is what stops it reading as two separate times.
- The brand mark sits on a light `#f2f3f4` tile: its black and dark-red segments
  disappear on the dark ground, and it must not be recoloured.

## Vendor gotchas

- **Veeam**: one result per *session*, and a session can hold several VMs, so
  every machine inherits the session verdict. Carried over from the PowerShell
  it replaces; per-object results need task-session endpoints this API version
  doesn't expose. Server names come from paging the session's log and regexing
  the messages — same patterns the script used. Self-signed cert by default
  (`VEEAM_VERIFY_TLS=false`).
- **N-able Cove**: `EnumerateAccountStatistics` returns *current state*, not
  history — the collector only ever sees the latest run per device, so Cove
  history accumulates from first poll. Two response shapes exist (objects with
  `.Settings`, or rows of `"CODE=value"` strings); both are handled. Column
  codes `I18 D9F18 D9F17 D9F12` with `D1F*` as the Files-and-Folders fallback.
  Partner id is resolved from the name at runtime — a stale configured id
  silently returns the wrong estate.
- **Azure**: ARM sometimes returns 7-digit fractional seconds, which
  `fromisoformat` rejects; `parse_dt` truncates to 6. A vault the principal can
  list but not read jobs on is logged and skipped, not fatal.
- **Legacy import**: those scripts wrote **Eastern local time**, not UTC, so the
  importer localizes each row individually (the offset depends on whether that
  timestamp was EST or EDT). The fall-back hour is genuinely ambiguous; `fold=0`
  makes the choice explicit. Source strings map onto this app's vocabulary so
  imported rows merge with collected ones instead of duplicating.

## State as of 2026-08-12

Working: all three collectors, legacy backfill, report-day model, missed
detection, per-server timezone overrides with automatic restamping, the four
pages, demo data, 89 passing tests, `update-and-run.cmd`.

### Not done / open questions

- **No app-level auth.** Firewall-only, same as assetdashboard.
- **Not run against the real APIs yet** — the collectors are ports of working
  PowerShell, validated by unit tests and demo data, but every vendor call is
  still unproven against production. Run each `collect` by hand first.
- Bytes-transferred is modelled and stored but nothing surfaces it; a
  "backup size trend" chart is the obvious next thing.
- No alerting. `cli report` exits non-zero when there are problems, which is
  enough for a scheduled task to email on, but there's no in-app notification.
- Per-source retention, and a way to bulk-edit timezones from the Servers grid,
  were both considered and not built.
