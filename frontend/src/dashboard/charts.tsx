import { useId, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { Link } from "react-router";

/**
 * Small SVG charts for the dashboard. Every chart has a table twin (the values are never
 * hover-only), thin marks, hairline grids and one y-axis. Colors are the validated roles in
 * index.css (.viz-root): one hue for magnitude, categorical slots 1-2 for the two-part stack.
 */

const WIDTH = 640;
const PLOT = { top: 12, right: 16, bottom: 28, left: 44 };

export function ChartCard({
  title,
  caption,
  table,
  legend,
  children,
}: {
  title: string;
  caption?: string;
  table: ReactNode;
  legend?: ReactNode;
  children: ReactNode;
}) {
  return (
    <figure className="viz-root rounded-xl border border-slate-200 bg-white p-5" aria-label={title}>
      <figcaption>
        <span className="block font-medium text-slate-900">{title}</span>
        {caption && <span className="mt-0.5 block text-xs text-slate-500">{caption}</span>}
      </figcaption>
      {legend && <div className="mt-3">{legend}</div>}
      <div className="mt-3">{children}</div>
      <details className="mt-2 text-xs text-slate-600">
        <summary className="cursor-pointer select-none text-slate-500 hover:text-slate-800">Show as table</summary>
        <div className="mt-2 max-h-64 overflow-auto">{table}</div>
      </details>
    </figure>
  );
}

export function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-600">
      <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: color }} />
      {label}
    </span>
  );
}

function Tooltip({ x, children }: { x: number; children: ReactNode }) {
  // Positioned in percent of the chart width so it follows the responsive SVG.
  const left = `${String(Math.min(Math.max((x / WIDTH) * 100, 12), 88))}%`;
  return (
    <div
      role="status"
      className="pointer-events-none absolute top-0 z-10 -translate-x-1/2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs shadow-sm"
      style={{ left }}
    >
      {children}
    </div>
  );
}

function ticks(max: number): number[] {
  if (max <= 0) return [0];
  const raw = max / 4;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let value = 0; value <= max + step * 0.001; value += step) out.push(Number(value.toFixed(6)));
  if ((out.at(-1) ?? 0) < max) out.push(Number((out.length * step).toFixed(6)));
  return out;
}

function YAxis({
  values,
  scale,
  format,
}: {
  values: number[];
  scale: (v: number) => number;
  format: (v: number) => string;
}) {
  return (
    <g>
      {values.map((value) => (
        <g key={value}>
          <line
            x1={PLOT.left}
            x2={WIDTH - PLOT.right}
            y1={scale(value)}
            y2={scale(value)}
            stroke={value === 0 ? "var(--viz-baseline)" : "var(--viz-grid)"}
            strokeWidth={1}
            shapeRendering="crispEdges"
          />
          <text
            x={PLOT.left - 8}
            y={scale(value)}
            textAnchor="end"
            dominantBaseline="middle"
            fontSize={11}
            fill="var(--viz-muted)"
            style={{ fontVariantNumeric: "tabular-nums" }}
          >
            {format(value)}
          </text>
        </g>
      ))}
    </g>
  );
}

function XLabels({ labels, x, height }: { labels: string[]; x: (index: number) => number; height: number }) {
  // At most ~6 labels: first, last and evenly spaced ones between.
  const every = Math.max(1, Math.ceil(labels.length / 6));
  return (
    <g>
      {labels.map((label, index) =>
        index % every === 0 || index === labels.length - 1 ? (
          <text key={label} x={x(index)} y={height - 8} textAnchor="middle" fontSize={11} fill="var(--viz-muted)">
            {label}
          </text>
        ) : null,
      )}
    </g>
  );
}

function useCursor(count: number) {
  const [cursor, setCursor] = useState<number | null>(null);
  const onKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    if (count === 0) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      setCursor((current) => Math.min(count - 1, Math.max(0, (current ?? count - 1) + step)));
    }
  };
  return { cursor, setCursor, onKeyDown };
}

function indexAt(event: PointerEvent<SVGSVGElement>, x: (index: number) => number, count: number): number {
  const box = event.currentTarget.getBoundingClientRect();
  const position = ((event.clientX - box.left) / box.width) * WIDTH;
  let best = 0;
  for (let index = 1; index < count; index += 1) {
    if (Math.abs(x(index) - position) < Math.abs(x(best) - position)) best = index;
  }
  return best;
}

export interface TrendPoint {
  label: string;
  value: number | null;
  detail?: string;
}

/** One series over time: 2px line, gaps where there is no value, crosshair tooltip. */
export function TrendLine({
  points,
  max,
  format,
  ariaLabel,
  height = 200,
}: {
  points: TrendPoint[];
  max: number;
  format: (value: number) => string;
  ariaLabel: string;
  height?: number;
}) {
  const { cursor, setCursor, onKeyDown } = useCursor(points.length);
  const plotWidth = WIDTH - PLOT.left - PLOT.right;
  const x = (index: number) =>
    PLOT.left + (points.length <= 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
  const y = (value: number) => PLOT.top + (1 - value / max) * (height - PLOT.top - PLOT.bottom);
  const segments: string[] = [];
  let current = "";
  points.forEach((point, index) => {
    if (point.value === null) {
      if (current) segments.push(current);
      current = "";
      return;
    }
    current += `${current ? "L" : "M"}${x(index).toFixed(1)},${y(point.value).toFixed(1)}`;
  });
  if (current) segments.push(current);
  const lastIndex = points.findLastIndex((point) => point.value !== null);
  const hovered = cursor === null ? null : points[cursor];
  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${String(WIDTH)} ${String(height)}`}
        className="h-auto w-full touch-none overflow-visible outline-none focus-visible:ring-2 focus-visible:ring-blue-300"
        role="img"
        aria-label={ariaLabel}
        tabIndex={0}
        onPointerMove={(event) => {
          setCursor(indexAt(event, x, points.length));
        }}
        onPointerLeave={() => {
          setCursor(null);
        }}
        onFocus={() => {
          setCursor(lastIndex >= 0 ? lastIndex : null);
        }}
        onBlur={() => {
          setCursor(null);
        }}
        onKeyDown={onKeyDown}
      >
        <YAxis values={ticks(max)} scale={y} format={format} />
        <XLabels labels={points.map((point) => point.label)} x={x} height={height} />
        {segments.map((path) => (
          <path
            key={path}
            d={path}
            fill="none"
            stroke="var(--viz-series-1)"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}
        {/* Isolated days (no neighbour) and the latest value get a dot. */}
        {points.map((point, index) =>
          point.value !== null &&
          (index === lastIndex ||
            ((points[index - 1]?.value ?? null) === null && (points[index + 1]?.value ?? null) === null)) ? (
            <circle
              key={point.label}
              cx={x(index)}
              cy={y(point.value)}
              r={4}
              fill="var(--viz-series-1)"
              stroke="var(--viz-surface)"
              strokeWidth={2}
            />
          ) : null,
        )}
        {cursor !== null && (
          <line
            x1={x(cursor)}
            x2={x(cursor)}
            y1={PLOT.top}
            y2={height - PLOT.bottom}
            stroke="var(--viz-baseline)"
            strokeWidth={1}
          />
        )}
      </svg>
      {hovered && cursor !== null && (
        <Tooltip x={x(cursor)}>
          <div className="font-semibold text-slate-900">
            {hovered.value === null ? "No data" : format(hovered.value)}
          </div>
          <div className="text-slate-500">{hovered.label}</div>
          {hovered.detail && <div className="text-slate-500">{hovered.detail}</div>}
        </Tooltip>
      )}
    </div>
  );
}

export interface StackedColumn {
  label: string;
  values: [number, number];
}

/** Two-part columns per day (part-to-whole): 2px surface gap between parts, rounded top. */
export function StackedColumns({
  columns,
  series,
  ariaLabel,
  height = 200,
}: {
  columns: StackedColumn[];
  series: [{ label: string; color: string }, { label: string; color: string }];
  ariaLabel: string;
  height?: number;
}) {
  const { cursor, setCursor, onKeyDown } = useCursor(columns.length);
  const clipId = useId();
  const plotWidth = WIDTH - PLOT.left - PLOT.right;
  const band = plotWidth / Math.max(columns.length, 1);
  const barWidth = Math.min(24, Math.max(3, band * 0.6));
  const x = (index: number) => PLOT.left + band * index + band / 2;
  const max = Math.max(1, ...columns.map((column) => column.values[0] + column.values[1]));
  const axis = ticks(max);
  const top = axis.at(-1) ?? max;
  const y = (value: number) => PLOT.top + (1 - value / top) * (height - PLOT.top - PLOT.bottom);
  const hovered = cursor === null ? null : columns[cursor];
  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${String(WIDTH)} ${String(height)}`}
        className="h-auto w-full touch-none outline-none focus-visible:ring-2 focus-visible:ring-blue-300"
        role="img"
        aria-label={ariaLabel}
        tabIndex={0}
        onPointerMove={(event) => {
          setCursor(indexAt(event, x, columns.length));
        }}
        onPointerLeave={() => {
          setCursor(null);
        }}
        onFocus={() => {
          setCursor(columns.length ? columns.length - 1 : null);
        }}
        onBlur={() => {
          setCursor(null);
        }}
        onKeyDown={onKeyDown}
      >
        <YAxis values={axis} scale={y} format={(value) => String(value)} />
        <XLabels labels={columns.map((column) => column.label)} x={x} height={height} />
        {columns.map((column, index) => {
          const [first, second] = column.values;
          if (first + second === 0) return null;
          const left = x(index) - barWidth / 2;
          const base = y(0);
          const firstTop = y(first);
          const totalTop = y(first + second);
          const gap = first > 0 && second > 0 ? 2 : 0;
          const radius = Math.min(4, barWidth / 2);
          return (
            <g key={column.label} opacity={cursor === null || cursor === index ? 1 : 0.55}>
              <clipPath id={`${clipId}-${String(index)}`}>
                {/* Rounded data end (top), square at the baseline. */}
                <rect x={left} y={totalTop} width={barWidth} height={base - totalTop + radius} rx={radius} />
              </clipPath>
              <g clipPath={`url(#${clipId}-${String(index)})`}>
                {first > 0 && (
                  <rect x={left} y={firstTop} width={barWidth} height={base - firstTop} fill={series[0].color} />
                )}
                {second > 0 && (
                  <rect
                    x={left}
                    y={totalTop}
                    width={barWidth}
                    height={Math.max(0, firstTop - totalTop - gap)}
                    fill={series[1].color}
                  />
                )}
              </g>
            </g>
          );
        })}
      </svg>
      {hovered && cursor !== null && (
        <Tooltip x={x(cursor)}>
          <div className="text-slate-500">{hovered.label}</div>
          {series.map((item, index) => (
            <div key={item.label} className="flex items-center gap-1.5">
              <span aria-hidden="true" className="inline-block h-0.5 w-3" style={{ background: item.color }} />
              <span className="font-semibold text-slate-900">{hovered.values[index]}</span>
              <span className="text-slate-500">{item.label}</span>
            </div>
          ))}
        </Tooltip>
      )}
    </div>
  );
}

export interface BarItem {
  key: string;
  label: string;
  value: number;
  href?: string;
}

/** Horizontal bars in one hue, value at the tip (a magnitude comparison). */
export function BarList({ items, ariaLabel }: { items: BarItem[]; ariaLabel: string }) {
  const max = Math.max(1, ...items.map((item) => item.value));
  if (items.length === 0) {
    return <p className="text-sm text-slate-500">Nothing in this period.</p>;
  }
  return (
    <ul aria-label={ariaLabel} className="space-y-2">
      {items.map((item) => (
        <li key={item.key} className="grid grid-cols-[minmax(0,10rem)_1fr] items-center gap-3 text-sm">
          <span className="truncate text-slate-700" title={item.label}>
            {item.href ? (
              <Link to={item.href} className="hover:underline">
                {item.label}
              </Link>
            ) : (
              item.label
            )}
          </span>
          <span className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="block h-3 rounded-r"
              style={{
                width: `${String(Math.max(2, (item.value / max) * 85))}%`,
                background: "var(--viz-series-1)",
              }}
            />
            <span className="text-xs font-medium text-slate-700" style={{ fontVariantNumeric: "tabular-nums" }}>
              {item.value}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}
