import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Stop, type UiState } from "../api";
import { hhmm } from "../util";

/**
 * Мобильная версия для сотрудника (`/ui/#tech/ST-001`): маршрут на день, текущий/следующий адрес с
 * навигацией, отметки «позвонил / прибыл / завершил». Рабочий цикл по адресу:
 * 📞 позвонил (за 30–60 мин) → 🧭 едет → 📍 прибыл → ✅ завершил → экран сразу показывает
 * следующий адрес маршрута, навигатор ведёт туда; после последнего — возврат в СЦ.
 * Отметки видны диспетчеру (карточка наряда, таблица). План они не меняют: в API кейса нет события
 * «визит завершён», статусы визитов в плане идут по модельному времени.
 * В демо-дне сотрудник может сообщить о задержке на объекте или поломке машины — это событие API
 * (VISIT_DELAYED / STAFF_UNAVAILABLE), план перестраивается. В реальном дне события присылает
 * оркестратор, и эти две кнопки недоступны.
 */
export function TechView({ initialStaff }: { initialStaff: string | null }) {
  const [state, setState] = useState<UiState | null>(null);
  const [staff, setStaff] = useState<string | null>(initialStaff);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await api.state();
      if (!s.busy) setState(s);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => {
    window.location.hash = staff ? `#tech/${staff}` : "#tech";
  }, [staff]);

  const route = state?.plan?.routes.find((r) => r.staff_id === staff);
  const visits = useMemo(() => (route?.stops ?? []).filter((s) => s.type === "VISIT" && s.order_id), [route]);
  const nowIso = state?.now ?? "";
  const marks = state?.marks ?? {};
  // следующий адрес — первый визит, который сотрудник ещё не отметил завершённым (и не завершён по плану)
  const next = visits.find((s) => s.status !== "DONE" && !(s.order_id && marks[s.order_id]?.done));
  const doneCount = visits.filter((s) => s.status === "DONE" || (s.order_id && marks[s.order_id]?.done)).length;
  const lastDone = [...visits].reverse().find((s) => s.order_id && marks[s.order_id]?.done);
  const endStop = route?.stops.find((s) => s.type === "DEPOT_END");
  const home = state?.service_centers?.find((c) => c.service_center_id === route?.service_center_id);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <div className="tech"><div className="empty">Загрузка…</div></div>;
  if (!state.loaded || !state.plan)
    return <div className="tech"><div className="empty">План на день ещё не построен. Обратитесь к диспетчеру.</div></div>;

  const me = state.staff?.find((s) => s.staff_id === staff);
  const o = next?.order_id ? state.orders?.[next.order_id] : undefined;
  const mk = next?.order_id ? marks[next.order_id] ?? {} : {};
  const demo = !!state.demo?.active;

  return (
    <div className="tech" data-testid="tech">
      <header className="tech-top">
        <b>ADO · сотрудник</b>
        <select value={staff ?? ""} onChange={(e) => setStaff(e.target.value || null)} aria-label="сотрудник">
          <option value="">выберите себя</option>
          {(state.staff ?? []).map((s) => (
            <option key={s.staff_id} value={s.staff_id}>{s.staff_id} · {s.display_name}</option>
          ))}
        </select>
        <span className="muted">
          {state.date} · {state.synthetic ? `время демо ${hhmm(nowIso)} · ДЕМО` : `сейчас ${hhmm(nowIso)}`}
        </span>
      </header>
      {msg && <div className="banner info">{msg}</div>}
      {!staff || !me ? (
        <div className="empty">Выберите себя в списке, чтобы увидеть маршрут на день</div>
      ) : (
        <>
          <div className="tech-me">
            {me.display_name} · смена {hhmm(me.shift_start)}–{hhmm(me.shift_end)} · обед {hhmm(me.break_window_planned.start)}–{hhmm(me.break_window_planned.end)} (начало)
            {route?.status === "UNAVAILABLE" && <div className="bad-text">Вы отмечены как недоступный — новых визитов нет</div>}
          </div>
          <div className="tech-progress">
            Выполнено {doneCount} из {visits.length}
            <span className="bar"><i style={{ width: `${visits.length ? (100 * doneCount) / visits.length : 0}%` }} /></span>
          </div>
          {next && o ? (
            <div className="tech-next" data-testid="tech-next">
              <div className="muted">
                {mk.arrive ? "Вы на адресе" : next.status === "IN_PROGRESS" ? "Сейчас" : "Следующий адрес"}
                {lastDone && !mk.arrive && lastDone.order_id ? ` · после ${state.orders?.[lastDone.order_id]?.address ?? lastDone.order_id}` : ""}
              </div>
              <div className="tech-addr">{o.address}</div>
              <div>
                {o.work_type_name} · слот {hhmm(o.slot.start)}–{hhmm(o.slot.end)} · по плану {hhmm(next.start)}–{hhmm(next.end)}
              </div>
              <div className="muted">
                АО: ONT {o.required_equipment.ONT} · роутер {o.required_equipment.ROUTER} · приставка {o.required_equipment.STB}
                {state.synthetic ? " · адрес синтетический (демо)" : ""}
              </div>
              <a className="btn nav" href={`https://yandex.ru/maps/?rtext=~${o.lat},${o.lon}&rtt=auto`} target="_blank" rel="noreferrer">
                🧭 Навигатор
              </a>
              <div className="tech-actions">
                <MarkBtn done={!!mk.call} label="📞 Позвонил абоненту" at={mk.call_wall} disabled={busy}
                  onClick={() => act(() => api.techMark(staff, o.order_id, "call", !!mk.call), mk.call ? "Отметка звонка снята" : "Звонок отмечен")} />
                <MarkBtn done={!!mk.arrive} label="📍 Прибыл" at={mk.arrive_wall} disabled={busy}
                  onClick={() => act(() => api.techMark(staff, o.order_id, "arrive", !!mk.arrive), mk.arrive ? "Отметка прибытия снята" : "Прибытие отмечено")} />
                <MarkBtn done={false} label="✅ Завершил" disabled={busy || !mk.arrive}
                  title={!mk.arrive ? "Сначала отметьте прибытие" : ""}
                  onClick={() =>
                    act(
                      () => api.techMark(staff, o.order_id, "done"),
                      `Заказ завершён. ${nextAfter(visits, next, state) ?? "Это был последний адрес — возвращайтесь в СЦ"}`,
                    )
                  } />
              </div>
              <div className="tech-actions">
                <button
                  disabled={busy || !demo || next.status !== "IN_PROGRESS"}
                  title={!demo ? "В реальном дне события присылает оркестратор" : next.status !== "IN_PROGRESS" ? "Только по начатому визиту" : ""}
                  onClick={() => act(() => api.techEvent({ kind: "delay", staff_id: staff, order_id: o.order_id, extra_min: 30 }), "Задержка +30 мин передана — план пересчитан")}
                >
                  ⏱ Задерживаюсь на 30 мин
                </button>
                <button
                  disabled={busy || !demo || route?.status === "UNAVAILABLE"}
                  title={!demo ? "В реальном дне события присылает оркестратор" : ""}
                  onClick={() => act(() => api.techEvent({ kind: "unavailable", staff_id: staff, reason: "VEHICLE_BREAKDOWN" }), "Диспетчер получил сообщение о поломке — наряды перераспределены")}
                >
                  🚗 Сломалась машина
                </button>
              </div>
              {!demo && <div className="muted">Кнопки задержки и поломки работают в демо-дне; в реальном дне эти события присылает оркестратор.</div>}
            </div>
          ) : (
            <div className="tech-next" data-testid="tech-next">
              <div className="tech-addr">На сегодня адресов больше нет 🎉</div>
              {endStop && home && (
                <>
                  <div>Возврат в {home.name} по плану к {hhmm(endStop.arrival)}</div>
                  <a className="btn nav" href={`https://yandex.ru/maps/?rtext=~${home.lat},${home.lon}&rtt=auto`} target="_blank" rel="noreferrer">
                    🧭 Навигатор в СЦ
                  </a>
                </>
              )}
            </div>
          )}
          <h4>Маршрут на день</h4>
          <ol className="tech-list">
            {(route?.stops ?? []).map((s: Stop) => (
              <li
                key={s.seq}
                className={
                  s.status === "DONE" || (s.order_id && marks[s.order_id]?.done)
                    ? "done"
                    : s.order_id && s.order_id === next?.order_id
                      ? "now"
                      : ""
                }
              >
                <span className="t">{hhmm(s.start)}</span>
                {s.type === "VISIT" && s.order_id ? (
                  <>
                    <b>{state.orders?.[s.order_id]?.address ?? s.location_id}</b>
                    <span className="muted"> · {state.orders?.[s.order_id]?.work_type_code} · до {hhmm(s.end)}</span>
                    {marks[s.order_id]?.done && (
                      <>
                        {" "}✅ {hhmm(marks[s.order_id].done_wall)}{" "}
                        <button className="undo" disabled={busy} title="Отменить отметку «завершил»"
                          onClick={() => act(() => api.techMark(staff, s.order_id!, "done", true), "Отметка «завершил» снята")}>
                          ↩
                        </button>
                      </>
                    )}
                  </>
                ) : (
                  <span className="muted">{s.type === "BREAK" ? "обед" : s.type === "RESTOCK" ? `пополнение АО в ${s.location_id}` : s.type === "DEPOT_START" ? `выезд из ${s.location_id}` : `возврат в ${s.location_id}`}</span>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

function MarkBtn(p: { done: boolean; label: string; at?: string; disabled: boolean; title?: string; onClick: () => void }) {
  return (
    <button className={p.done ? "marked" : ""} disabled={p.disabled} onClick={p.onClick} title={p.title ?? (p.done ? "нажмите ещё раз, чтобы снять отметку" : "")}>
      {p.label}{p.done && p.at ? ` · ${hhmm(p.at)}` : ""}
    </button>
  );
}

/** Текст «Следующий: адрес» после завершения визита `cur`. */
function nextAfter(visits: Stop[], cur: Stop, state: UiState): string | null {
  const i = visits.indexOf(cur);
  const n = visits.slice(i + 1).find((s) => s.status !== "DONE" && !(s.order_id && state.marks?.[s.order_id]?.done));
  if (!n?.order_id) return null;
  return `Следующий адрес: ${state.orders?.[n.order_id]?.address ?? n.location_id}, по плану в ${hhmm(n.start)}`;
}
