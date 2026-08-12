import {
  CSSProperties,
  ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Counts, Outcome } from "./api";

/* ===========================================================================
   Status system

   These are status colors, not a categorical palette: they mean good/bad, so
   they are reserved and never reused to tell two servers apart. Status is never
   color alone — every chip and cell carries a glyph and a label too.
   =========================================================================== */

export interface StatusMeta {
  glyph: string;
  label: string;
  cls: string; // chip-*/cell-*/sw-* suffix
}

export const STATUS: Record<string, StatusMeta> = {
  success: { glyph: "●", label: "success", cls: "success" },
  warning: { glyph: "▲", label: "warning", cls: "warning" },
  failed: { glyph: "✕", label: "failed", cls: "failed" },
  // Hollow, because nothing happened — the glyph says "absent" even in grayscale.
  missed: { glyph: "○", label: "no backup", cls: "missed" },
  running: { glyph: "◐", label: "running", cls: "other" },
  unknown: { glyph: "?", label: "unknown", cls: "other" },
  other: { glyph: "◐", label: "running / unknown", cls: "other" },
  none: { glyph: "·", label: "no data", cls: "none" },
};

export function meta(state: string | null | undefined): StatusMeta {
  return STATUS[state ?? ""] ?? STATUS.none;
}

/** Worst-first, matching the backend's severity order. */
export const OUTCOME_ORDER: Outcome[] = [
  "failed",
  "missed",
  "warning",
  "running",
  "unknown",
  "success",
];

/**
 * Chart classes. Deliberately five, not six: `running` and `unknown` are
 * transient (a running job resolves within hours; unknown is a vendor-mapping
 * gap) and both want to be slate, which no palette can separate from itself.
 * They fold into one "other" band in charts and stay fully distinct in chips and
 * tables, where a written label carries the meaning instead of a hue.
 */
export type ChartClass = "success" | "other" | "warning" | "missed" | "failed";

/** Good at the bottom, trouble on top where the eye lands. Fixed order, so a
    run of clean nights never repaints the ones with failures. */
export const STACK_ORDER: ChartClass[] = ["success", "other", "warning", "missed", "failed"];

export type ChartCounts = Record<ChartClass, number>;

export function chartCounts(counts: Partial<Counts>): ChartCounts {
  return {
    success: counts.success ?? 0,
    other: (counts.running ?? 0) + (counts.unknown ?? 0),
    warning: counts.warning ?? 0,
    missed: counts.missed ?? 0,
    failed: counts.failed ?? 0,
  };
}

const CHART_LABELS: Record<ChartClass, string> = {
  success: "Success",
  other: "Running / unknown",
  warning: "Warning",
  missed: "No backup",
  failed: "Failed",
};

/** A legend is always present for >= 2 series; "other" only earns a slot when
    it is actually in the data. */
export function chartLegend(totals: ChartCounts) {
  return STACK_ORDER.filter((key) => key !== "other" || totals.other > 0)
    .slice()
    .reverse()
    .map((key) => ({ key, label: CHART_LABELS[key] }));
}

export function StatusChip({
  state,
  label,
  tip,
  className = "",
}: {
  state: string;
  label?: string | null;
  tip?: string;
  className?: string;
}) {
  const m = meta(state);
  return (
    <span className={`chip chip-${m.cls} ${className}`} title={tip}>
      <span className="glyph" aria-hidden>
        {m.glyph}
      </span>
      {label ?? m.label}
    </span>
  );
}

/** Matrix cell: glyph, colored by state. Failures pulse. */
export function Cell({
  state,
  text,
  tip,
}: {
  state: string | null;
  text?: string | null;
  tip?: string;
}) {
  const m = meta(state);
  return (
    <div
      className={`matrix-cell cell-${m.cls}${text ? " two" : ""}${
        state === "failed" ? " ah-crit" : ""
      }`}
      title={tip}
    >
      <span className="glyph" aria-hidden>
        {m.glyph}
      </span>
      {text && <span className="num">{text}</span>}
    </div>
  );
}

export function Legend({ items }: { items: { key: string; label: string }[] }) {
  return (
    <div className="legend">
      {items.map((i) => (
        <span className="key" key={i.key}>
          <span className={`sq sw-${meta(i.key).cls}`} />
          {i.label}
        </span>
      ))}
    </div>
  );
}

/* ===========================================================================
   Intro animation clock — one value drives every count-up and bar so two
   numbers on screen can never disagree mid-animation.
   =========================================================================== */

const DUR = 950;

export function useIntro(): number {
  const [p, setP] = useState(() => {
    if (typeof document === "undefined") return 1;
    // rAF never fires in a background tab: clamp immediately or every derived
    // number renders as 0 forever.
    if (document.hidden) return 1;
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? 1 : 0;
  });
  const raf = useRef(0);
  const safety = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    if (p >= 1) return;
    let done = false;
    const t0 = performance.now();
    const step = (t: number) => {
      const q = Math.min(1, (t - t0) / DUR);
      setP(1 - Math.pow(1 - q, 3));
      if (q < 1 && !done) raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    safety.current = setTimeout(() => {
      done = true;
      setP(1);
    }, DUR + 120);
    const onVis = () => {
      if (document.hidden) setP(1);
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelAnimationFrame(raf.current);
      clearTimeout(safety.current);
      document.removeEventListener("visibilitychange", onVis);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return p;
}

export function countUp(value: number, p: number): number {
  return Math.round(value * p);
}

export function nfmt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString("en-US");
}

/* ===========================================================================
   Time
   =========================================================================== */

function parseUtc(value: string): Date {
  return new Date(value.endsWith("Z") || value.includes("+") ? value : value + "Z");
}

export function fmtDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = parseUtc(value);
  return isNaN(d.getTime()) ? value : d.toLocaleString();
}

/** Time-of-day in the *viewer's* timezone. */
export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = parseUtc(value);
  if (isNaN(d.getTime())) return value;
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function fmtDay(value: string | null | undefined, opts?: Intl.DateTimeFormatOptions): string {
  if (!value) return "—";
  // A report date is a calendar label, not an instant: parse it as local noon so
  // no timezone can shift it to the day before.
  const [y, m, d] = value.split("-").map(Number);
  if (!y || !m || !d) return value;
  return new Date(y, m - 1, d, 12).toLocaleDateString(
    undefined,
    opts ?? { weekday: "short", month: "short", day: "numeric" }
  );
}

export function fmtDayShort(value: string): string {
  return fmtDay(value, { month: "numeric", day: "numeric" });
}

export function hoursSince(value: string | null | undefined): number | null {
  if (!value) return null;
  const d = parseUtc(value);
  if (isNaN(d.getTime())) return null;
  return (Date.now() - d.getTime()) / 3_600_000;
}

/** Days floor rather than round, so 47h reads "1d" and never a misleading "2d". */
export function ageLabel(value: string | null | undefined): string {
  const h = hoursSince(value);
  if (h == null) return "never";
  const mins = Math.max(0, Math.floor(h * 60));
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h}h ${rest}m` : `${h}h`;
}

/* ===========================================================================
   Controls
   =========================================================================== */

export function LiveToggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      className={`live${on ? " on" : ""}`}
      onClick={onToggle}
      title={on ? "Polling for updates" : "Live updates paused"}
    >
      <span className="dot" aria-hidden />
      Live
    </button>
  );
}

export function Segmented<T extends string | number>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label?: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={String(o.value)}
          className={o.value === value ? "on" : ""}
          onClick={() => onChange(o.value)}
          aria-pressed={o.value === value}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Check({
  on,
  label,
  onChange,
  title,
}: {
  on: boolean;
  label: string;
  onChange: (on: boolean) => void;
  title?: string;
}) {
  return (
    <label className={`check${on ? " on" : ""}`} title={title}>
      <input type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

export interface DdOption {
  value: string;
  label: string;
  count?: number | null;
}

export function Dropdown({
  field,
  value,
  options,
  onChange,
  placeholder = "All",
  wide,
}: {
  field: string;
  value: string;
  options: DdOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  wide?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const current = options.find((o) => o.value === value);
  const isSet = value !== "" && !!current;

  return (
    <div className={`dd${open ? " open" : ""}`} ref={root}>
      <button className="dd-trigger" onClick={() => setOpen((o) => !o)}>
        <span className="dd-field">{field}</span>
        <span className={`dd-value${isSet ? " set" : ""}`}>{current?.label ?? placeholder}</span>
        <span className="dd-chev" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div className={`dd-panel${wide ? " wide" : ""}`} role="listbox">
          {options.map((o) => (
            <button
              key={o.value}
              className={`dd-opt${o.value === value ? " on" : ""}`}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
            >
              <span className="tick" aria-hidden>
                ✓
              </span>
              <span className="opt-label">{o.label}</span>
              {o.count != null && <span className="opt-count">{nfmt(o.count)}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ===========================================================================
   Charts

   Every chart here is interactive by default (hover + keyboard focus show the
   same thing) and every one has a table-view twin on its page, because a value
   that is only reachable by hovering is not reachable.
   =========================================================================== */

/** Container width, so charts render at real pixels instead of a distorting
    viewBox stretch. */
function useMeasure<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new ResizeObserver((entries) => {
      setWidth(Math.floor(entries[0].contentRect.width));
    });
    observer.observe(node);
    setWidth(Math.floor(node.getBoundingClientRect().width));
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

interface TipState {
  x: number;
  y: number;
  node: ReactNode;
}

function Tooltip({ tip }: { tip: TipState | null }) {
  if (!tip) return null;
  return (
    <div className="chart-tip" style={{ left: tip.x, top: tip.y }} role="status">
      {tip.node}
    </div>
  );
}

const AXIS_BAND = 24; // reserved inside the container so labels never clip
const Y_GUTTER = 40;
const SEG_GAP = 2; // surface gap between stacked segments — never a border

export interface ColumnPoint extends ChartCounts {
  date: string;
  total: number;
}

export function toColumnPoint(date: string, counts: Partial<Counts>): ColumnPoint {
  const mapped = chartCounts(counts);
  return {
    date,
    ...mapped,
    total: STACK_ORDER.reduce((sum, key) => sum + mapped[key], 0),
  };
}

/**
 * Nightly outcome mix. One bar per backup night, segments in a fixed order so
 * a run of clean nights never repaints the ones with failures.
 */
export function DayColumns({
  points,
  progress = 1,
  height = 170,
  selected,
  onSelect,
}: {
  points: ColumnPoint[];
  progress?: number;
  height?: number;
  selected?: string | null;
  onSelect?: (date: string) => void;
}) {
  const [ref, width] = useMeasure<HTMLDivElement>();
  const [tip, setTip] = useState<TipState | null>(null);
  const plot = height - AXIS_BAND;
  const max = Math.max(1, ...points.map((p) => p.total));
  const inner = Math.max(0, width - Y_GUTTER);
  const step = points.length ? inner / points.length : 0;
  const barWidth = Math.max(2, Math.min(18, step - 3));

  // ~6 labels regardless of range, so a 90-day view doesn't turn into a smear.
  const labelEvery = Math.max(1, Math.ceil(points.length / 6));
  const ticks = [0, Math.round(max / 2), max];

  return (
    <div className="chart" ref={ref} style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label="Backup outcomes per night">
          {ticks.map((t) => {
            const y = plot - (t / max) * (plot - 6);
            return (
              <g key={t}>
                {/* solid hairline, one shade off the surface — never dashed */}
                <line x1={Y_GUTTER} x2={width} y1={y} y2={y} className="grid-line" />
                <text x={Y_GUTTER - 6} y={y + 3} className="axis-text" textAnchor="end">
                  {t}
                </text>
              </g>
            );
          })}
          {points.map((point, index) => {
            const x = Y_GUTTER + index * step + (step - barWidth) / 2;
            let cursor = plot;
            const isSel = selected === point.date;
            return (
              <g
                key={point.date}
                className={`col${onSelect ? " clickable" : ""}${isSel ? " on" : ""}`}
                onMouseMove={(e) =>
                  setTip({
                    x: e.nativeEvent.offsetX + 14,
                    y: Math.max(4, e.nativeEvent.offsetY - 12),
                    node: <ColumnTip point={point} />,
                  })
                }
                onMouseLeave={() => setTip(null)}
                onFocus={() =>
                  setTip({ x: x + barWidth, y: 8, node: <ColumnTip point={point} /> })
                }
                onBlur={() => setTip(null)}
                onClick={onSelect ? () => onSelect(point.date) : undefined}
                tabIndex={onSelect ? 0 : -1}
                onKeyDown={(e) => {
                  if (onSelect && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    onSelect(point.date);
                  }
                }}
              >
                {/* hit target spans the full step so a 3px bar is still clickable */}
                <rect
                  x={Y_GUTTER + index * step}
                  y={0}
                  width={Math.max(step, 8)}
                  height={plot}
                  className="col-hit"
                />
                {STACK_ORDER.map((key) => {
                  const value = point[key];
                  if (!value) return null;
                  const full = (value / max) * (plot - 6) * progress;
                  // Gap is surface showing through, not a stroke around the mark.
                  const h = Math.max(1, full - SEG_GAP);
                  cursor -= full;
                  return (
                    <rect
                      key={key}
                      x={x}
                      y={cursor}
                      width={barWidth}
                      height={h}
                      rx={2}
                      className={`col-seg sw-${key}`}
                    />
                  );
                })}
              </g>
            );
          })}
          {points.map((point, index) =>
            index % labelEvery === 0 ? (
              <text
                key={`l-${point.date}`}
                x={Y_GUTTER + index * step + step / 2}
                y={height - 6}
                className="axis-text"
                textAnchor="middle"
              >
                {fmtDayShort(point.date)}
              </text>
            ) : null
          )}
        </svg>
      )}
      <Tooltip tip={tip} />
    </div>
  );
}

function ColumnTip({ point }: { point: ColumnPoint }) {
  // Worst first, matching how the failure list is ordered elsewhere.
  const rows = [...STACK_ORDER].reverse().filter((k) => point[k] > 0);
  return (
    <>
      <div className="tip-title">{fmtDay(point.date)}</div>
      {rows.map((key) => (
        <div className="tip-row" key={key}>
          <span className={`sq sw-${key}`} />
          <span className="tip-label">{CHART_LABELS[key]}</span>
          <span className="tip-value">{point[key]}</span>
        </div>
      ))}
      {!rows.length && <div className="tip-row muted">no data</div>}
    </>
  );
}

export interface LinePoint {
  date: string;
  value: number | null;
}

/**
 * One duration series over time.
 *
 * The longest job of the night and the median job are both durations, but they
 * differ by an order of magnitude — plotting them together on one axis flattens
 * the median into the baseline, and giving the median its own axis would be a
 * dual-axis chart inventing a correlation. So they are drawn as small multiples
 * instead: same measure, same x range, one honest y scale each.
 */
export function DurationChart({
  points,
  label,
  color = "var(--color-neutral-300)",
  progress = 1,
  height = 132,
}: {
  points: LinePoint[];
  label: string;
  color?: string;
  progress?: number;
  height?: number;
}) {
  const [ref, width] = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const plot = height - AXIS_BAND;
  const inner = Math.max(1, width - Y_GUTTER - 6);
  const max = Math.max(60, ...points.map((p) => p.value ?? 0));
  const step = points.length > 1 ? inner / (points.length - 1) : inner;

  const xAt = (i: number) => Y_GUTTER + i * step;
  const yAt = (v: number) => plot - (v / max) * (plot - 8) * progress;

  const path = useMemo(() => {
    let d = "";
    let open = false;
    points.forEach((p, i) => {
      if (p.value == null) {
        open = false;
        return;
      }
      d += `${open ? "L" : "M"}${xAt(i).toFixed(1)} ${yAt(p.value).toFixed(1)}`;
      open = true;
    });
    return d;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, width, progress, max]);

  const labelEvery = Math.max(1, Math.ceil(points.length / 6));
  const active = hover != null ? points[hover] : null;

  // Mark the endpoint only — a dot on every point is chaos and goes unread.
  const lastIndex = points.reduce<number>((last, p, i) => (p.value != null ? i : last), -1);

  return (
    <div className="chart" ref={ref} style={{ height }}>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={`${label} per night`}
          onMouseMove={(e) => {
            const x = e.nativeEvent.offsetX - Y_GUTTER;
            setHover(Math.max(0, Math.min(points.length - 1, Math.round(x / step))));
          }}
          onMouseLeave={() => setHover(null)}
        >
          {[0, max / 2, max].map((t) => (
            <g key={t}>
              <line x1={Y_GUTTER} x2={width} y1={yAt(t)} y2={yAt(t)} className="grid-line" />
              <text x={Y_GUTTER - 6} y={yAt(t) + 3} className="axis-text" textAnchor="end">
                {fmtDuration(t)}
              </text>
            </g>
          ))}
          {active && (
            <line x1={xAt(hover!)} x2={xAt(hover!)} y1={0} y2={plot} className="crosshair" />
          )}
          <path d={path} className="line" style={{ stroke: color }} />
          {lastIndex >= 0 && (
            <circle
              cx={xAt(lastIndex)}
              cy={yAt(points[lastIndex].value!)}
              r={3.5}
              className="dot-end"
              style={{ fill: color }}
            />
          )}
          {active?.value != null && (
            <circle
              cx={xAt(hover!)}
              cy={yAt(active.value)}
              r={4.5}
              className="dot-end"
              style={{ fill: color }}
            />
          )}
          {points.map((point, index) =>
            index % labelEvery === 0 ? (
              <text
                key={point.date}
                x={xAt(index)}
                y={height - 6}
                className="axis-text"
                textAnchor="middle"
              >
                {fmtDayShort(point.date)}
              </text>
            ) : null
          )}
        </svg>
      )}
      {active && (
        <div
          className="chart-tip"
          style={{ left: Math.min(xAt(hover!) + 12, Math.max(0, width - 150)), top: 4 }}
          role="status"
        >
          <div className="tip-title">{fmtDay(active.date)}</div>
          <div className="tip-row">
            <span className="sq" style={{ background: color }} />
            <span className="tip-label">{label}</span>
            <span className="tip-value">{fmtDuration(active.value)}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/** One server's recent nights, oldest to newest. */
export function HeatStrip({
  cells,
  onSelect,
}: {
  cells: { date: string; outcome: string | null; duration_sec?: number | null }[];
  onSelect?: (date: string) => void;
}) {
  return (
    <div className="heat">
      {cells.map((cell) => {
        const m = meta(cell.outcome);
        return (
          <button
            key={cell.date}
            className={`heat-cell sw-${m.cls}${cell.outcome ? "" : " empty"}`}
            title={`${fmtDay(cell.date)} — ${m.label}${
              cell.duration_sec != null ? ` (${fmtDuration(cell.duration_sec)})` : ""
            }`}
            onClick={onSelect ? () => onSelect(cell.date) : undefined}
            aria-label={`${cell.date}: ${m.label}`}
          />
        );
      })}
    </div>
  );
}

/** Proportion bar. Segments are separated by a surface gap, not a border. */
export function StackBar({
  counts,
  progress = 1,
  height = 8,
}: {
  counts: Partial<Counts>;
  progress?: number;
  height?: number;
}) {
  const mapped = chartCounts(counts);
  const segments = STACK_ORDER.filter((k) => mapped[k] > 0);
  const total = segments.reduce((sum, k) => sum + mapped[k], 0);
  return (
    <div className="bar-track r4" style={{ height, gap: segments.length > 1 ? SEG_GAP : 0 }}>
      {segments.map((key) => (
        <div
          key={key}
          className={`bar-seg sw-${key}`}
          style={{ flexGrow: mapped[key] * progress + 0.0001 }}
          title={`${CHART_LABELS[key]}: ${mapped[key]}${
            total ? ` (${Math.round((mapped[key] / total) * 100)}%)` : ""
          }`}
        />
      ))}
    </div>
  );
}

export function Sparkline({
  points,
  progress = 1,
  width = 120,
  height = 30,
  color = "var(--color-ok)",
}: {
  points: number[];
  progress?: number;
  width?: number;
  height?: number;
  color?: string;
}) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points
    .map(
      (v, i) =>
        `${i === 0 ? "M" : "L"}${((i / (points.length - 1)) * width).toFixed(1)} ${(
          height - 2 - ((v - min) / span) * (height - 4)
        ).toFixed(1)}`
    )
    .join(" ");
  const dash = 300;
  return (
    <svg width={width} height={height} aria-hidden style={{ overflow: "visible" }}>
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeDasharray={dash}
        strokeDashoffset={dash * (1 - progress)}
        style={{ transition: "stroke-dashoffset 1.1s ease-out" }}
      />
    </svg>
  );
}

/* ===========================================================================
   Sorting
   =========================================================================== */

export interface SortState {
  key: string | null;
  dir: 1 | -1;
  toggle: (k: string) => void;
}

export function useSort<T>(
  rows: T[],
  accessors: Record<string, (r: T) => unknown>,
  initialKey?: string,
  initialDir: 1 | -1 = 1
): { sorted: T[]; sort: SortState } {
  const [key, setKey] = useState<string | null>(initialKey ?? null);
  const [dir, setDir] = useState<1 | -1>(initialDir);
  const sorted = useMemo(() => {
    const get = key ? accessors[key] : undefined;
    if (!get) return rows;
    return [...rows].sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      const ea = va == null || va === "";
      const eb = vb == null || vb === "";
      if (ea && eb) return 0;
      if (ea) return 1; // empties always sink, regardless of direction
      if (eb) return -1;
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
      return (
        String(va).localeCompare(String(vb), undefined, { numeric: true, sensitivity: "base" }) * dir
      );
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, key, dir]);
  const toggle = useCallback((k: string) => {
    setKey((current) => {
      if (current === k) {
        setDir((d) => (d === 1 ? -1 : 1));
        return current;
      }
      setDir(1);
      return k;
    });
  }, []);
  return { sorted, sort: { key, dir, toggle } };
}

export function SortLabel({
  id,
  sort,
  children,
  right,
  style,
  title,
}: {
  id: string;
  sort: SortState;
  children: ReactNode;
  right?: boolean;
  style?: CSSProperties;
  title?: string;
}) {
  const active = sort.key === id;
  return (
    <button
      className={`col-label${right ? " right" : ""}${active ? " sorted" : ""}`}
      onClick={() => sort.toggle(id)}
      style={style}
      title={title}
    >
      {children}
      <span className="sort-ind" aria-hidden>
        {active ? (sort.dir === 1 ? "▲" : "▼") : "↕"}
      </span>
    </button>
  );
}

export function Th({
  id,
  sort,
  children,
  num,
}: {
  id: string;
  sort: SortState;
  children: ReactNode;
  num?: boolean;
}) {
  const active = sort.key === id;
  return (
    <th
      className={`th-sort ${active ? "sorted" : ""} ${num ? "num" : ""}`}
      onClick={() => sort.toggle(id)}
    >
      {children}
      <span className="sort-ind" aria-hidden>
        {active ? (sort.dir === 1 ? "▲" : "▼") : "↕"}
      </span>
    </th>
  );
}

export function Skeleton({ height, style }: { height: number; style?: CSSProperties }) {
  return <div className="skel" style={{ height, ...style }} aria-hidden />;
}

/* ===========================================================================
   Sidebar footer — each screen fills it with its own summary
   =========================================================================== */

export interface FootRow {
  name: string;
  value?: string;
  state?: string;
}
export interface SidebarInfo {
  label: string;
  rows: FootRow[];
}

export const SidebarContext = createContext<{
  setFoot: (info: SidebarInfo | null) => void;
  setCounts: (counts: Record<string, string>) => void;
}>({ setFoot: () => {}, setCounts: () => {} });

export function useSidebar(info: SidebarInfo | null, counts?: Record<string, string>) {
  const { setFoot, setCounts } = useContext(SidebarContext);
  const infoKey = JSON.stringify(info);
  const countKey = JSON.stringify(counts ?? {});
  useEffect(() => {
    setFoot(info);
    if (counts) setCounts(counts);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [infoKey, countKey]);
}

/* ===========================================================================
   Icons — 24x24 stroke paths, fill none, currentColor
   =========================================================================== */

function I({ d }: { d: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d={d} />
    </svg>
  );
}

export const Icons = {
  overview: () => <I d="M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z" />,
  servers: () => (
    <I d="M3 5h18v5H3zM3 14h18v5H3zM7 7.5h.01M7 16.5h.01" />
  ),
  history: () => <I d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2" />,
  sync: () => (
    <I d="M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15" />
  ),
  shield: () => <I d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />,
  clock: () => <I d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2" />,
};
