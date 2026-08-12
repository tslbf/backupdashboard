import { useEffect, useRef, useState } from "react";
import { CollectorRun, CollectorStatus, LogEntry, Meta, api } from "../api";
import {
  Check,
  Skeleton,
  StatusChip,
  ageLabel,
  fmtDateTime,
  fmtDuration,
  fmtTime,
  nfmt,
  useSidebar,
} from "../components";

const STATUS_POLL_MS = 10_000;
const LOG_POLL_MS = 2_000;
const CLIENT_LOG_CAP = 500;

export default function Collectors() {
  const [rows, setRows] = useState<CollectorStatus[] | null>(null);
  const [runs, setRuns] = useState<CollectorRun[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    Promise.all([api.collectors(), api.collectorRuns(40)])
      .then(([status, history]) => {
        setRows(status);
        setRuns(history);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
    api.meta().then(setMeta).catch(() => {});
    const iv = setInterval(load, STATUS_POLL_MS);
    return () => clearInterval(iv);
  }, []);

  useSidebar(
    rows
      ? {
          label: "Sources",
          rows: rows.map((r) => ({
            name: r.display_name,
            value: r.configured ? (r.last_run?.status ?? "never") : "off",
            state: !r.configured
              ? "none"
              : r.last_run?.status === "success"
                ? "success"
                : r.last_run?.status === "error"
                  ? "failed"
                  : "other",
          })),
        }
      : null
  );

  const run = async (source: string) => {
    setBusy(source);
    await api.runCollector(source);
    // Queued in the background; the log panel shows it start within ~2s.
    setTimeout(() => load().finally(() => setBusy(null)), 1200);
  };

  if (error) return <div className="error-box">Could not load collectors: {error}</div>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Collectors</h1>
          <p className="subtitle">
            Where the numbers come from, and whether they are current. A source that has not run
            recently makes every count on the other pages stale.
          </p>
        </div>
        <div className="head-actions">
          <button
            className="btn-secondary"
            onClick={() => api.refresh().then(() => setTimeout(load, 1500))}
            title="Recompute every night's rollup from the stored events"
          >
            Rebuild history
          </button>
        </div>
      </div>

      {!rows ? (
        <Skeleton height={280} />
      ) : (
        <div className="tile-row wide">
          {rows.map((row) => {
            const status = !row.configured
              ? "none"
              : row.last_run?.status === "success"
                ? "success"
                : row.last_run?.status === "error"
                  ? "failed"
                  : row.last_run?.status === "running"
                    ? "running"
                    : "unknown";
            return (
              <div className="card" key={row.source}>
                <div className="card-head">
                  <h2>{row.display_name}</h2>
                  <StatusChip state={status} label={row.configured ? undefined : "not configured"} />
                </div>

                <dl className="kv">
                  <dt>Last run</dt>
                  <dd>
                    {row.last_run?.started_at ? (
                      <span title={fmtDateTime(row.last_run.started_at)}>
                        {ageLabel(row.last_run.started_at)}
                      </span>
                    ) : (
                      <span className="muted">never</span>
                    )}
                  </dd>
                  <dt>Records</dt>
                  <dd>{row.last_run ? nfmt(row.last_run.records) : "—"}</dd>
                  <dt>Schedule</dt>
                  <dd>
                    {row.interval_minutes > 0 ? `every ${row.interval_minutes} min` : "manual only"}
                  </dd>
                  <dt>Timezone</dt>
                  <dd className="ident">
                    {row.default_timezone ?? "—"}
                    {meta && row.default_timezone !== meta.display_timezone && (
                      <div className="tz-pill" style={{ marginTop: 4 }}>
                        not your local time
                      </div>
                    )}
                  </dd>
                </dl>

                {row.last_run?.message && (
                  <div className="error-box" style={{ fontSize: "var(--text-meta)" }}>
                    {row.last_run.message}
                  </div>
                )}

                <div className="drawer-actions">
                  <button
                    className="btn-primary"
                    disabled={!row.configured || busy === row.source}
                    onClick={() => run(row.source)}
                  >
                    {busy === row.source ? "Starting…" : "Run now"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <LiveLog />

      <div className="card flush">
        <div className="card-head" style={{ padding: "var(--space-7) var(--space-8) var(--space-4)" }}>
          <div>
            <h2>Run history</h2>
            <div className="card-hint">
              The durable record. A run marked <b>interrupted</b> was in flight when the app
              restarted — its outcome was never written, so it is not evidence of a failed backup.
            </div>
          </div>
        </div>
        {runs.length === 0 ? (
          <div className="empty">No collector has run yet. Use “Run now” on a configured source.</div>
        ) : (
          <div className="table-wrap" style={{ maxHeight: 420 }}>
            <table className="data">
              <thead>
                <tr>
                  <th style={{ width: 120 }}>Result</th>
                  <th style={{ width: 150 }}>Source</th>
                  <th style={{ width: 170 }}>Started</th>
                  <th className="num" style={{ width: 110 }}>Took</th>
                  <th className="num" style={{ width: 110 }}>Records</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <StatusChip
                        state={
                          r.status === "success"
                            ? "success"
                            : r.status === "error"
                              ? "failed"
                              : "running"
                        }
                        label={r.status === "error" ? "error" : r.status}
                      />
                    </td>
                    <td>{r.display_name}</td>
                    <td className="nowrap" title={fmtDateTime(r.started_at)}>
                      {fmtTime(r.started_at)} <span className="muted">· {ageLabel(r.started_at)}</span>
                    </td>
                    <td className="num muted">
                      {r.status === "running" ? "running…" : fmtDuration(r.duration_sec)}
                    </td>
                    <td className="num">{nfmt(r.records)}</td>
                    <td className="muted">{r.message ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h2>How a backup night is decided</h2>
        <p className="note" style={{ lineHeight: 1.6 }}>
          Every run is stored in UTC and filed under the backup night it belongs to, computed in the
          server's <b>own</b> timezone: a night runs from{" "}
          {meta ? `${String(meta.night_cutoff_hour).padStart(2, "0")}:00` : "12:00"} local one day to{" "}
          {meta ? `${String(meta.night_cutoff_hour).padStart(2, "0")}:00` : "12:00"} local the next.
          That is why a UK job finishing at 23:00 London — which is late afternoon where you are —
          still appears in the same morning review as your Eastern overnight jobs, instead of
          falling into the previous day and disappearing.
        </p>
        <p className="note">
          A server inherits its timezone from the source that first reported it (
          {(meta?.sources ?? [])
            .filter((s) => s.expected)
            .map((s) => `${s.display_name} → ${s.default_timezone}`)
            .join(", ")}
          ). Override it per server from that server's page.
        </p>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */

/**
 * A tail of the app's own log.
 *
 * The status cards can say a collector is running; only this can show it doing
 * something. Polls incrementally — each request asks for lines after the last id
 * it has, so a quiet minute costs an empty array rather than the whole buffer.
 */
function LiveLog() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [live, setLive] = useState(true);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [failed, setFailed] = useState(false);
  const after = useRef(0);
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    if (!live) return;
    let cancelled = false;

    const tick = () =>
      api
        .logs(after.current)
        .then((resp) => {
          if (cancelled) return;
          setFailed(false);
          // The server restarted and its buffer reset: start over rather than
          // waiting forever for ids that will never come.
          if (resp.buffer_end < after.current) after.current = 0;
          if (resp.entries.length) {
            after.current = resp.last_id;
            setEntries((current) => [...current, ...resp.entries].slice(-CLIENT_LOG_CAP));
          }
        })
        .catch(() => !cancelled && setFailed(true));

    tick();
    const iv = setInterval(tick, LOG_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [live]);

  // Follow the tail, but not if the reader has scrolled up to look at something.
  useEffect(() => {
    const node = box.current;
    if (node && pinned.current) node.scrollTop = node.scrollHeight;
  }, [entries]);

  const onScroll = () => {
    const node = box.current;
    if (!node) return;
    pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
  };

  const shown = errorsOnly
    ? entries.filter((e) => e.level === "ERROR" || e.level === "WARNING")
    : entries;

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>Live activity</h2>
          <div className="card-hint">
            The app's own log, newest at the bottom. This is what tells you a collector is actually
            working rather than just marked as running.
          </div>
        </div>
        <div className="head-actions">
          <Check on={errorsOnly} label="Problems only" onChange={setErrorsOnly} />
          <button
            className={`live${live ? " on" : ""}`}
            onClick={() => setLive((v) => !v)}
            title={live ? "Streaming" : "Paused — nothing is being fetched"}
          >
            <span className="dot" aria-hidden />
            {live ? "Live" : "Paused"}
          </button>
        </div>
      </div>

      {failed && (
        <div className="error-box" style={{ fontSize: "var(--text-meta)" }}>
          Lost contact with the server — it may have stopped. Retrying every {LOG_POLL_MS / 1000}s.
        </div>
      )}

      <div className="logbox" ref={box} onScroll={onScroll} role="log" aria-live="polite">
        {shown.length === 0 ? (
          <div className="log-empty">
            {entries.length === 0
              ? "Nothing logged yet. Trigger a collector with “Run now” and watch here."
              : "No warnings or errors in the current buffer."}
          </div>
        ) : (
          shown.map((e) => (
            <div className={`log-line lvl-${e.level.toLowerCase()}`} key={e.id}>
              <span className="log-ts">{fmtTime(e.ts)}</span>
              <span className="log-level">{e.level}</span>
              <span className="log-src">{e.logger.replace(/^app\./, "")}</span>
              <span className="log-msg">{e.message}</span>
            </div>
          ))
        )}
      </div>
      <div className="meta">
        Showing the last {CLIENT_LOG_CAP} lines held in memory. Anything older is in the run history
        above, which is stored in the database.
      </div>
    </div>
  );
}
