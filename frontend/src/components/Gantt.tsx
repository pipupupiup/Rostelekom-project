import { useEffect, useMemo, useRef, useState } from "react";
import type { Route, StaffInfo, UiState } from "../api";
import { hhmm, minutesOf, type OrderRow, STATUS_COLOR, staffColor } from "../util";

interface Props {
  state: UiState;
  rows: OrderRow[];
  staff: StaffInfo[];
  selectedStaff: string | null;
  selectedOrder: string | null;
  onSelectStaff: (id: string | null) => void;
  onSelectOrder: (id: string | null) => void;
}

const ROW = 18;
const LABEL = 92;
const T0 = 8 * 60;
const T1 = 21.5 * 60;

/** Загрузка по времени: строка — сотрудник, полосы — визиты (цвет статуса), перерыв, пополнение,
 *  тонкая линия — дорога, серый фон — смена, жёлтый фон — когда может идти обед (D36). Красная обводка — риск опоздания; вертикаль — «сейчас». */
export function Gantt(p: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1100);
  useEffect(() => {
    if (!wrap.current) return;
    const el = wrap.current;
    const obs = new ResizeObserver(() => setWidth(Math.max(700, el.clientWidth - 16)));
    obs.observe(el);
    return () => obs.disconnect();
  }, [p.state.plan === null]);
  const scale = (m: number) => LABEL + ((m - T0) / (T1 - T0)) * (width - LABEL - 8);
  const routes = useMemo(() => new Map((p.state.plan?.routes ?? []).map((r) => [r.staff_id, r])), [p.state.plan]);
  const rowsById = useMemo(() => new Map(p.rows.map((r) => [r.order_id, r])), [p.rows]);
  const staff = p.staff.filter((s) => !s.absent);
  const height = staff.length * ROW + 24;
  const now = p.state.now ? minutesOf(p.state.now) : null;
  const hours: number[] = [];
  for (let h = 8; h <= 21; h++) hours.push(h);

  if (!p.state.plan) return <div className="empty">План ещё не построен</div>;

  return (
    <div className="gantt-wrap" ref={wrap}>
      <svg width={width} height={height} className="gantt" data-testid="gantt">
        {hours.map((h) => (
          <g key={h}>
            <line x1={scale(h * 60)} x2={scale(h * 60)} y1={16} y2={height} className="grid" />
            <text x={scale(h * 60) + 2} y={11} className="axis">{`${h}:00`}</text>
          </g>
        ))}
        {staff.map((s, i) => {
          const y = 18 + i * ROW;
          const r: Route | undefined = routes.get(s.staff_id);
          const sel = s.staff_id === p.selectedStaff;
          return (
            <g key={s.staff_id} className={sel ? "row sel" : "row"}>
              <rect x={0} y={y - 1} width={width} height={ROW} className={sel ? "row-bg sel" : i % 2 ? "row-bg odd" : "row-bg"} />
              <rect
                x={scale(minutesOf(s.shift_start))}
                y={y + 2}
                width={scale(minutesOf(s.shift_end)) - scale(minutesOf(s.shift_start))}
                height={ROW - 6}
                className="shift"
              />
              {s.break_window_planned && (
                <rect
                  x={scale(minutesOf(s.break_window_planned.start))}
                  y={y + 2}
                  width={scale(minutesOf(s.break_window_planned.end) + s.break_duration_min) - scale(minutesOf(s.break_window_planned.start))}
                  height={ROW - 6}
                  className="bw"
                >
                  <title>
                    обед может идти {hhmm(s.break_window_planned.start)}–{hhmm(s.break_window_planned.end)} + {s.break_duration_min} мин
                    (окно начала во входных данных {hhmm(s.break_window.start)}–{hhmm(s.break_window.end)})
                  </title>
                </rect>
              )}
              <text
                x={4}
                y={y + 12}
                className="label"
                onClick={() => p.onSelectStaff(sel ? null : s.staff_id)}
                fill={staffColor(s.staff_id)}
              >
                {s.staff_id} {s.profiles.includes("INSTALL") ? (s.profiles.includes("LTP3") ? "И+3Л" : "И") : "3Л"}
                {r?.status === "UNAVAILABLE" ? " ⛔" : ""}
              </text>
              {r?.status === "UNAVAILABLE" && r.unavailable_from && (
                <rect
                  x={scale(minutesOf(r.unavailable_from))}
                  y={y + 1}
                  width={scale(r.unavailable_until ? minutesOf(r.unavailable_until) : minutesOf(s.shift_end)) - scale(minutesOf(r.unavailable_from))}
                  height={ROW - 4}
                  className="unavail"
                >
                  <title>недоступен {hhmm(r.unavailable_from)}–{hhmm(r.unavailable_until) || "конец смены"}</title>
                </rect>
              )}
              {r?.stops.map((st, k) => {
                const prev = k > 0 ? r.stops[k - 1] : null;
                const travel = prev && st.arrival > prev.end ? (
                  <line
                    key={`t${k}`}
                    x1={scale(minutesOf(prev.end))}
                    x2={scale(minutesOf(st.arrival))}
                    y1={y + ROW / 2 - 1}
                    y2={y + ROW / 2 - 1}
                    className="travel"
                  />
                ) : null;
                if (st.type === "DEPOT_START" || st.type === "DEPOT_END") return travel;
                const x = scale(minutesOf(st.start));
                const w = Math.max(2, scale(minutesOf(st.end)) - x);
                if (st.type === "BREAK")
                  return (
                    <g key={k}>
                      {travel}
                      <rect x={x} y={y + 3} width={w} height={ROW - 8} className="break">
                        <title>перерыв {hhmm(st.start)}–{hhmm(st.end)}</title>
                      </rect>
                    </g>
                  );
                if (st.type === "RESTOCK")
                  return (
                    <g key={k}>
                      {travel}
                      <rect x={x} y={y + 3} width={w} height={ROW - 8} className="restock">
                        <title>пополнение АО в {st.location_id} {hhmm(st.start)}–{hhmm(st.end)}</title>
                      </rect>
                    </g>
                  );
                const row = st.order_id ? rowsById.get(st.order_id) : undefined;
                const isSel = st.order_id === p.selectedOrder;
                return (
                  <g key={k}>
                    {travel}
                    {st.start > st.arrival && (
                      <line x1={scale(minutesOf(st.arrival))} x2={x} y1={y + ROW / 2} y2={y + ROW / 2} className="wait" />
                    )}
                    <rect
                      x={x}
                      y={y + 1}
                      width={w}
                      height={ROW - 4}
                      rx={2}
                      fill={row ? STATUS_COLOR[row.status] : "#2563eb"}
                      className={`visit${row?.risk ? " risk" : ""}${isSel ? " picked" : ""}`}
                      onClick={() => st.order_id && p.onSelectOrder(st.order_id)}
                      data-order={st.order_id ?? undefined}
                    >
                      <title>
                        {st.order_id} · {hhmm(st.start)}–{hhmm(st.end)}
                        {row?.risk ? ` · риск: ${row.risk.reason}` : ""}
                      </title>
                    </rect>
                  </g>
                );
              })}
            </g>
          );
        })}
        {now !== null && now >= T0 && now <= T1 && (
          <line x1={scale(now)} x2={scale(now)} y1={14} y2={height} className="now">
            <title>модельное время {hhmm(p.state.now)}</title>
          </line>
        )}
      </svg>
    </div>
  );
}
