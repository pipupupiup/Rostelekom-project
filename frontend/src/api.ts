// Типы ответов: план — по схемам spec/api_spec.yaml; остальное — /ui-api/state (ado/api/ui.py).

export type StopType = "DEPOT_START" | "VISIT" | "BREAK" | "RESTOCK" | "DEPOT_END";
export type StopStatus = "PLANNED" | "IN_PROGRESS" | "DONE";

export interface Equipment { ONT: number; ROUTER: number; STB: number }
export interface Window { start: string; end: string }

export interface Stop {
  seq: number;
  type: StopType;
  location_id: string;
  order_id: string | null;
  arrival: string;
  start: string;
  end: string;
  travel_sec?: number;
  distance_m?: number;
  status?: StopStatus;
  equipment_after?: Equipment;
  explanation?: string;
}

export interface Route {
  staff_id: string;
  service_center_id: string;
  status: "ACTIVE" | "UNAVAILABLE";
  unavailable_from: string | null;
  unavailable_until: string | null;
  stops: Stop[];
  totals: { visits: number; travel_sec: number; distance_m: number; work_min: number; utilization: number };
}

export interface Unassigned {
  order_id: string;
  reason_code: string;
  reason_text: string;
  suggested_action: "RESCHEDULE" | "REASSIGN" | "ESCALATE";
  proposed_slot: Window | null;
  since: string | null;
}

export interface PgpGroup {
  group_id: string;
  access_node_id: string;
  order_ids: string[];
  detected_at: string;
  action: string;
  comment?: string;
}

export interface Alert {
  alert_id: string;
  created_at: string;
  type: "LATE_RISK" | "PGP_DETECTED" | "UNASSIGNED";
  status: "ACTIVE" | "RESOLVED";
  resolved_at: string | null;
  order_id: string | null;
  group_id: string | null;
  staff_id: string | null;
  forecast_start: string | null;
  lateness_min: number | null;
  suggested_action: string;
  suggested_staff_id: string | null;
  proposed_slot: Window | null;
  applied: boolean;
  message: string;
}

export interface Plan {
  date: string;
  plan_version: number;
  based_on_event_id: string | null;
  as_of: string;
  computation_ms?: number;
  routes: Route[];
  unassigned: Unassigned[];
  pgp_groups: PgpGroup[];
  cancelled: { order_id: string; cancelled_at: string; event_id: string | null }[];
  alerts: Alert[];
  summary?: Record<string, number>;
}

export interface OrderInfo {
  order_id: string;
  work_type_code: string;
  work_type_name: string;
  duration_min: number;
  technology: string;
  building_id: string;
  address: string;
  lat: number;
  lon: number;
  access_node_id: string;
  is_repeat: boolean;
  requires_height: boolean;
  registered_at: string;
  slot: Window;
  latest_start: string;
  deadline: string;
  required_equipment: Equipment;
}

export interface StaffInfo {
  staff_id: string;
  display_name: string;
  service_center_id: string;
  profiles: string[];
  technologies: string[];
  height_permit: boolean;
  shift_start: string;
  shift_end: string;
  break_window: { start: string; end: string };
  /** Окно начала перерыва, по которому строится план (⊆ break_window, D36). */
  break_window_planned: { start: string; end: string };
  break_duration_min: number;
  absent: boolean;
}

export interface Risk {
  order_id: string;
  staff_id: string;
  level: "VIOLATION" | "HIGH";
  forecast_start: string;
  latest_start: string;
  slack_min: number;
  lateness_min: number;
  notice_possible: boolean;
  reason: string;
}

export interface Metrics {
  success: number;
  orders_total: number;
  weighted_success: number;
  weighted_total: number;
  weighted_share: number;
  visits_planned: number;
  unassigned: number;
  pgp_orders: number;
  cancelled: number;
  travel_sec_total: number;
  utilization_cv: number;
  hard_violations: number;
  violations: string[];
}

export interface DemoInfo {
  active: boolean;
  remaining: number;
  sent: number;
  last_event: Record<string, unknown> | null;
  last_message: string | null;
}

export interface UiState {
  loaded: boolean;
  unchanged?: boolean;
  busy?: boolean;
  synthetic: boolean;
  message?: string;
  date?: string;
  now?: string;
  plan_version?: number;
  service_centers?: { service_center_id: string; name: string; lat: number; lon: number }[];
  staff?: StaffInfo[];
  orders?: Record<string, OrderInfo>;
  plan?: Plan | null;
  risks?: Risk[];
  calls_due?: string[];
  metrics?: Metrics | null;
  versions?: { plan_version: number; based_on_event_id: string | null; as_of: string; computation_ms: number | null }[];
  demo?: DemoInfo;
  breaks?: BreakPlan | null;
  /** D39: модель длительностей по истории (null — истории нет, прогноз по нормативу). */
  durations?: { orders: number; quantile: number; source: string; by_type: Record<string, Record<string, number>> } | null;
  duration_forecast?: Record<string, DurationForecast>;
  /** D38: коэффициенты загруженности дорог по часам, если учитываются (иначе null). */
  traffic?: Record<string, number> | null;
  /** D41: наряды, которые по регламенту выполняют двое (ВЛС в частном секторе). */
  joint_orders?: string[];
  /** Отметки сотрудников из мобильной версии: call / arrive / done → время модели,
   *  `*_wall` — когда нажали (часы сервера). */
  marks?: Record<string, Record<string, string>>;
}

export interface DurationForecast {
  min: number;
  norm_min: number;
  n_staff: number;
  n_type: number;
  source: "staff+type" | "type" | "norm";
  staff_id: string;
}

export interface ScenarioRun {
  success: number;
  orders_total?: number;
  weighted_share: number;
  unassigned: number;
  travel_h: number;
  road_h?: number;
  restocks: number;
  hard_violations?: number;
  seconds?: number;
  absent?: string[];
}

export interface ScenarioResult {
  kind: "fewer_staff" | "backpack" | "traffic";
  orders: number;
  base: ScenarioRun;
  scenario: ScenarioRun;
  runs?: ScenarioRun[];
  conclusion: string;
  synthetic: boolean;
  recommended_start?: Record<string, number>;
  current_start?: Record<string, number>;
  capacity?: Record<string, number>;
  p90_need?: Record<string, number>;
  routes_needing_more_than_current?: number;
  routes?: number;
  factors?: Record<string, number>;
}

/** D36: окна перерывов по сменам (входные и суженные для непрерывной работы), причины невозможности. */
export interface BreakPlan {
  ok: boolean;
  changed: boolean;
  issues: string[];
  groups: {
    key: string;
    shift: string;
    break_duration_min: number;
    input_window: string;
    effective_window: string;
    break_busy: string;
    staff: number;
    profiles: Record<string, number>;
  }[];
}

/** D35/D37: линии маршрутов по дорогам (GraphHopper или OSRM). `available: false` — геометрии нет, причина в `message`. */
export interface RouteGeometry {
  plan_version: number;
  configured: boolean;
  available: boolean;
  source: string | null;
  message: string | null;
  routes: Record<string, { coordinates: [number, number][]; distance_km: number; duration_min: number; max_snap_m: number }>;
  errors: Record<string, string>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(res.status, `Сервер вернул не JSON (HTTP ${res.status})`);
  }
  if (!res.ok) {
    const msg = (data as { error?: { message?: string } } | null)?.error?.message ?? `HTTP ${res.status}`;
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

export const api = {
  state: (sinceVersion?: number) =>
    request<UiState>("GET", `/ui-api/state${sinceVersion ? `?since_version=${sinceVersion}` : ""}`),
  demoStart: (seed: number, nOrders: number, timeLimitSec: number) =>
    request<{ plan_version: number; events: number }>("POST", "/ui-api/demo/start", {
      seed,
      n_orders: nOrders,
      time_limit_sec: timeLimitSec,
    }),
  demoExit: () => request<{ exited: boolean; showing: "api_day" | "start"; date: string | null }>("POST", "/ui-api/demo/exit"),
  routeGeometry: (staffIds: string[]) =>
    request<RouteGeometry>("GET", `/ui-api/route-geometry?staff_id=${encodeURIComponent(staffIds.join(","))}`),
  history: (orders: unknown) => request<{ orders: number; source: string }>("POST", "/ui-api/history", orders),
  scenario: (body: Record<string, unknown>) => request<ScenarioResult>("POST", "/ui-api/scenario", body),
  techMark: (staff_id: string, order_id: string, kind: "call" | "arrive" | "done", undo = false) =>
    request<{ marks: Record<string, string> }>("POST", "/ui-api/tech/mark", { staff_id, order_id, kind, undo }),
  techEvent: (body: Record<string, unknown>) => request<{ plan_version: number; message?: string }>("POST", "/ui-api/tech/event", body),
  demoStep: (count: number) =>
    request<{ sent: string[]; remaining: number; result: { message?: string } | null }>("POST", "/ui-api/demo/step", {
      count,
    }),
};
