import type { Plan, Risk, Stop, UiState } from "./api";

/** Время суток HH:MM из ISO-строки с +05:00 — без пересчёта поясов браузером. */
export const hhmm = (iso: string | null | undefined): string => (iso ? iso.slice(11, 16) : "—");

/** Минуты от полуночи дня плана (локальное время в строке). */
export const minutesOf = (iso: string): number =>
  Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16)) + Number(iso.slice(17, 19)) / 60;

export const minutesBetween = (a: string, b: string): number => Math.round((Date.parse(b) - Date.parse(a)) / 60000);

export type OrderStatus = "PLANNED" | "IN_PROGRESS" | "DONE" | "UNASSIGNED" | "CANCELLED";

export const STATUS_LABEL: Record<OrderStatus, string> = {
  PLANNED: "запланирован",
  IN_PROGRESS: "выполняется",
  DONE: "выполнен",
  UNASSIGNED: "не распределён",
  CANCELLED: "отменён",
};

export const STATUS_COLOR: Record<OrderStatus, string> = {
  PLANNED: "#2563eb",
  IN_PROGRESS: "#d97706",
  DONE: "#16a34a",
  UNASSIGNED: "#dc2626",
  CANCELLED: "#9ca3af",
};

const PALETTE = [
  "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
  "#e377c2", "#17becf", "#bcbd22", "#4e79a7", "#f28e2b", "#59a14f",
];
export const staffColor = (staffId: string): string => {
  const n = Number(staffId.replace(/\D/g, "")) || 0;
  return PALETTE[n % PALETTE.length];
};

export interface OrderRow {
  order_id: string;
  status: OrderStatus;
  staff_id: string | null;
  stop: Stop | null;
  risk: Risk | null;
}

/** Классификация нарядов по данным плана (ничего не пересчитывается — только раскладка ответа API).
 *  Наряды из `pgp_groups` (переданы в станционное подразделение) в интерфейсе диспетчера не
 *  показываются (D33): API их по-прежнему возвращает, метрики `orders_total` их тоже не считают. */
export function orderRows(state: UiState): OrderRow[] {
  const plan = state.plan as Plan | null | undefined;
  const orders = state.orders ?? {};
  const where = new Map<string, { staff: string; stop: Stop }>();
  const risk = new Map((state.risks ?? []).map((r) => [r.order_id, r]));
  for (const r of plan?.routes ?? []) for (const s of r.stops) if (s.type === "VISIT" && s.order_id) where.set(s.order_id, { staff: r.staff_id, stop: s });
  const un = new Set((plan?.unassigned ?? []).map((u) => u.order_id));
  const pgp = new Set((plan?.pgp_groups ?? []).flatMap((g) => g.order_ids));
  const canc = new Set((plan?.cancelled ?? []).map((c) => c.order_id));
  return Object.keys(orders)
    .sort()
    .filter((oid) => where.has(oid) || !pgp.has(oid))
    .map((oid) => {
      const w = where.get(oid);
      let status: OrderStatus = "UNASSIGNED";
      if (w) status = (w.stop.status ?? "PLANNED") as OrderStatus;
      else if (canc.has(oid)) status = "CANCELLED";
      else if (un.has(oid)) status = "UNASSIGNED";
      return { order_id: oid, status, staff_id: w?.staff ?? null, stop: w?.stop ?? null, risk: risk.get(oid) ?? null };
    });
}

export const fmtHours = (sec: number): string => `${(sec / 3600).toFixed(1)} ч`;
export const pct = (x: number): string => `${(x * 100).toFixed(1)} %`;

/** Сообщение сервера о событии без частей про ПГП (в интерфейсе не показываются, D33). */
export const withoutPgp = (msg: string | null | undefined): string =>
  (msg ?? "")
    .split(/;\s*/)
    .filter((part) => part && !/ПГП/.test(part))
    .join("; ");

/** Предупреждения для экрана диспетчера: без PGP_DETECTED и без снятий нарядов в ПГП (D33). */
export const visibleAlerts = <T extends { type: string; group_id: string | null }>(alerts: T[]): T[] =>
  alerts.filter((a) => a.type !== "PGP_DETECTED" && !a.group_id);
