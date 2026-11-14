import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type UiState } from "./api";
import { Gantt } from "./components/Gantt";
import { MapView } from "./components/MapView";
import { ScenariosPanel } from "./components/Scenarios";
import { TechView } from "./components/TechView";
import {
  applyFilters,
  EMPTY_FILTERS,
  FilterBar,
  type Filters,
  OrderCard,
  OrdersTable,
  RisksPanel,
  ShiftsPanel,
  StaffPanel,
  UnassignedPanel,
} from "./components/Panels";
import { fmtHours, hhmm, orderRows, pct, withoutPgp } from "./util";

type Tab = "orders" | "risks" | "unassigned" | "shifts" | "scenarios" | "staff";
const POLL_MS = 2000;

/** `/ui/#tech/ST-001` — мобильная версия для сотрудника, иначе — экран диспетчера. */
export function App() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  if (hash.startsWith("#tech")) return <TechView initialStaff={hash.split("/")[1] || null} />;
  return <Dispatcher />;
}

function Dispatcher() {
  const [state, setState] = useState<UiState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("orders");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [selStaff, setSelStaff] = useState<string | null>(null);
  const [selOrder, setSelOrder] = useState<string | null>(null);
  const [allRoutes, setAllRoutes] = useState(false);
  const [flash, setFlash] = useState(false);
  const [serverBusy, setServerBusy] = useState(false);
  const version = useRef<number | undefined>(undefined);

  const inFlight = useRef(false);
  const refresh = useCallback(async (force = false) => {
    if (inFlight.current && !force) return; // не копим запросы, пока сервер считает план
    inFlight.current = true;
    try {
      const s = await api.state(force ? undefined : version.current);
      setError(null);
      setServerBusy(!!s.busy);
      if (s.unchanged) {
        const marks = s.marks;  // отметки сотрудников приходят и без новой версии плана
        if (marks) setState((prev) => (prev && JSON.stringify(prev.marks ?? {}) !== JSON.stringify(marks) ? { ...prev, marks } : prev));
        return;
      }
      if (s.busy && version.current !== undefined) return;
      if (version.current !== undefined && s.plan_version !== version.current) {
        setFlash(true);
        setTimeout(() => setFlash(false), 1500);
      }
      version.current = s.plan_version;
      setState(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelOrder(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    refresh(true);
    const t = setInterval(() => refresh(), POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const rows = useMemo(() => (state?.loaded ? orderRows(state) : []), [state]);
  const filtered = useMemo(() => (state ? applyFilters(rows, filters, state) : []), [rows, filters, state]);
  const visible = useMemo(() => new Set(filtered.map((r) => r.order_id)), [filtered]);
  const staff = state?.staff ?? [];

  const selectStaff = (id: string | null) => {
    setSelStaff(id);
    if (id) setTab("staff");
  };
  const selectOrder = (id: string | null) => {
    setSelOrder(id);
    const r = rows.find((x) => x.order_id === id);
    if (r?.staff_id) setSelStaff(r.staff_id);
  };

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    try {
      await fn();
      await refresh(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const demoActive = !!state?.demo?.active;
  const m = state?.metrics;
  const plan = state?.plan;
  const lastEvent = state?.demo?.last_event as { type?: string; event_id?: string; timestamp?: string } | null;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">ADO · диспетчер</div>
        {state?.loaded && (
          <div className={`kpis${flash ? " flash" : ""}`} data-testid="kpis">
            <span>{state.date}</span>
            <span title="модельное время">⏱ {hhmm(state.now)}</span>
            <span data-testid="plan-version">план v{state.plan_version}{plan?.based_on_event_id ? ` (${plan.based_on_event_id})` : ""}</span>
            {m && (
              <>
                <span title="визит в слот и в контрольный срок, без нарушений; повторные — вес 2">
                  успешно {m.success}/{m.orders_total} · {pct(m.weighted_share)}
                </span>
                <span>в пути {fmtHours(m.travel_sec_total)}</span>
                <span>CV загрузки {m.utilization_cv.toFixed(2)}</span>
                <span className={m.unassigned ? "warn-text" : ""}>не распр. {m.unassigned}</span>
                <span className={(state.risks?.length ?? 0) ? "bad-text" : ""}>риски {state.risks?.length ?? 0}</span>
                {state.traffic && (
                  <span title={Object.entries(state.traffic).map(([h, f]) => `${h}:00 × ${f}`).join(", ")}>🚦 пробки учтены</span>
                )}
                <span className={m.hard_violations ? "bad-text" : "ok-text"} title={m.violations.join("\n")}>
                  нарушений {m.hard_violations}
                </span>
              </>
            )}
          </div>
        )}
        <a className="tech-link" href={`#tech${selStaff ? `/${selStaff}` : ""}`} target="_blank" rel="noreferrer" title="Мобильная версия для сотрудника">
          📱 сотрудник
        </a>
        <div className="demo">
          {demoActive && (
            <span className="badge" title="данные сгенерированы ado.synthetic, не данные кейса; демо работает только в памяти и в БД не пишется">
              ДЕМО · СИНТЕТИКА
            </span>
          )}
          <button
            disabled={!!busy}
            data-testid="demo-start"
            title={demoActive ? "Начать демо-день заново: текущий прогон демо будет заменён новым" : "Синтетический день: 400 нарядов и поток событий"}
            onClick={() => run(demoActive ? "Перезапускаем демо-день…" : "Строим синтетический день…", () => api.demoStart(7, 400, 30))}
          >
            {demoActive ? "↻ перезапустить демо" : "демо-день"}
          </button>
          <button disabled={!!busy || !state?.demo?.active || !state.demo.remaining} onClick={() => run("Событие…", () => api.demoStep(1))} data-testid="step">
            событие ▶
          </button>
          <button disabled={!!busy || !state?.demo?.active || !state.demo.remaining} onClick={() => run("События…", () => api.demoStep(10))}>
            +10
          </button>
          {state?.demo?.active && <span className="muted">осталось {state.demo.remaining}</span>}
          {demoActive && (
            <button
              disabled={!!busy}
              data-testid="demo-exit"
              title="Вернуться к дню, загруженному через API, или к стартовому экрану. Загруженные данные и история версий в БД не удаляются."
              onClick={() => run("Выходим из демо…", async () => {
                await api.demoExit();
                version.current = undefined;
                setSelStaff(null);
                setSelOrder(null);
                setTab("orders");
              })}
            >
              ✕ выйти из демо
            </button>
          )}
        </div>
      </header>

      {error && (
        <div className="banner error" data-testid="error">
          Ошибка связи с сервером: {error}
          {state?.loaded ? ` — на экране последняя полученная версия плана v${state.plan_version}, она может быть устаревшей` : ""}
        </div>
      )}
      {busy && <div className="banner info">{busy}</div>}
      {!busy && serverBusy && <div className="banner info">Сервер рассчитывает план — данные обновятся по готовности</div>}
      {lastEvent && (
        <div className="banner event" data-testid="last-event">
          Последнее событие: {lastEvent.type} {lastEvent.event_id} в {hhmm(lastEvent.timestamp)}
          {withoutPgp(state?.demo?.last_message) ? ` — ${withoutPgp(state?.demo?.last_message)}` : ""}
        </div>
      )}

      {!state ? (
        <div className="empty big">Загрузка…</div>
      ) : !state.loaded ? (
        <div className="empty big" data-testid="not-loaded">
          День не загружен. Загрузите данные через API (PUT /api/v1/reference, …/travel-matrix, /days/{"{date}"}, POST …/plan)
          или нажмите «демо-день» (синтетические данные; демо работает только в памяти, из него можно выйти в любой момент).
        </div>
      ) : (
        <main className="grid">
          <section className="left">
            <div className="toolbar">
              <label>
                <input type="checkbox" checked={allRoutes} onChange={(e) => setAllRoutes(e.target.checked)} /> все маршруты
              </label>
              {selStaff && (
                <button onClick={() => { setSelStaff(null); setSelOrder(null); }}>
                  снять выбор {selStaff}
                </button>
              )}
              <span className="legend">
                <i style={{ background: "#2563eb" }} />план <i style={{ background: "#d97706" }} />идёт <i style={{ background: "#16a34a" }} />выполнен
                <i style={{ background: "#dc2626" }} />не распр. <i style={{ background: "#fde047" }} />СЦ
              </span>
            </div>
            <MapView
              state={state}
              rows={rows}
              selectedStaff={selStaff}
              selectedOrder={selOrder}
              showAllRoutes={allRoutes}
              visibleOrders={visible}
              onSelectStaff={selectStaff}
              onSelectOrder={selectOrder}
            >
              <OrderCard state={state} row={rows.find((r) => r.order_id === selOrder)} onClose={() => setSelOrder(null)} />
            </MapView>
          </section>
          <section className="right">
            <nav className="tabs">
              {([
                ["orders", `Наряды (${filtered.length})`],
                ["risks", `Риски (${state.risks?.length ?? 0})`],
                ["unassigned", `Нераспределённые (${plan?.unassigned.length ?? 0})`],
                ["shifts", state.breaks && !state.breaks.ok ? "Смены ⚠" : "Смены и обеды"],
                ["scenarios", "Сценарии"],
                ["staff", selStaff ? `Маршрут ${selStaff}` : "Маршрут"],
              ] as [Tab, string][]).map(([k, label]) => (
                <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)} data-testid={`tab-${k}`}>
                  {label}
                </button>
              ))}
            </nav>
            <div className="panel">
              {!plan ? (
                <div className="empty">План ещё не построен (POST /api/v1/days/{state.date}/plan)</div>
              ) : tab === "orders" ? (
                <>
                  <FilterBar f={filters} set={(f) => { setFilters(f); setSelStaff(f.staff || selStaff); }} staff={staff} />
                  <OrdersTable rows={filtered} state={state} selected={selOrder} onSelect={selectOrder} />
                </>
              ) : tab === "risks" ? (
                <RisksPanel state={state} onSelect={selectOrder} />
              ) : tab === "unassigned" ? (
                <UnassignedPanel state={state} onSelect={selectOrder} />
              ) : tab === "shifts" ? (
                <ShiftsPanel state={state} />
              ) : tab === "scenarios" ? (
                <ScenariosPanel state={state} onHistoryLoaded={() => refresh(true)} />
              ) : (
                <StaffPanel state={state} staffId={selStaff} onSelectOrder={selectOrder} />
              )}
            </div>
          </section>
          <section className="bottom">
            <Gantt
              state={state}
              rows={rows}
              staff={staff}
              selectedStaff={selStaff}
              selectedOrder={selOrder}
              onSelectStaff={selectStaff}
              onSelectOrder={selectOrder}
            />
          </section>
        </main>
      )}
    </div>
  );
}
