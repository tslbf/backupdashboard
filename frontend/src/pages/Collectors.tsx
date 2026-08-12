import { useEffect, useState } from "react";
import { CollectorStatus, Meta, api } from "../api";
import { Skeleton, StatusChip, ageLabel, fmtDateTime, nfmt, useSidebar } from "../components";

const POLL_MS = 15_000;

export default function Collectors() {
  const [rows, setRows] = useState<CollectorStatus[] | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.collectors().then(setRows).catch((e) => setError(String(e)));

  useEffect(() => {
    load();
    api.meta().then(setMeta).catch(() => {});
    const iv = setInterval(load, POLL_MS);
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
    // The run is queued in the background; give it a moment before re-reading.
    setTimeout(() => {
      load().finally(() => setBusy(null));
    }, 1500);
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
                  <StatusChip
                    state={status}
                    label={row.configured ? undefined : "not configured"}
                  />
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
                    {row.interval_minutes > 0
                      ? `every ${row.interval_minutes} min`
                      : "manual only"}
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
                  <div className="error-box" style={{ fontSize: 13 }}>
                    {row.last_run.message}
                  </div>
                )}

                <div className="drawer-actions">
                  <button
                    className="btn-primary"
                    disabled={!row.configured || busy === row.source}
                    onClick={() => run(row.source)}
                  >
                    {busy === row.source ? "Running…" : "Run now"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="card">
        <h2>How a backup night is decided</h2>
        <p className="note" style={{ lineHeight: 1.6 }}>
          Every run is stored in UTC and filed under the backup night it belongs to, computed in
          the server's <b>own</b> timezone: a night runs from{" "}
          {meta ? `${String(meta.night_cutoff_hour).padStart(2, "0")}:00` : "12:00"} local one day
          to {meta ? `${String(meta.night_cutoff_hour).padStart(2, "0")}:00` : "12:00"} local the
          next. That is why a UK job finishing at 23:00 London — which is {""}
          late afternoon where you are — still appears in the same morning review as your Eastern
          overnight jobs, instead of falling into the previous day and disappearing.
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
