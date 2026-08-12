# Time, and why this app has a whole model for it

The estate spans two regions. You read the dashboard in US Eastern; the N-able
Cove devices are in the UK. That single fact decides the shape of the data
model, so it is worth being explicit about.

## The failure mode this prevents

The obvious way to build "last night" is a wall-clock window in your own
timezone — say 18:00 yesterday to 08:00 today, Eastern.

Now take a UK file server that backs up at 22:00 London time. In August that
finishes at **17:00 Eastern** — an hour *before* the window opens. It is not
late, it is not early, it simply falls outside a window built around someone
else's evening. It disappears.

And it disappears **silently**. There is no error, no red row, no count that
looks wrong. The dashboard shows a clean night, and the server that failed to
back up is invisible. That is the worst possible failure for a backup report.

Shifting the window wider does not fix it either: widen it enough to catch UK
evening jobs and you start catching *two* nights of US jobs, double-counting
every Eastern server.

## The model: a report date, computed per server

Every event is stored in UTC and stamped with the **backup night it belongs
to**, computed in the *server's own* timezone:

```
report_date = local_date_of(end_time_in_server_tz + (24 - cutoff_hour))
```

With the default cutoff of 12:00, a backup night runs **noon to noon, local**.
Everything a server finishes between noon on day D-1 and noon on day D belongs
to report date D — "the night leading into the morning of D", wherever it lives.

Five jobs, one night:

| Job | Finished (local) | Finished (UTC) | Finished (your ET) | Report date |
|---|---|---|---|---|
| Veeam PGHSQL01 | 23:00 Tue, New York | 03:00 Wed | 23:00 Tue | **Wed** |
| Veeam PGHAPP01 | 02:30 Wed, New York | 06:30 Wed | 02:30 Wed | **Wed** |
| Cove LONFILE01 | 23:00 Tue, London | 22:00 Tue | **18:00 Tue** | **Wed** |
| Cove NOTDC01 | 02:00 Wed, London | 01:00 Wed | 21:00 Tue | **Wed** |
| Azure AZUSSQL01 | 03:00 Wed, New York | 07:00 Wed | 03:00 Wed | **Wed** |

All five land on Wednesday. Wednesday morning you open the dashboard and see one
coherent review, with the UK servers in it — even though one of them finished
during your Tuesday afternoon.

## What "last night" means at 4pm

`current_report_date` is deliberately **your local calendar date**, not
`report_date(now)`.

At 09:00 Wednesday, both give Wednesday. At 16:00 Wednesday they diverge:
`report_date(now)` would say Thursday, because 16:00 is past the noon cutoff and
Thursday's window has technically opened. But almost nothing has run in it, and
no human at 4pm on Wednesday means Thursday by "last night".

So the landing page stays on Wednesday until local midnight. Use the
**Previous / Next** buttons, or the History page, to look at any other night.

## Daylight saving

Nothing is hardcoded, because nothing can be:

- The US and UK change clocks on **different dates**. Between the second Sunday
  of March and the last Sunday of March, the transatlantic gap is **4 hours**,
  not the usual 5. A constant offset silently misfiles every UK backup for
  those two weeks.
- Transition nights are 23 or 25 hours long, and they fall on different dates
  per region.
- Offsets are resolved per timestamp through the IANA database
  (`zoneinfo` + the `tzdata` package, since Windows ships no IANA data).

`backend/tests/test_timeframes.py` pins all of this down, including the March
mismatch window and both transition nights.

## Which timezone a server gets

Resolution order, first hit wins:

1. **Per-server override** — set on the server's page in the UI. Always wins.
2. **The primary source's default** — the source that *first* reported the
   server, fixed at creation so a machine that later shows up in a second tool
   doesn't silently change zone. Seeded as:
   - Veeam → `America/New_York` (PGHVEEAM, and DUBVEEAM = Dublin, **Ohio**)
   - N-able Cove → `Europe/London`
   - Azure Backup → `America/New_York`
3. **`DISPLAY_TIMEZONE`** from `.env`.

A UK machine protected by Veeam, or a US machine on the Cove agent, is an
override — set it on that server's page. The Servers grid has a Timezone filter
and column so you can audit the assignments in one pass.

### Changing a timezone rewrites history

`report_date` is denormalized onto every event row so the hot queries are an
indexed string compare. Changing a server's timezone or cutoff therefore
re-stamps that server's events and rebuilds its nights automatically — the API
does it inside the same request, and the response reports how many events moved.

After changing a *source-level* default, or importing rows written before a
server's timezone was corrected, run:

```
python -m app.cli recompute
```

Note that ordinary overnight backups usually *don't* move: 23:00 Eastern is
04:00 London, and both readings put the run in the same night. That is a feature
of the noon-to-noon window, and also why the original misfiling is so hard to
spot without this model — the two readings agree right up until they don't.

## The cutoff hour

`NIGHT_CUTOFF_HOUR` (default 12) is really the question "when does this server's
backup day start?". Noon works for anything that runs overnight. A server that
backs up at 14:00 local needs a later cutoff, or its afternoon run gets filed
under tomorrow — set it per server under **Night starts at** on the server page.
