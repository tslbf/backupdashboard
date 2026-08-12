import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { DayDetail, DurationPoint, Meta, TrendPoint, api } from "../api";
import {
  Check,
  DayColumns,
  DurationChart,
  Dropdown,
  Legend,
  Segmented,
  Skeleton,
  StatusChip,
  chartCounts,
  chartLegend,
  fmtDay,
  fmtDuration,
  fmtTime,
  nfmt,
  toColumnPoint,
  useIntro,
  useSidebar,
} from "../components";

const RANGES = [
  { value: 14, label: "14 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
];

export default function History() {
  const p = useIntro();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [durations, setDurations] = useState<DurationPoint[]>([]);
  const [days, setDays] = useState(30);
  const [source, setSource] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DayDetail | null>(null);
  const [asTable, setAsTable] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.meta().then(setMeta).catch(() => {});
  }, []);

  useEffect(() => {
    api
      .trends(days, source || undefined)
      .then(setTrend)
      .catch((e) => setError(String(e)));
    api.durations(days).then(setDurations).catch(() => {});
  }, [days, source]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    api.day(selected).then(setDetail).catch(() => setDetail(null));
  }, [selected]);

  const columns = useMemo(() => trend.map((t) => toColumnPoint(t.date, t)), [trend]);

  const totals = useMemo(() => {
    const acc = chartCounts({});
    for (const point of columns) {
      acc.success += point.success;
      acc.warning += point.warning;
      acc.failed += point.failed;
      acc.missed += point.missed;
      acc.other += point.other;
    }
    return acc;
  }, [columns]);

  const overall = useMemo(() => {
    const total = totals.success + totals.warning + totals.failed + totals.missed + totals.other;
    return total ? Math.round(((totals.success + totals.warning) / total) * 1000) / 10 : null;
  }, [totals]);

  const worstNights = useMemo(
    () =>
      [...trend]
        .filter((t) => t.failed + t.missed > 0)
        .sort((a, b) => b.failed + b.missed - (a.failed + a.missed))
        .slice(0, 5),
    [trend]
  );

  useSidebar({
    label: `Last ${days} nights`,
    rows: [
      { name: "Success rate", value: overall != null ? `${overall}%` : "—" },
      { name: "Failed", value: nfmt(totals.failed), state: "failed" },
      { name: "No backup", value: nfmt(totals.missed), state: "missed" },
      { name: "Warning", value: nfmt(totals.warning), state: "warning" },
    ],
  });

  if (error) return <div className="error-box">Could not load history: {error}</div>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>History</h1>
          <p className="subtitle">
            Every backup night, counted per server per source. Click a night to see what ran.
          </p>
        </div>
        <div className="head-actions">
          <div className="badge">
            Success rate <b>{overall != null ? `${overall}%` : "—"}</b>
          </div>
        </div>
      </div>

      {/* One filter row above every chart it scopes. */}
      <div className="filters">
        <Segmented value={days} options={RANGES} onChange={setDays} label="Range" />
        <Dropdown
          field="Source"
          value={source}
          onChange={setSource}
          options={[
            { value: "", label: "All sources" },
            ...(meta?.sources ?? []).map((s) => ({ value: s.source, label: s.display_name })),
          ]}
        />
        <Check
          on={asTable}
          label="Table view"
          title="The same numbers, without relying on color"
          onChange={setAsTable}
        />
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h2>Outcomes per night</h2>
            <div className="card-hint">
              A backup night runs noon to noon in each server's own timezone, so UK and US jobs
              share a column.
            </div>
          </div>
          <Legend items={chartLegend(totals)} />
        </div>
        {!trend.length ? (
          <Skeleton height={170} />
        ) : asTable ? (
          <TrendTable trend={trend} onSelect={setSelected} />
        ) : (
          <DayColumns points={columns} progress={p} selected={selected} onSelect={setSelected} />
        )}
      </div>

      <div className="split-2">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>How long the backup window takes</h2>
              <div className="card-hint">
                Same measure, two magnitudes — so they get one honest scale each rather than a
                shared axis that would flatten the median into the baseline.
              </div>
            </div>
          </div>
          {durations.length ? (
            <>
              <div className="kicker">Longest single job</div>
              <DurationChart
                label="longest job"
                color="var(--accent)"
                points={durations.map((d) => ({ date: d.date, value: d.max_sec }))}
                progress={p}
              />
              <div className="kicker" style={{ marginTop: "var(--s4)" }}>
                Median job
              </div>
              <DurationChart
                label="median job"
                color="var(--ok-bar)"
                points={durations.map((d) => ({ date: d.date, value: d.median_sec }))}
                progress={p}
              />
            </>
          ) : (
            <Skeleton height={280} />
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Worst nights</h2>
          </div>
          {worstNights.length === 0 ? (
            <div className="empty-good">
              <span className="mark" aria-hidden>
                ●
              </span>
              No failed or missed backups in this range.
            </div>
          ) : (
            <ul className="chase">
              {worstNights.map((night) => (
                <li key={night.date}>
                  <span className="who">
                    <a
                      href="#day"
                      onClick={(e) => {
                        e.preventDefault();
                        setSelected(night.date);
                      }}
                    >
                      {fmtDay(night.date)}
                    </a>
                    <div className="why">
                      {night.failed} failed · {night.missed} no backup
                    </div>
                  </span>
                  <span className="meta">{night.success_rate}%</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {selected && (
        <div className="card flush" id="day">
          <div className="card-head" style={{ padding: "var(--s7) var(--s8) var(--s4)" }}>
            <div>
              <h2>{fmtDay(selected, { weekday: "long", month: "long", day: "numeric" })}</h2>
              <div className="card-hint">
                {detail
                  ? `${detail.rows.length} server-source results on this night`
                  : "Loading…"}
              </div>
            </div>
            <div className="head-actions">
              <Link className="btn-secondary" to={`/?date=${selected}`}>
                Open as landing page
              </Link>
              <button className="btn-secondary" onClick={() => setSelected(null)}>
                Close
              </button>
            </div>
          </div>
          {!detail ? (
            <div style={{ padding: "var(--s7)" }}>
              <Skeleton height={200} />
            </div>
          ) : (
            <div className="table-wrap" style={{ maxHeight: 460 }}>
              <table className="data">
                <thead>
                  <tr>
                    <th style={{ width: 110 }}>Status</th>
                    <th>Server</th>
                    <th>Source</th>
                    <th>Result</th>
                    <th>Finished</th>
                    <th>Server local</th>
                    <th className="num">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.rows.map((row) => (
                    <tr key={`${row.server_id}-${row.source}`}>
                      <td>
                        <StatusChip state={row.outcome} />
                      </td>
                      <td>
                        <Link className="row-link" to={`/servers/${row.server_id}`}>
                          {row.server}
                        </Link>
                      </td>
                      <td className="muted">{row.source_name}</td>
                      <td className="muted">{row.result_raw ?? "—"}</td>
                      <td className="nowrap">{fmtTime(row.end_utc)}</td>
                      <td className="nowrap muted">
                        {row.end_local ?? "—"}{" "}
                        <span className="tz-pill">{row.timezone.split("/")[1]?.replace("_", " ")}</span>
                      </td>
                      <td className="num muted">{fmtDuration(row.duration_sec)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </>
  );
}

/** The chart's table-view twin: same numbers, no reliance on color. */
function TrendTable({
  trend,
  onSelect,
}: {
  trend: TrendPoint[];
  onSelect: (date: string) => void;
}) {
  return (
    <div className="table-wrap" style={{ maxHeight: 420 }}>
      <table className="data">
        <thead>
          <tr>
            <th>Night</th>
            <th className="num">Success</th>
            <th className="num">Warning</th>
            <th className="num">Failed</th>
            <th className="num">No backup</th>
            <th className="num">Total</th>
            <th className="num">Rate</th>
          </tr>
        </thead>
        <tbody>
          {[...trend].reverse().map((row) => (
            <tr key={row.date} className="clickable" onClick={() => onSelect(row.date)}>
              <td>{fmtDay(row.date)}</td>
              <td className="num">{row.success}</td>
              <td className="num">{row.warning}</td>
              <td className="num">{row.failed}</td>
              <td className="num">{row.missed}</td>
              <td className="num">{row.total}</td>
              <td className="num">{row.success_rate != null ? `${row.success_rate}%` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
