import type { Alert, StaffInfo, UiState } from "../api";
import { hhmm, type OrderRow, type OrderStatus, STATUS_COLOR, STATUS_LABEL, fmtHours, visibleAlerts } from "../util";

export interface Filters {
  staff: string;
  profile: string;
  status: string;
  from: string;
  to: string;
  q: string;
}
export const EMPTY_FILTERS: Filters = { staff: "", profile: "", status: "", from: "", to: "", q: "" };

const REASON: Record<string, string> = {
  NO_QUALIFIED_STAFF: "нет допущенного сотрудника",
  NO_SHIFT_TIME: "не хватает времени смены",
  SLOT_UNREACHABLE: "слот недостижим",
  NO_EQUIPMENT: "нет АО",
  STAFF_UNAVAILABLE: "исполнитель недоступен",
  OTHER: "другое",
};
const ACTION: Record<string, string> = {
  RESCHEDULE: "перенос",
  REASSIGN: "передать",
  ESCALATE: "решение диспетчера",
  TRANSFER_TO_STATION: "в станционное",
  NONE: "—",
};

export function applyFilters(rows: OrderRow[], f: Filters, state: UiState): OrderRow[] {
  const orders = state.orders ?? {};
  const staffProfiles = new Map((state.staff ?? []).map((s) => [s.staff_id, s.profiles]));
  const q = f.q.trim().toLowerCase();
  return rows.filter((r) => {
    const o = orders[r.order_id];
    if (!o) return false;
    if (f.staff && r.staff_id !== f.staff) return false;
    if (f.status && r.status !== f.status && !(f.status === "RISK" && r.risk)) return false;
    if (f.profile) {
      const install = ["INST_PON_MKD", "INST_FTTB_MKD", "INST_PRIVATE_VLS", "ADD_SERVICE"].includes(o.work_type_code);
      const ltp = ["REPAIR", "DEGRADATION", "DISMANTLE"].includes(o.work_type_code);
      if (f.profile === "INSTALL" && !install && !(r.staff_id && staffProfiles.get(r.staff_id)?.includes("INSTALL"))) return false;
      if (f.profile === "LTP3" && !ltp && !(r.staff_id && staffProfiles.get(r.staff_id)?.includes("LTP3"))) return false;
    }
    const start = hhmm(o.slot.start);
    if (f.from && start < f.from) return false;
    if (f.to && start > f.to) return false;
    if (q && !(r.order_id.toLowerCase().includes(q) || o.address.toLowerCase().includes(q))) return false;
    return true;
  });
}

export function FilterBar({ f, set, staff }: { f: Filters; set: (f: Filters) => void; staff: StaffInfo[] }) {
  return (
    <div className="filters" data-testid="filters">
      <select value={f.staff} onChange={(e) => set({ ...f, staff: e.target.value })} aria-label="сотрудник">
        <option value="">все сотрудники</option>
        {staff.map((s) => (
          <option key={s.staff_id} value={s.staff_id}>
            {s.staff_id} {s.profiles.join("/")}
          </option>
        ))}
      </select>
      <select value={f.profile} onChange={(e) => set({ ...f, profile: e.target.value })} aria-label="профиль">
        <option value="">все профили</option>
        <option value="INSTALL">инсталляция</option>
        <option value="LTP3">3ЛТП</option>
      </select>
      <select value={f.status} onChange={(e) => set({ ...f, status: e.target.value })} aria-label="статус">
        <option value="">все статусы</option>
        {(Object.keys(STATUS_LABEL) as OrderStatus[]).map((s) => (
          <option key={s} value={s}>
            {STATUS_LABEL[s]}
          </option>
        ))}
        <option value="RISK">с риском опоздания</option>
      </select>
      <label>
        слот с <input type="time" value={f.from} onChange={(e) => set({ ...f, from: e.target.value })} />
      </label>
      <label>
        по <input type="time" value={f.to} onChange={(e) => set({ ...f, to: e.target.value })} />
      </label>
      <input placeholder="наряд / адрес" value={f.q} onChange={(e) => set({ ...f, q: e.target.value })} />
      <button onClick={() => set(EMPTY_FILTERS)}>сброс</button>
    </div>
  );
}

export function OrdersTable({ rows, state, selected, onSelect }: { rows: OrderRow[]; state: UiState; selected: string | null; onSelect: (id: string) => void }) {
  const orders = state.orders ?? {};
  if (!rows.length) return <div className="empty">Нет нарядов по выбранным фильтрам</div>;
  return (
    <table className="tbl" data-testid="orders-table">
      <thead>
        <tr>
          <th>Наряд</th><th>Тип</th><th>Слот</th><th>Статус</th><th>Исполнитель</th><th>Прибытие</th><th>Начало–конец</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const o = orders[r.order_id];
          return (
            <tr key={r.order_id} className={r.order_id === selected ? "sel" : ""} onClick={() => onSelect(r.order_id)}>
              <td>
                {r.order_id.slice(-4)}
                {o.is_repeat && <span className="tag rep" title="повторное обращение (вес 2)">П</span>}
                {state.joint_orders?.includes(r.order_id) && (
                  <span className="tag warn" title="по регламенту — два сотрудника; совместные наряды не реализованы (API: один маршрут)">2 чел.</span>
                )}
              </td>
              <td title={o.work_type_name}>{o.work_type_code}</td>
              <td>{hhmm(o.slot.start)}–{hhmm(o.slot.end)}</td>
              <td>
                <span className="dot" style={{ background: STATUS_COLOR[r.status] }} />
                {STATUS_LABEL[r.status]}
                {state.marks?.[r.order_id]?.done ? (
                  <span className="tag rep" title={`сотрудник отметил «завершил» в ${hhmm(state.marks[r.order_id].done_wall)}`}>✅ отм.</span>
                ) : state.marks?.[r.order_id]?.arrive ? (
                  <span className="tag rep" title="сотрудник отметил прибытие">📍 на месте</span>
                ) : null}
                {r.risk && <span className={`tag ${r.risk.level === "VIOLATION" ? "bad" : "warn"}`}>риск</span>}
              </td>
              <td>{r.staff_id ?? "—"}</td>
              <td>{r.stop ? hhmm(r.stop.arrival) : "—"}</td>
              <td>{r.stop ? `${hhmm(r.stop.start)}–${hhmm(r.stop.end)}` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function RisksPanel({ state, onSelect }: { state: UiState; onSelect: (id: string) => void }) {
  const risks = state.risks ?? [];
  const late = visibleAlerts(state.plan?.alerts ?? []).filter((a) => a.type === "LATE_RISK").slice().reverse();
  return (
    <div data-testid="risks">
      <h4>Прогноз опоздания по текущему плану</h4>
      <div className="muted">
        Длительность работ в прогнозе:{" "}
        {state.durations ? `по истории (${state.durations.orders} нарядов, P${Math.round(state.durations.quantile * 100)})` : "по нормативу (история не загружена — вкладка «Сценарии»)"}
        {state.traffic ? "; дороги — с коэффициентами по часам" : ""}
      </div>
      {risks.length === 0 ? (
        <div className="empty small">Рисков нет: все предстоящие визиты укладываются в слот</div>
      ) : (
        <table className="tbl">
          <thead><tr><th>Наряд</th><th>Сотрудник</th><th>Уровень</th><th>Прогноз</th><th>Последний старт</th><th>Причина</th></tr></thead>
          <tbody>
            {risks.map((r) => (
              <tr key={`${r.order_id}${r.level}`} onClick={() => onSelect(r.order_id)}>
                <td>{r.order_id.slice(-4)}</td>
                <td>{r.staff_id}</td>
                <td><span className={`tag ${r.level === "VIOLATION" ? "bad" : "warn"}`}>{r.level === "VIOLATION" ? "нарушение" : "риск"}</span></td>
                <td>{hhmm(r.forecast_start)}</td>
                <td>{hhmm(r.latest_start)}</td>
                <td>{r.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h4>Предупреждения о переносе / передаче (LATE_RISK)</h4>
      <AlertsTable alerts={late} onSelect={onSelect} empty="Предупреждений не было" />
      {(state.calls_due ?? []).length > 0 && (
        <>
          <h4>Пора звонить абоненту (за 60–30 мин до слота)</h4>
          <div className="chips">{state.calls_due!.map((o) => <span key={o} className="chip" onClick={() => onSelect(o)}>{o.slice(-4)}</span>)}</div>
        </>
      )}
    </div>
  );
}

function AlertsTable({ alerts, onSelect, empty }: { alerts: Alert[]; onSelect: (id: string) => void; empty: string }) {
  if (!alerts.length) return <div className="empty small">{empty}</div>;
  return (
    <table className="tbl">
      <thead><tr><th>Создано</th><th>Наряд</th><th>Было</th><th>Действие</th><th>Сообщение</th></tr></thead>
      <tbody>
        {alerts.map((a) => (
          <tr key={a.alert_id} onClick={() => a.order_id && onSelect(a.order_id)}>
            <td>{hhmm(a.created_at)}</td>
            <td>{a.order_id?.slice(-4) ?? "—"}</td>
            <td>{a.staff_id ?? "—"}</td>
            <td>{ACTION[a.suggested_action] ?? a.suggested_action}{a.suggested_staff_id ? ` → ${a.suggested_staff_id}` : ""}{a.proposed_slot ? ` ${a.proposed_slot.start.slice(5, 10)} ${hhmm(a.proposed_slot.start)}` : ""}</td>
            <td>{a.message}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function UnassignedPanel({ state, onSelect }: { state: UiState; onSelect: (id: string) => void }) {
  const list = state.plan?.unassigned ?? [];
  const orders = state.orders ?? {};
  const byReason = new Map<string, number>();
  for (const u of list) byReason.set(u.reason_code, (byReason.get(u.reason_code) ?? 0) + 1);
  if (!list.length) return <div className="empty">Все наряды распределены</div>;
  return (
    <div data-testid="unassigned">
      <div className="chips">
        {[...byReason].map(([k, n]) => <span key={k} className="chip">{REASON[k] ?? k}: {n}</span>)}
      </div>
      <table className="tbl">
        <thead><tr><th>Наряд</th><th>Тип</th><th>Слот</th><th>Причина</th><th>Действие</th><th>Пояснение</th></tr></thead>
        <tbody>
          {list.map((u) => {
            const o = orders[u.order_id];
            return (
              <tr key={u.order_id} onClick={() => onSelect(u.order_id)}>
                <td>{u.order_id.slice(-4)}</td>
                <td>{o?.work_type_code}</td>
                <td>{o ? `${hhmm(o.slot.start)}–${hhmm(o.slot.end)}` : "—"}</td>
                <td>{REASON[u.reason_code] ?? u.reason_code}</td>
                <td>{ACTION[u.suggested_action]}{u.proposed_slot ? ` на ${u.proposed_slot.start.slice(5, 10)} ${hhmm(u.proposed_slot.start)}` : ""}</td>
                <td>{u.reason_text}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function StaffPanel({ state, staffId, onSelectOrder }: { state: UiState; staffId: string | null; onSelectOrder: (id: string) => void }) {
  if (!staffId) return <div className="empty">Выберите сотрудника на диаграмме, карте или в фильтре</div>;
  const s = state.staff?.find((x) => x.staff_id === staffId);
  const r = state.plan?.routes.find((x) => x.staff_id === staffId);
  if (!s) return <div className="empty">Сотрудник не найден</div>;
  return (
    <div data-testid="staff-panel">
      <div className="card">
        <b>{s.staff_id}</b> {s.display_name} · {s.profiles.join(", ")} · {s.technologies.join(", ")}
        {s.height_permit ? " · допуск к высоте" : ""} · СЦ {s.service_center_id} · смена {hhmm(s.shift_start)}–{hhmm(s.shift_end)}
        {r && (
          <div className="muted">
            {r.status === "UNAVAILABLE" ? `недоступен с ${hhmm(r.unavailable_from)}${r.unavailable_until ? ` до ${hhmm(r.unavailable_until)}` : " до конца смены"} · ` : ""}
            визитов {r.totals.visits} · работы {r.totals.work_min} мин · в пути {fmtHours(r.totals.travel_sec)} · загрузка {(r.totals.utilization * 100).toFixed(0)} %
          </div>
        )}
      </div>
      {!r || r.stops.length === 0 ? (
        <div className="empty small">Маршрут пуст</div>
      ) : (
        <table className="tbl">
          <thead><tr><th>#</th><th>Тип</th><th>Где</th><th>Прибытие</th><th>Начало</th><th>Конец</th><th>Статус</th><th>Почему назначен</th></tr></thead>
          <tbody>
            {r.stops.map((st) => (
              <tr key={st.seq} onClick={() => st.order_id && onSelectOrder(st.order_id)}>
                <td>{st.seq}</td>
                <td>{st.type}</td>
                <td>{st.order_id ? st.order_id.slice(-4) : st.location_id}</td>
                <td>{hhmm(st.arrival)}</td>
                <td>{hhmm(st.start)}</td>
                <td>{hhmm(st.end)}</td>
                <td>{st.status === "DONE" ? "выполнено" : st.status === "IN_PROGRESS" ? "идёт" : "план"}</td>
                <td className="muted">{st.explanation ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function OrderCard({ state, row, onClose }: { state: UiState; row: OrderRow | undefined; onClose: () => void }) {
  if (!row) return null;
  const o = state.orders?.[row.order_id];
  if (!o) return null;
  const un = state.plan?.unassigned.find((u) => u.order_id === row.order_id);
  const fc = state.duration_forecast?.[row.order_id];
  const joint = state.joint_orders?.includes(row.order_id);
  const mk = state.marks?.[row.order_id];
  return (
    <div className="order-card" data-testid="order-card" role="dialog" aria-label={`Наряд ${o.order_id}`}>
      <div className="oc-head">
        <b>{o.order_id}</b>
        <button className="oc-close" onClick={onClose} aria-label="закрыть карточку" title="закрыть (Esc)">×</button>
      </div>
      <div className="oc-body">
        <div>{o.work_type_name}</div>
        <div className="muted">
          {o.technology} · {o.duration_min} мин{o.is_repeat ? " · повторное (вес 2)" : ""}{o.requires_height ? " · работы на высоте / ВЛС" : ""}
        </div>
        <dl>
          <dt>Адрес</dt>
          <dd>
            {o.address}
            <div className="muted">
              {state.synthetic ? "синтетический адрес из демо-данных" : "из входных данных"} · {o.lat.toFixed(5)}, {o.lon.toFixed(5)} · дом {o.building_id} · узел {o.access_node_id}
            </div>
          </dd>
          <dt>Слот</dt>
          <dd>{hhmm(o.slot.start)}–{hhmm(o.slot.end)}, последний старт {hhmm(o.latest_start)}</dd>
          <dt>Срок</dt>
          <dd>{o.deadline.slice(8, 10)}.{o.deadline.slice(5, 7)} {hhmm(o.deadline)}</dd>
          <dt>АО</dt>
          <dd>ONT {o.required_equipment.ONT} · роутер {o.required_equipment.ROUTER} · приставка {o.required_equipment.STB}</dd>
          <dt>Статус</dt>
          <dd>
            <span className="dot" style={{ background: STATUS_COLOR[row.status] }} />
            {STATUS_LABEL[row.status]}
            {row.staff_id && ` · ${row.staff_id}`}
            {row.stop && <div>прибытие {hhmm(row.stop.arrival)}, работы {hhmm(row.stop.start)}–{hhmm(row.stop.end)}</div>}
          </dd>
        </dl>
        {fc && fc.source !== "norm" && (
          <p>
            Длительность: норматив {fc.norm_min} мин, по истории ≈ <b>{fc.min} мин</b>
            <span className="muted"> (P{Math.round((state.durations?.quantile ?? 0.75) * 100)}; нарядов сотрудника {fc.n_staff}, типа {fc.n_type})</span>
          </p>
        )}
        {joint && (
          <p className="warn-text-dark">
            По регламенту — два сотрудника одновременно. API кейса назначает наряд только в один маршрут (H9), поэтому второй
            сотрудник не планируется — совместные наряды не реализованы.
          </p>
        )}
        {mk && (mk.call || mk.arrive || mk.done) && (
          <p className="muted">
            Отметки сотрудника (время нажатия): {mk.call ? `звонок ${hhmm(mk.call_wall)} ` : ""}
            {mk.arrive ? `прибыл ${hhmm(mk.arrive_wall)} ` : ""}{mk.done ? `завершил ${hhmm(mk.done_wall)}` : ""}
          </p>
        )}
        {row.stop?.explanation && <p className="muted">Почему этот сотрудник: {row.stop.explanation}</p>}
        {un && <p className="bad-text">Не распределён: {un.reason_text}</p>}
        {row.risk && <p className="bad-text">Риск: {row.risk.reason}</p>}
      </div>
    </div>
  );
}

export function ShiftsPanel({ state }: { state: UiState }) {
  const bp = state.breaks;
  if (!bp) return <div className="empty">Разведение перерывов отключено в настройках расчёта</div>;
  return (
    <div data-testid="shifts">
      <p className="muted">
        Окна начала обеда сужаются внутри окон из входных данных так, чтобы обеды смен не совпадали и во время обеда одной
        смены другая работала. Длительность перерыва и смены не меняются.
      </p>
      <table className="tbl">
        <thead>
          <tr><th>Смена</th><th>Сотрудников</th><th>Обед, мин</th><th>Окно начала (вход)</th><th>Окно начала (план)</th><th>Обед может идти</th></tr>
        </thead>
        <tbody>
          {bp.groups.map((g) => (
            <tr key={g.key}>
              <td>{g.key}</td>
              <td>{g.staff} ({Object.entries(g.profiles).map(([k, n]) => `${k === "LTP3" ? "3ЛТП" : "инст."} ${n}`).join(", ")})</td>
              <td>{g.break_duration_min}</td>
              <td>{g.input_window}</td>
              <td className={g.effective_window !== g.input_window ? "changed" : ""}>{g.effective_window}</td>
              <td>{g.break_busy}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {bp.ok ? (
        <div className="ok-box">Непрерывная работа обеспечена: во время обеда каждой смены другая смена на работе.</div>
      ) : (
        <div className="warn-box">
          <b>Перекрытие смен обеспечено не полностью — ограничения не нарушаются, причины:</b>
          <ul>{bp.issues.map((i) => <li key={i}>{i}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
