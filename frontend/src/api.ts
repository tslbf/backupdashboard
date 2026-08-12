export type Outcome = "success" | "warning" | "failed" | "missed" | "running" | "unknown";

export type Counts = Record<Outcome, number>;

export interface SourceMeta {
  source: string;
  display_name: string;
  default_timezone: string;
  timezone_label: string;
  expected: boolean;
}

export interface Meta {
  display_timezone: string;
  display_timezone_label: string;
  night_cutoff_hour: number;
  current_report_date: string;
  generated_at: string;
  stale_server_days: number;
  sources: SourceMeta[];
  outcome_labels: Record<string, string>;
}

export interface Problem {
  server_id: number;
  server: string;
  source: string;
  source_name: string;
  outcome: Outcome;
  result_raw: string | null;
  end_utc: string | null;
  end_local: string | null;
  timezone: string;
  timezone_label: string;
  duration_sec: number | null;
  duration_label: string;
  streak: number;
  last_success_utc: string | null;
  last_success_days: number | null;
  event_count: number;
}

export interface SourceBreakdown extends Counts {
  source: string;
  display_name: string;
  default_timezone: string | null;
  timezone_label: string;
  total: number;
}

export interface Overview {
  report_date: string;
  previous_date: string;
  generated_at: string;
  display_timezone: string;
  night_cutoff_hour: number;
  is_current: boolean;
  counts: Counts;
  previous_counts: Counts;
  servers_total: number;
  servers_protected: number;
  protected_pct: number | null;
  jobs_total: number;
  problems: Problem[];
  per_source: SourceBreakdown[];
  attention: {
    never_succeeded: { server_id: number; server: string; last_event_utc: string | null }[];
    stale_success: {
      server_id: number;
      server: string;
      last_success_utc: string | null;
      days: number | null;
    }[];
  };
}

export interface TrendPoint extends Counts {
  date: string;
  total: number;
  success_rate: number | null;
}

export interface DurationPoint {
  date: string;
  median_sec: number | null;
  max_sec: number | null;
  jobs: number;
}

export interface DayCell {
  date: string;
  outcome: Outcome | null;
  duration_sec?: number | null;
  sources?: string[];
}

export interface ServerRow {
  id: number;
  name: string;
  display_name: string;
  primary_source: string | null;
  timezone: string;
  timezone_origin: "override" | "source" | "default";
  timezone_label: string;
  night_cutoff_hour: number;
  cutoff_is_override: boolean;
  expected: boolean;
  hidden: boolean;
  notes: string | null;
  last_event_utc: string | null;
  last_success_utc: string | null;
  sources: string[];
  days: DayCell[];
  success_rate: number | null;
  problem_nights: number;
  last_outcome: Outcome | null;
}

export interface ServersResponse {
  dates: string[];
  servers: ServerRow[];
}

export interface ServerEvent {
  id: number;
  source: string;
  source_name: string;
  job_name: string | null;
  report_date: string;
  start_utc: string | null;
  end_utc: string | null;
  start_local: string | null;
  end_local: string | null;
  duration_sec: number | null;
  duration_label: string;
  outcome: Outcome;
  result_raw: string | null;
}

export interface ServerDetail extends Omit<ServerRow, "days" | "success_rate" | "problem_nights" | "last_outcome" | "sources"> {
  counts: Counts;
  timeline: DayCell[];
  streak: number;
  events: ServerEvent[];
}

export interface DayRow {
  server_id: number;
  server: string;
  source: string;
  source_name: string;
  outcome: Outcome;
  result_raw: string | null;
  end_utc: string | null;
  end_local: string | null;
  timezone: string;
  duration_sec: number | null;
  duration_label: string;
  event_count: number;
}

export interface DayDetail {
  report_date: string;
  counts: Counts;
  rows: DayRow[];
}

export interface CollectorStatus {
  source: string;
  display_name: string;
  configured: boolean;
  interval_minutes: number;
  default_timezone: string | null;
  last_run: {
    started_at: string | null;
    finished_at: string | null;
    status: string;
    records: number;
    message: string | null;
  } | null;
}

export interface ServerPatch {
  timezone?: string;
  night_cutoff_hour?: number;
  expected?: boolean;
  hidden?: boolean;
  notes?: string;
  clear_timezone?: boolean;
  clear_cutoff?: boolean;
}

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

export const api = {
  meta: () => get<Meta>("/api/meta"),
  overview: (date?: string) => get<Overview>(`/api/overview${date ? `?date=${date}` : ""}`),
  trends: (days = 30, source?: string) =>
    get<TrendPoint[]>(`/api/trends?days=${days}${source ? `&source=${source}` : ""}`),
  durations: (days = 30) => get<DurationPoint[]>(`/api/trends/duration?days=${days}`),
  servers: (days = 14, includeHidden = false) =>
    get<ServersResponse>(`/api/servers?days=${days}&include_hidden=${includeHidden}`),
  server: (id: string | number, days = 60) =>
    get<ServerDetail>(`/api/servers/${id}?days=${days}`),
  day: (date: string) => get<DayDetail>(`/api/days/${date}`),
  timezones: () => get<string[]>("/api/timezones"),
  collectors: () => get<CollectorStatus[]>("/api/collectors"),
  runCollector: (source: string) => fetch(`/api/collectors/${source}/run`, { method: "POST" }),
  refresh: () => fetch("/api/refresh", { method: "POST" }),
  patchServer: (id: number, patch: ServerPatch) =>
    fetch(`/api/servers/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json() as Promise<ServerRow>;
    }),
};
