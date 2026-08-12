import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Meta, ServerDetail as ServerDetailData, api } from "../api";
import {
  Check,
  Dropdown,
  HeatStrip,
  Legend,
  Skeleton,
  StatusChip,
  ageLabel,
  chartCounts,
  chartLegend,
  fmtDateTime,
  fmtDay,
  fmtTime,
  nfmt,
  useSidebar,
} from "../components";

export default function ServerDetail() {
  const { id } = useParams();
  const [data, setData] = useState<ServerDetailData | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [zones, setZones] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const load = () => {
    if (!id) return;
    api
      .server(id)
      .then(setData)
      .catch((e) => setError(String(e)));
  };

  useEffect(load, [id]);
  useEffect(() => {
    api.meta().then(setMeta).catch(() => {});
    api.timezones().then(setZones).catch(() => {});
  }, []);

  const legendTotals = useMemo(() => {
    const totals = chartCounts({});
    for (const cell of data?.timeline ?? []) {
      if (!cell.outcome) continue;
      const mapped = chartCounts({ [cell.outcome]: 1 });
      totals.success += mapped.success;
      totals.warning += mapped.warning;
      totals.failed += mapped.failed;
      totals.missed += mapped.missed;
      totals.other += mapped.other;
    }
    return totals;
  }, [data]);

  useSidebar(
    data
      ? {
          label: data.name,
          rows: [
            { name: "Success", value: nfmt(data.counts.success), state: "success" },
            { name: "Warning", value: nfmt(data.counts.warning), state: "warning" },
            { name: "Failed", value: nfmt(data.counts.failed), state: "failed" },
            { name: "No backup", value: nfmt(data.counts.missed), state: "missed" },
          ],
        }
      : null
  );

  const patch = async (body: Parameters<typeof api.patchServer>[1], note: string) => {
    if (!data) return;
    setSaving(true);
    setSaved(null);
    try {
      await api.patchServer(data.id, body);
      load();
      setSaved(note);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (error) return <div className="error-box">Could not load this server: {error}</div>;
  if (!data) return <Skeleton height={400} />;

  const differs = meta && data.timezone !== meta.display_timezone;
  const city = data.timezone.split("/")[1]?.replace("_", " ") ?? data.timezone;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">
            <Link to="/servers">Servers</Link> / {data.name}
          </div>
          <h1 className="detail-title">{data.name}</h1>
          <p className="subtitle">
            {data.display_name !== data.name ? `${data.display_name} · ` : ""}
            {data.timezone} ({data.timezone_label}) · backup night starts{" "}
            {String(data.night_cutoff_hour).padStart(2, "0")}:00 local
          </p>
        </div>
        <div className="head-actions">
          {data.streak > 0 && (
            <span className="streak-pill">
              {data.streak} night{data.streak === 1 ? "" : "s"} without a clean backup
            </span>
          )}
        </div>
      </div>

      {differs && (
        <div className="warn-banner">
          This server runs on <b>{data.timezone}</b>, not your {meta!.display_timezone}. Its
          backups are filed by <b>its own night</b>, so a job finishing at 23:00 local appears in
          the same review as your local overnight jobs even though the wall-clock times differ by{" "}
          {data.timezone_label} vs {meta!.display_timezone_label}.
        </div>
      )}

      <div className="split-2">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Last {data.timeline.length} nights</h2>
              <div className="card-hint">Oldest on the left. Hover a night for its result.</div>
            </div>
            <Legend items={chartLegend(legendTotals)} />
          </div>
          <HeatStrip cells={data.timeline} />
          <div className="tile-row" style={{ marginTop: "var(--s5)" }}>
            <div className="tile">
              <span className="label">Last good backup</span>
              <span className="value" style={{ fontSize: 22 }}>
                {ageLabel(data.last_success_utc)}
              </span>
              <span className="hint">{fmtDateTime(data.last_success_utc)}</span>
            </div>
            <div className="tile">
              <span className="label">Last run</span>
              <span className="value" style={{ fontSize: 22 }}>
                {ageLabel(data.last_event_utc)}
              </span>
              <span className="hint">{fmtDateTime(data.last_event_utc)}</span>
            </div>
            <div className="tile">
              <span className="label">Clean nights</span>
              <span className="value" style={{ fontSize: 22 }}>
                {nfmt(data.counts.success)}
              </span>
              <span className="hint">of {nfmt(data.timeline.filter((t) => t.outcome).length)}</span>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Settings</h2>
            {saving && <span className="meta">saving…</span>}
            {saved && !saving && <span className="meta">{saved}</span>}
          </div>

          <div className="drawer-section">
            <div className="fact-label">Timezone</div>
            <div className="card-hint" style={{ marginBottom: 6 }}>
              Decides which backup night a run belongs to. Inherited from{" "}
              {meta?.sources.find((s) => s.source === data.primary_source)?.display_name ??
                "the app default"}{" "}
              unless set here.
            </div>
            <div className="filters">
              <Dropdown
                field="Zone"
                value={data.timezone_origin === "override" ? data.timezone : ""}
                placeholder={`${data.timezone} (inherited)`}
                onChange={(value) => patch({ timezone: value }, "Timezone updated, history restamped")}
                options={zones.map((z) => ({ value: z, label: z }))}
                wide
              />
              {data.timezone_origin === "override" && (
                <button
                  className="btn-secondary"
                  onClick={() => patch({ clear_timezone: true }, "Back to the source default")}
                >
                  Use source default
                </button>
              )}
            </div>
          </div>

          <div className="drawer-section">
            <div className="fact-label">Night starts at</div>
            <div className="card-hint" style={{ marginBottom: 6 }}>
              The local hour that divides one backup night from the next. Raise it for a server
              that backs up in the afternoon.
            </div>
            <div className="filters">
              <Dropdown
                field="Hour"
                value={data.cutoff_is_override ? String(data.night_cutoff_hour) : ""}
                placeholder={`${String(data.night_cutoff_hour).padStart(2, "0")}:00 (inherited)`}
                onChange={(value) =>
                  patch({ night_cutoff_hour: Number(value) }, "Cutoff updated, history restamped")
                }
                options={Array.from({ length: 24 }, (_, h) => ({
                  value: String(h),
                  label: `${String(h).padStart(2, "0")}:00`,
                }))}
              />
              {data.cutoff_is_override && (
                <button
                  className="btn-secondary"
                  onClick={() => patch({ clear_cutoff: true }, "Back to the app default")}
                >
                  Use default
                </button>
              )}
            </div>
          </div>

          <div className="drawer-section">
            <div className="fact-label">Expectations</div>
            <div className="filters">
              <Check
                on={data.expected}
                label="Expected nightly"
                title="Off: a night with no run stops counting as No backup"
                onChange={(on) =>
                  patch({ expected: on }, on ? "Now expected nightly" : "No longer expected nightly")
                }
              />
              <Check
                on={data.hidden}
                label="Hidden"
                title="Hidden servers leave every view and count"
                onChange={(on) => patch({ hidden: on }, on ? "Hidden" : "Visible again")}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="card flush">
        <div className="card-head" style={{ padding: "var(--s7) var(--s8) var(--s4)" }}>
          <div>
            <h2>Recent runs</h2>
            <div className="card-hint">
              Times in your timezone, with {data.timezone.split("/")[1]?.replace("_", " ")} local
              time alongside.
            </div>
          </div>
        </div>
        {data.events.length === 0 ? (
          <div className="empty">No backup runs recorded for this server yet.</div>
        ) : (
          <div className="table-wrap" style={{ maxHeight: 520 }}>
            <table className="data">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>Status</th>
                  <th style={{ width: 120 }}>Night</th>
                  <th>Source</th>
                  <th>Job</th>
                  <th>Result</th>
                  <th>Started</th>
                  <th>Finished</th>
                  <th className="num">Duration</th>
                </tr>
              </thead>
              <tbody>
                {data.events.map((event) => (
                  <tr key={event.id}>
                    <td>
                      <StatusChip state={event.outcome} />
                    </td>
                    <td>{fmtDay(event.report_date)}</td>
                    <td className="muted">{event.source_name}</td>
                    <td className="muted">{event.job_name ?? "—"}</td>
                    <td className="muted">{event.result_raw ?? "—"}</td>
                    <td className="nowrap">
                      {fmtTime(event.start_utc)}
                      {differs && (
                        <span className="tz-pill alt" style={{ marginLeft: 6 }}>
                          {event.start_local} {city}
                        </span>
                      )}
                    </td>
                    <td className="nowrap">
                      {fmtTime(event.end_utc)}
                      {differs && (
                        <span className="tz-pill alt" style={{ marginLeft: 6 }}>
                          {event.end_local} {city}
                        </span>
                      )}
                    </td>
                    <td className="num muted">{event.duration_label}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
