# Rolling up into the asset dashboard

The point of this app matching `tslbf/assetdashboard`'s stack was always that
the two would eventually share a landing page. This is the surface that makes
that possible from the backup side: one endpoint, one small payload, no
knowledge of report dates or timezones required by the caller.

## The endpoint

```
GET /api/summary
```

No auth (same as everything else here — firewall only), no parameters, always
the current backup night.

```json
{
  "version": 1,
  "app": "backup",
  "title": "Backup status",
  "report_date": "2026-08-13",
  "generated_at": "2026-08-13T12:04:11",
  "display_timezone": "America/New_York",
  "headline": "55 of 56 protected",
  "servers_total": 56,
  "servers_protected": 55,
  "protected_pct": 98.2,
  "needs_attention": 1,
  "worst_outcome": "missed",
  "counts": { "success": 55, "warning": 0, "failed": 0, "missed": 1,
              "running": 0, "unknown": 0 },
  "problems": [
    { "server": "NOTCODEBEAMER", "outcome": "missed", "source": "N-able Cove",
      "detail": "no run recorded", "streak": 46 }
  ],
  "collectors": [
    { "source": "veeam", "display_name": "Veeam",
      "configured": true, "status": "error" }
  ]
}
```

### Why it looks like this

- **`headline` is pre-worded.** The consumer is a different codebase; if it
  builds its own sentence from the numbers, the two dashboards will eventually
  phrase the same fact differently and one of them will be wrong.
- **`problems` is capped at five.** A tile has room for a few rows. A caller
  that wants the whole estate should link through rather than pull it over the
  wire on every page load.
- **`collectors[].status` travels with the numbers.** Stale data is worse than
  no data on a tile somebody else owns — the caller has to be able to say "this
  is from a collector that failed last night" rather than showing a confident
  number from three days ago.
- **`worst_outcome` is `unknown`, not `success`, on an empty estate.** Nothing
  collected is not the same as everything succeeded. That distinction is the
  whole premise of this app and it survives into the summary.

### Versioning

`version` is `1`. The contract is **additive only**: new fields may appear at
any time, and a caller must ignore ones it does not know. Renaming or removing a
field breaks a consumer this repo cannot see or test, so it is a version bump
and a note here.

## The asset dashboard side — built

`tslbf/assetdashboard`, branch `claude/backup-status-tile`. One setting:

```
BACKUP_DASHBOARD_URL=http://AZUSCCM01:8010
```

Its Overview then grows a one-line strip above the coverage charts —
*"42 of 49 protected · night of 2026-08-13 · 8 need attention →"* — linking out
to this dashboard. Blank, and the strip does not render at all.

**It reads this endpoint from its own backend, not from the browser.** That was
the important call:

- The app server can reach this host; a viewer's desk may not be able to.
- Neither app needs a `CORS_ORIGINS` entry naming the other.
- The setting lives with the rest of its backend config rather than being baked
  into a static bundle at build time.
- Its proxy caches for five minutes, so a slow or unreachable backup host never
  adds its timeout to an Overview load.

The strip never silently disappears. Unreachable, it says so and shows the error,
because a missing strip and a healthy estate look identical — the same failure
this whole app exists to prevent.

## Reading it from somewhere else

Same-origin is simplest if both apps sit behind one host. A browser calling this
API directly from another origin needs that origin in `CORS_ORIGINS` here:

```
CORS_ORIGINS=http://localhost:5173,http://AZUSCCM01:8020
```

### A drop-in tile

React, no dependencies beyond what the asset dashboard already has:

```tsx
const BACKUP_API = "http://AZUSCCM01:8010";

export function BackupTile() {
  const [data, setData] = useState<any>(null);
  const [down, setDown] = useState(false);

  useEffect(() => {
    const load = () =>
      fetch(`${BACKUP_API}/api/summary`)
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => { setData(d); setDown(false); })
        .catch(() => setDown(true));
    load();
    // The answer changes once a night; a minute is already generous.
    const timer = setInterval(load, 300_000);
    return () => clearInterval(timer);
  }, []);

  // Say so rather than showing nothing: a tile that silently disappears is
  // indistinguishable from an estate with no problems.
  if (down) return <div className="tile">Backup status unavailable</div>;
  if (!data) return <div className="tile">Loading…</div>;

  const stale = data.collectors.some(
    (c: any) => c.configured && c.status && c.status !== "success"
  );

  return (
    <a className="tile" href={`${BACKUP_API}/`}>
      <span className="label">Backups · {data.report_date}</span>
      <span className="value">{data.headline}</span>
      {data.needs_attention > 0 && (
        <span className="bad">{data.needs_attention} need attention</span>
      )}
      {stale && <span className="warn">a collector failed — may be stale</span>}
    </a>
  );
}
```

### Or without React

```html
<div id="backup-tile">Loading…</div>
<script>
  fetch("http://AZUSCCM01:8010/api/summary")
    .then((r) => r.json())
    .then((d) => {
      document.getElementById("backup-tile").textContent =
        d.needs_attention
          ? `${d.headline} — ${d.needs_attention} need attention`
          : d.headline;
    })
    .catch(() => {
      document.getElementById("backup-tile").textContent =
        "Backup status unavailable";
    });
</script>
```

## The other direction

Nothing here consumes the asset dashboard yet. When it grows an equivalent
`/api/summary`, the natural shape is a row of tiles on whichever app becomes the
front door — the two payloads are deliberately the same shape (`app`, `title`,
`headline`, `needs_attention`, `worst_outcome`) so a single tile component can
render either.

Worth saying plainly: **this is two apps that link, not one app.** They run as
separate services on separate ports with separate databases, and they still look
different — this one wears the LB Foster dark ops theme, the asset dashboard
wears its own. Merging them into a single service with shared navigation is a
much larger job and a separate decision; nothing here forecloses it.

## The morning digest

The other way the numbers leave this app. See
[install.md](install.md#9-the-morning-digest) — it goes out after the daily
collection, from the same `overview` data the landing page uses, so the email
and the page cannot disagree.
