import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Meta, Overview as OverviewData, Problem, TrendPoint, api } from "../api";
import {
  DayColumns,
  Legend,
  LiveToggle,
  Segmented,
  Skeleton,
  SortLabel,
  StackBar,
  StatusChip,
  ageLabel,
  chartCounts,
  chartLegend,
  countUp,
  fmtDay,
  fmtTime,
  nfmt,
  toColumnPoint,
  useIntro,
  useSidebar,
  useSort,
} from "../components";

const POLL_MS = 60_000;
const TREND_DAYS = 30;

export default function Overview() {
  const p = useIntro();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [data, setData] = useState<OverviewData | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [date, setDate] = useState<string | null>(null);
  const [live, setLive] = useState(true);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<string>("");

  useEffect(() => {
    api.meta().then(setMeta).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    api
      .overview(date ?? undefined)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, [date, tick]);

  useEffect(() => {
    api.trends(TREND_DAYS).then(setTrend).catch(() => {});
  }, [tick]);

  useEffect(() => {
    if (!live) return;
    const iv = setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => clearInterval(iv);
  }, [live]);

  /* ---------------- derived ---------------- */

  const counts = data?.counts;
  const needsAttention = counts
    ? counts.failed + counts.missed + counts.warning
    : 0;
  const previousAttention = data
    ? data.previous_counts.failed + data.previous_counts.missed + data.previous_counts.warning
    : 0;

  const problems = useMemo(() => {
    const rows = data?.problems ?? [];
    return sourceFilter ? rows.filter((r) => r.source === sourceFilter) : rows;
  }, [data, sourceFilter]);

  const { sorted, sort } = useSort<Problem>(
    problems,
    {
      severity: (r) => ["failed", "missed", "warning"].indexOf(r.outcome),
      server: (r) => r.server,
      source: (r) => r.source_name,
      streak: (r) => -r.streak,
      last: (r) => r.last_success_days ?? 9999,
      when: (r) => r.end_utc ?? "",
      duration: (r) => r.duration_sec ?? -1,
    },
    "severity"
  );

  const columns = useMemo(
    () => trend.map((t) => toColumnPoint(t.date, t)),
    [trend]
  );
  const legendTotals = useMemo(() => {
    const totals = chartCounts({});
    for (const point of columns) {
      totals.success += point.success;
      totals.warning += point.warning;
      totals.failed += point.failed;
      totals.missed += point.missed;
      totals.other += point.other;
    }
    return totals;
  }, [columns]);

  useSidebar(
    data
      ? {
          label: "Last night",
          rows: [
            { name: "Failed", value: nfmt(data.counts.failed), state: "failed" },
            { name: "No backup", value: nfmt(data.counts.missed), state: "missed" },
            { name: "Warning", value: nfmt(data.counts.warning), state: "warning" },
            { name: "Success", value: nfmt(data.counts.success), state: "success" },
          ],
        }
      : null,
    data ? { overview: needsAttention ? String(needsAttention) : "" } : undefined
  );

  /* ---------------- render ---------------- */

  if (error) return <div className="error-box">Could not load the dashboard: {error}</div>;

  if (!data || !meta) {
    return (
      <>
        <div className="page-head">
          <h1>Last night</h1>
        </div>
        <div className="night-band">
          <Skeleton height={200} />
          <Skeleton height={200} />
          <Skeleton height={200} />
          <Skeleton height={200} />
        </div>
        <Skeleton height={280} />
      </>
    );
  }

  const shift = (days: number) => {
    const [y, m, d] = data.report_date.split("-").map(Number);
    const next = new Date(y, m - 1, d + days, 12);
    const stamp = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}-${String(
      next.getDate()
    ).padStart(2, "0")}`;
    setDate(stamp);
  };

  const allClear = needsAttention === 0;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">
            Backup night · {meta.display_timezone_label} · runs from noon
            {" "}{fmtDay(data.previous_date, { month: "short", day: "numeric" })} to noon{" "}
            {fmtDay(data.report_date, { month: "short", day: "numeric" })} in each server's own
            timezone
          </div>
          <h1>
            {data.is_current ? "Last night" : fmtDay(data.report_date, { weekday: "long", month: "long", day: "numeric" })}
          </h1>
          <p className="subtitle">
            {fmtDay(data.report_date, { weekday: "long", month: "long", day: "numeric" })} ·{" "}
            {nfmt(data.servers_total)} servers · {nfmt(data.jobs_total)} jobs
          </p>
        </div>
        <div className="head-actions">
          <button className="btn-secondary" onClick={() => shift(-1)}>
            ← Previous
          </button>
          <button
            className="btn-secondary"
            onClick={() => shift(1)}
            disabled={data.is_current}
            title={data.is_current ? "This is the most recent night" : undefined}
          >
            Next →
          </button>
          {!data.is_current && (
            <button className="btn-primary" onClick={() => setDate(null)}>
              Back to last night
            </button>
          )}
          <span className="head-divider" />
          <LiveToggle on={live} onToggle={() => setLive((v) => !v)} />
        </div>
      </div>

      {/* ---------- what happened last night ----------
          The hero is the count that is *working*, not the count that is broken.
          A good night is four zeros, and a band of four zeros reads as an app
          that failed to load rather than an estate that is fine — which is the
          opposite of what the page is for. The exceptions keep their own tiles
          beside it, and "needs attention" is called out in the hero itself so
          the one number you act on is never buried. */}
      <div className="night-band">
        <div className="card">
          <div className="kicker">Protected</div>
          <div className={`hero-figure ${allClear ? "good" : ""}`}>
            {countUp(data.servers_protected, p)}
            <span className="hero-of">/ {nfmt(data.servers_total)}</span>
          </div>
          <div className="hero-sub">
            servers have a restore point from this night
            {data.protected_pct != null ? ` · ${data.protected_pct}%` : ""}
          </div>
          <div style={{ marginTop: "auto" }}>
            <StackBar counts={data.counts} progress={p} height={10} />
            <div className={`hero-verdict ${allClear ? "good" : "bad"}`}>
              <span className="mark" aria-hidden>
                {allClear ? "●" : "▲"}
              </span>
              {allClear
                ? "Every expected backup completed"
                : `${nfmt(needsAttention)} ${needsAttention === 1 ? "server needs" : "servers need"} attention`}
            </div>
            <Delta current={needsAttention} previous={previousAttention} />
          </div>
        </div>

        <StatTile
          label="Failed"
          state="failed"
          value={data.counts.failed}
          previous={data.previous_counts.failed}
          progress={p}
          hint="Job ran and errored"
        />
        <StatTile
          label="No backup"
          state="missed"
          value={data.counts.missed}
          previous={data.previous_counts.missed}
          progress={p}
          hint="Expected, never ran"
        />
        <StatTile
          label="Warning"
          state="warning"
          value={data.counts.warning}
          previous={data.previous_counts.warning}
          progress={p}
          hint="Completed with errors"
        />
      </div>

      {/* ---------- the list you actually work from ---------- */}
      <div className="split-2">
        <div className="card flush">
          <div className="card-head" style={{ padding: "var(--space-7) var(--space-8) 0" }}>
            <div>
              <h2>Servers needing attention</h2>
              <div className="card-hint">
                Sorted worst first. Times show each server's own clock where it differs from yours.
              </div>
            </div>
            <div className="head-actions">
              <Segmented
                label="Filter by source"
                value={sourceFilter}
                onChange={setSourceFilter}
                options={[
                  { value: "", label: "All" },
                  ...data.per_source.map((s) => ({ value: s.source, label: s.display_name })),
                ]}
              />
            </div>
          </div>

          {sorted.length === 0 ? (
            <div className="empty-good">
              <span className="mark" aria-hidden>
                ●
              </span>
              {problems.length === 0 && data.problems.length > 0
                ? "Nothing to chase for this source."
                : "Every expected backup completed."}
            </div>
          ) : (
            <div className="table-wrap" style={{ marginTop: "var(--space-5)" }}>
              <table className="data">
                <thead>
                  <tr>
                    <th style={{ width: 110 }}>
                      <SortLabel id="severity" sort={sort}>
                        Status
                      </SortLabel>
                    </th>
                    <th>
                      <SortLabel id="server" sort={sort}>
                        Server
                      </SortLabel>
                    </th>
                    <th style={{ width: 120 }}>
                      <SortLabel id="source" sort={sort}>
                        Source
                      </SortLabel>
                    </th>
                    <th>
                      <SortLabel id="when" sort={sort}>
                        Finished
                      </SortLabel>
                    </th>
                    <th className="num">
                      <SortLabel id="duration" sort={sort} right>
                        Duration
                      </SortLabel>
                    </th>
                    <th className="num">
                      <SortLabel id="last" sort={sort} right>
                        Last good
                      </SortLabel>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((row) => (
                    <tr key={`${row.server_id}-${row.source}`}>
                      <td>
                        <StatusChip state={row.outcome} />
                      </td>
                      <td>
                        <Link className="row-link" to={`/servers/${row.server_id}`}>
                          {row.server}
                        </Link>
                        {row.streak > 1 && (
                          <span className="streak-pill" style={{ marginLeft: 8 }}>
                            {row.streak} nights
                          </span>
                        )}
                        {/* The vendor's own words, kept close to the server they
                            describe rather than costing a whole column. */}
                        <div className="cell-sub">{row.result_raw ?? "no run recorded"}</div>
                      </td>
                      <td className="muted nowrap">{row.source_name}</td>
                      <td>
                        <WhenCell row={row} displayTz={meta.display_timezone} />
                      </td>
                      <td className="num muted">{row.duration_label}</td>
                      <td className="num">
                        {row.last_success_utc ? (
                          <span className={row.last_success_days! > 2 ? "" : "muted"}>
                            {ageLabel(row.last_success_utc)}
                          </span>
                        ) : (
                          <span style={{ color: "var(--o-failed-fg)" }}>never</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="mini-grid" style={{ gap: "var(--space-7)" }}>
          <div className="card">
            <div className="card-head">
              <h2>By source</h2>
            </div>
            {data.per_source.map((source) => (
              <div key={source.source} style={{ marginBottom: "var(--space-4)" }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: 5,
                  }}
                >
                  <span>{source.display_name}</span>
                  <span className="tz-note">
                    <span className="tz-pill">{source.timezone_label}</span>
                    <span className="num">{nfmt(source.total)}</span>
                  </span>
                </div>
                <StackBar counts={source} progress={p} height={8} />
              </div>
            ))}
            <Legend items={chartLegend(legendTotals)} />
          </div>

          {data.attention.never_succeeded.length > 0 && (
            <AttentionCard
              title="Never had a good backup"
              hint="Seen by a backup tool, but no run has ever succeeded."
              rows={data.attention.never_succeeded.map((r) => ({
                id: r.server_id,
                name: r.server,
                note: r.last_event_utc ? `last try ${ageLabel(r.last_event_utc)}` : "—",
              }))}
            />
          )}

          {data.attention.stale_success.length > 0 && (
            <AttentionCard
              title="No recent restore point"
              hint="Last successful backup is more than three days old."
              rows={data.attention.stale_success.map((r) => ({
                id: r.server_id,
                name: r.server,
                note: `${r.days}d ago`,
              }))}
            />
          )}
        </div>
      </div>

      {/* ---------- history ---------- */}
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Last {TREND_DAYS} nights</h2>
            <div className="card-hint">
              Click a night to open it. Each bar is one backup night, counted per server per source.
            </div>
          </div>
          <Legend items={chartLegend(legendTotals)} />
        </div>
        {columns.length ? (
          <DayColumns
            points={columns}
            progress={p}
            selected={data.report_date}
            onSelect={(d) => setDate(d)}
          />
        ) : (
          <Skeleton height={170} />
        )}
        <div className="meta">
          Full numbers, and a table view of this chart, are on the{" "}
          <Link to="/history">History</Link> page.
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */

function StatTile({
  label,
  state,
  value,
  previous,
  progress,
  hint,
}: {
  label: string;
  state: string;
  value: number;
  previous: number;
  progress: number;
  hint: string;
}) {
  return (
    <div className="card">
      <div className="card-head">
        <div className="kicker">{label}</div>
        <span className={`sq sw-${state}`} aria-hidden />
      </div>
      <div className="stat-num">{countUp(value, progress)}</div>
      <Delta current={value} previous={previous} />
      <div className="note" style={{ marginTop: "auto" }}>
        {hint}
      </div>
    </div>
  );
}

/** Change against the night before — the number that says "is this new?". */
function Delta({ current, previous }: { current: number; previous: number }) {
  const diff = current - previous;
  const cls = diff > 0 ? "up" : diff < 0 ? "down" : "flat";
  const text =
    diff === 0 ? "same as the night before" : `${diff > 0 ? "+" : ""}${diff} vs the night before`;
  return <div className={`delta ${cls}`}>{text}</div>;
}

/**
 * A finish time in two clocks. The viewer's own time is primary; the server's
 * local time appears alongside whenever it differs, because "17:42" on a UK
 * server is the middle of the Eastern afternoon and looks wrong until you can
 * see it was 22:42 where the server lives.
 */
function WhenCell({ row, displayTz }: { row: Problem; displayTz: string }) {
  if (!row.end_utc) return <span className="muted">—</span>;
  const differs = row.timezone !== displayTz;
  return (
    <span className="nowrap">
      {fmtTime(row.end_utc)}
      {differs && (
        <span className="tz-pill alt" style={{ marginLeft: 6 }}>
          {row.end_local} {row.timezone.split("/")[1]?.replace("_", " ")}
        </span>
      )}
    </span>
  );
}

function AttentionCard({
  title,
  hint,
  rows,
}: {
  title: string;
  hint: string;
  rows: { id: number; name: string; note: string }[];
}) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>{title}</h2>
        <span className="badge">
          <b>{rows.length}</b>
        </span>
      </div>
      <div className="card-hint">{hint}</div>
      <ul className="chase">
        {rows.slice(0, 8).map((row) => (
          <li key={row.id}>
            <span className="who">
              <Link to={`/servers/${row.id}`}>{row.name}</Link>
            </span>
            <span className="meta">{row.note}</span>
          </li>
        ))}
      </ul>
      {rows.length > 8 && <div className="meta">+{rows.length - 8} more</div>}
    </div>
  );
}
