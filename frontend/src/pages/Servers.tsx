import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Meta, ServerRow, ServersResponse, api } from "../api";
import {
  Check,
  Dropdown,
  HeatStrip,
  Legend,
  Segmented,
  Skeleton,
  SortLabel,
  StatusChip,
  ageLabel,
  chartCounts,
  chartLegend,
  fmtDay,
  nfmt,
  useSidebar,
  useSort,
} from "../components";

const RANGES = [
  { value: 14, label: "14 nights" },
  { value: 30, label: "30 nights" },
  { value: 60, label: "60 nights" },
];

export default function Servers() {
  const [data, setData] = useState<ServersResponse | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [days, setDays] = useState(14);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const [zone, setZone] = useState("");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [includeHidden, setIncludeHidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    api.meta().then(setMeta).catch(() => {});
  }, []);

  const reload = () => {
    setData(null);
    api
      .servers(days, includeHidden)
      .then(setData)
      .catch((e) => setError(String(e)));
  };

  useEffect(reload, [days, includeHidden]);

  const rows = useMemo(() => {
    const all = data?.servers ?? [];
    const needle = search.trim().toLowerCase();
    return all.filter((row) => {
      if (needle && !row.name.toLowerCase().includes(needle)) return false;
      if (source && !row.sources.includes(source)) return false;
      if (zone && row.timezone !== zone) return false;
      if (problemsOnly && row.problem_nights === 0) return false;
      return true;
    });
  }, [data, search, source, zone, problemsOnly]);

  const { sorted, sort } = useSort<ServerRow>(
    rows,
    {
      name: (r) => r.name,
      source: (r) => r.sources.join(","),
      timezone: (r) => r.timezone,
      last: (r) => r.last_success_utc ?? "",
      rate: (r) => r.success_rate ?? -1,
      problems: (r) => -r.problem_nights,
      outcome: (r) => ["failed", "missed", "warning", "running", "unknown", "success"].indexOf(
        r.last_outcome ?? "success"
      ),
    },
    "problems"
  );

  // Selection is scoped to what is on screen: a filter narrows the list, so a
  // server the filter removed must not stay silently selected and get hidden
  // by a bulk action aimed at rows you can see.
  const visibleIds = useMemo(() => new Set(rows.map((r) => r.id)), [rows]);
  const selected = useMemo(
    () => [...picked].filter((id) => visibleIds.has(id)),
    [picked, visibleIds]
  );
  const allPicked = rows.length > 0 && selected.length === rows.length;

  const toggle = (id: number) =>
    setPicked((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const toggleAll = () =>
    setPicked(allPicked ? new Set() : new Set(rows.map((r) => r.id)));

  const selectedRows = useMemo(
    () => rows.filter((r) => visibleIds.has(r.id) && picked.has(r.id)),
    [rows, picked, visibleIds]
  );
  const anyVisible = selectedRows.some((r) => !r.hidden);

  const applyHidden = (hidden: boolean) => {
    if (!selected.length) return;
    setBusy(true);
    setNotice(null);
    api
      .bulkServers(selected, { hidden })
      .then((res) => {
        setNotice(
          `${res.updated} server${res.updated === 1 ? "" : "s"} ${hidden ? "hidden" : "restored"}.`
        );
        setPicked(new Set());
        reload();
      })
      .catch((e) => setNotice(`Could not update: ${e}`))
      .finally(() => setBusy(false));
  };

  const zones = useMemo(() => {
    const set = new Map<string, number>();
    for (const row of data?.servers ?? []) set.set(row.timezone, (set.get(row.timezone) ?? 0) + 1);
    return [...set.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [data]);

  const legendTotals = useMemo(() => {
    const totals = chartCounts({});
    for (const row of data?.servers ?? []) {
      for (const cell of row.days) {
        if (!cell.outcome) continue;
        const mapped = chartCounts({ [cell.outcome]: 1 });
        totals.success += mapped.success;
        totals.warning += mapped.warning;
        totals.failed += mapped.failed;
        totals.missed += mapped.missed;
        totals.other += mapped.other;
      }
    }
    return totals;
  }, [data]);

  useSidebar(
    data
      ? {
          label: "Coverage",
          rows: [
            { name: "Servers", value: nfmt(data.servers.length) },
            {
              name: "With problems",
              value: nfmt(data.servers.filter((s) => s.problem_nights > 0).length),
              state: "failed",
            },
            {
              name: "Clean run",
              value: nfmt(data.servers.filter((s) => s.problem_nights === 0).length),
              state: "success",
            },
          ],
        }
      : null,
    { servers: data ? String(data.servers.length) : "" }
  );

  if (error) return <div className="error-box">Could not load servers: {error}</div>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Servers</h1>
          <p className="subtitle">
            One row per protected server, one cell per backup night — oldest on the left.
          </p>
        </div>
        <div className="head-actions">
          <Legend items={chartLegend(legendTotals)} />
        </div>
      </div>

      {/* One filter row above everything it scopes. */}
      <div className="filters">
        <div className="search">
          <span className="icon" aria-hidden>
            ⌕
          </span>
          <input
            type="text"
            placeholder="Search server name"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <Dropdown
          field="Source"
          value={source}
          onChange={setSource}
          options={[
            { value: "", label: "All sources" },
            ...(meta?.sources ?? []).map((s) => ({ value: s.source, label: s.display_name })),
          ]}
        />
        <Dropdown
          field="Timezone"
          value={zone}
          onChange={setZone}
          options={[
            { value: "", label: "All timezones" },
            ...zones.map(([name, count]) => ({ value: name, label: name, count })),
          ]}
          wide
        />
        <Segmented value={days} options={RANGES} onChange={setDays} label="Range" />
        <Check on={problemsOnly} label="Problems only" onChange={setProblemsOnly} />
        <Check
          on={includeHidden}
          label="Include hidden"
          onChange={setIncludeHidden}
          title="Servers you have hidden from every view and count"
        />
        <span className="badge">
          <b>{nfmt(sorted.length)}</b> shown
        </span>
      </div>

      {/* Appears only when something is selected, so it never competes with the
          filters for attention on the way in. */}
      {selected.length > 0 && (
        <div className="bulk-bar" role="region" aria-label="Bulk actions">
          <span className="bulk-count">
            <b>{nfmt(selected.length)}</b> selected
          </span>
          <span className="bulk-names">
            {selectedRows.slice(0, 4).map((r) => r.name).join(", ")}
            {selectedRows.length > 4 ? ` +${selectedRows.length - 4} more` : ""}
          </span>
          <span className="bulk-spacer" />
          {anyVisible ? (
            <button className="btn-primary" disabled={busy} onClick={() => applyHidden(true)}>
              {busy ? "Hiding…" : "Hide from every view"}
            </button>
          ) : (
            <button className="btn-primary" disabled={busy} onClick={() => applyHidden(false)}>
              {busy ? "Restoring…" : "Unhide"}
            </button>
          )}
          <button className="btn-secondary" disabled={busy} onClick={() => setPicked(new Set())}>
            Clear
          </button>
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
          {notice.includes("hidden") && (
            <>
              {" "}
              Tick <b>Include hidden</b> above to see or restore them.
            </>
          )}
        </div>
      )}

      <div className="card flush">
        {!data ? (
          <div style={{ padding: "var(--space-7)" }}>
            <Skeleton height={320} />
          </div>
        ) : sorted.length === 0 ? (
          <div className="empty">No servers match these filters.</div>
        ) : (
          <div className="table-wrap">
            <table className="data pin-first">
              <thead>
                <tr>
                  <th className="pick" style={{ width: 34 }}>
                    <input
                      type="checkbox"
                      checked={allPicked}
                      ref={(el) => {
                        // Some-but-not-all reads as neither ticked nor empty.
                        if (el) el.indeterminate = selected.length > 0 && !allPicked;
                      }}
                      onChange={toggleAll}
                      aria-label={allPicked ? "Clear selection" : "Select all shown"}
                      title={allPicked ? "Clear selection" : "Select all shown"}
                    />
                  </th>
                  <th style={{ minWidth: 150 }}>
                    <SortLabel id="name" sort={sort}>
                      Server
                    </SortLabel>
                  </th>
                  <th style={{ width: 110 }}>
                    <SortLabel id="outcome" sort={sort}>
                      Last night
                    </SortLabel>
                  </th>
                  <th style={{ minWidth: 220 }}>
                    Last {days} nights
                    <span className="meta" style={{ marginLeft: 8 }}>
                      {data.dates.length
                        ? `${fmtDay(data.dates[0], { month: "numeric", day: "numeric" })} → ${fmtDay(
                            data.dates[data.dates.length - 1],
                            { month: "numeric", day: "numeric" }
                          )}`
                        : ""}
                    </span>
                  </th>
                  <th className="num" style={{ width: 90 }}>
                    <SortLabel id="rate" sort={sort} right>
                      Success
                    </SortLabel>
                  </th>
                  <th className="num" style={{ width: 90 }}>
                    <SortLabel id="problems" sort={sort} right>
                      Bad nights
                    </SortLabel>
                  </th>
                  <th style={{ width: 130 }}>
                    <SortLabel id="source" sort={sort}>
                      Source
                    </SortLabel>
                  </th>
                  <th style={{ width: 150 }}>
                    <SortLabel id="timezone" sort={sort}>
                      Timezone
                    </SortLabel>
                  </th>
                  <th className="num" style={{ width: 110 }}>
                    <SortLabel id="last" sort={sort} right>
                      Last good
                    </SortLabel>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((row) => (
                  <tr
                    key={row.id}
                    className={`${row.hidden ? "stale-row" : ""}${
                      picked.has(row.id) ? " picked" : ""
                    }`}
                  >
                    <td className="pick">
                      <input
                        type="checkbox"
                        checked={picked.has(row.id)}
                        onChange={() => toggle(row.id)}
                        aria-label={`Select ${row.name}`}
                      />
                    </td>
                    <td>
                      <Link className="row-link" to={`/servers/${row.id}`}>
                        {row.name}
                      </Link>
                      {row.hidden && (
                        <span className="tz-pill" style={{ marginLeft: 6 }}>
                          hidden
                        </span>
                      )}
                    </td>
                    <td>
                      <StatusChip state={row.last_outcome ?? "none"} />
                    </td>
                    <td>
                      {/* A cell is a night on a server — clicking one opens that
                          server at that night, which is the record behind it. */}
                      <HeatStrip
                        cells={row.days}
                        onSelect={(date) => navigate(`/servers/${row.id}?date=${date}`)}
                      />
                    </td>
                    <td className="num">
                      {row.success_rate == null ? (
                        <span className="muted">—</span>
                      ) : (
                        `${row.success_rate}%`
                      )}
                    </td>
                    <td className="num">
                      {row.problem_nights > 0 ? (
                        <span style={{ color: "var(--o-failed-fg)" }}>{row.problem_nights}</span>
                      ) : (
                        <span className="muted">0</span>
                      )}
                    </td>
                    <td className="muted">
                      {row.sources.map((s) => meta?.sources.find((m) => m.source === s)?.display_name ?? s).join(", ") || "—"}
                    </td>
                    <td>
                      <span className="tz-note">
                        {row.timezone}
                        {row.timezone_origin === "override" && (
                          <span className="tz-pill" title="Set by hand on this server">
                            set
                          </span>
                        )}
                      </span>
                    </td>
                    <td className="num muted">{ageLabel(row.last_success_utc)}</td>
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
