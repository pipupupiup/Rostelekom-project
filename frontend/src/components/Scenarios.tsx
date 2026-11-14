import { useState } from "react";
import { api, type ScenarioResult, type ScenarioRun, type UiState } from "../api";

/** Сценарный анализ (D40) и загрузка истории нарядов для прогноза длительности (D39). */
export function ScenariosPanel({ state, onHistoryLoaded }: { state: UiState; onHistoryLoaded: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [res, setRes] = useState<ScenarioResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [n, setN] = useState(3);
  const [hist, setHist] = useState<string | null>(null);

  const run = async (label: string, body: Record<string, unknown>) => {
    setBusy(label);
    setErr(null);
    try {
      setRes(await api.scenario(body));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy("Загружаем историю…");
    setErr(null);
    try {
      const data = JSON.parse(await file.text());
      const r = await api.history(Array.isArray(data) ? { orders: data, label: file.name } : data);
      setHist(`Загружено: ${r.orders} выполненных нарядов из ${file.name}`);
      onHistoryLoaded();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const d = state.durations;
  return (
    <div data-testid="scenarios">
      <h4>Что, если…</h4>
      <p className="muted">
        Начало текущего дня пересчитывается быстрым алгоритмом (regret-3 + локальный поиск) для базы и для изменённых условий.
        Активный план и история не меняются. Расчёт — 5–15 с.
      </p>
      <div className="scn-buttons">
        <span>
          на смену не вышли{" "}
          <input type="number" min={1} max={20} value={n} onChange={(e) => setN(Number(e.target.value) || 1)} style={{ width: 48 }} />{" "}
          сотрудника{" "}
          <button disabled={!!busy} data-testid="scn-staff" onClick={() => run("Считаем план без сотрудников…", { kind: "fewer_staff", n, samples: 3 })}>
            рассчитать
          </button>
        </span>
        <button disabled={!!busy} data-testid="scn-backpack" onClick={() => run("Считаем запас АО…", { kind: "backpack" })}>
          какой запас АО нужен, чтобы не заезжать в СЦ
        </button>
        <button disabled={!!busy} data-testid="scn-traffic" onClick={() => run("Считаем с пробками…", { kind: "traffic" })}>
          с учётом пробок по часам
        </button>
      </div>
      {busy && <div className="banner info">{busy}</div>}
      {err && <div className="warn-box">{err}</div>}
      {res && <ScenarioView r={res} />}

      <h4>История нарядов → прогноз длительности работ</h4>
      {d ? (
        <p>
          Модель: {d.orders} выполненных нарядов ({d.source}), оценка — P{Math.round(d.quantile * 100)} по сотруднику и типу работ.
          Используется в прогнозе рисков опоздания и в карточке наряда; сам план — по нормативу (требование API, H5).
        </p>
      ) : (
        <p className="muted">История не загружена — риски считаются по нормативу.</p>
      )}
      <label className="file">
        загрузить orders_train.json (поле fact) <input type="file" accept=".json,application/json" onChange={(e) => upload(e.target.files?.[0])} />
      </label>
      {hist && <div className="ok-box">{hist}</div>}
      {d && (
        <table className="tbl">
          <thead><tr><th>Тип работ</th><th>нарядов</th><th>медиана, мин</th><th>P{Math.round(d.quantile * 100)}, мин</th></tr></thead>
          <tbody>
            {Object.entries(d.by_type).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td>{v.n}</td><td>{v.p50_min}</td><td>{v[`p${Math.round(d.quantile * 100)}_min`]}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function row(label: string, b: ScenarioRun, s: ScenarioRun) {
  const keys: [keyof ScenarioRun, string][] = [
    ["success", "выполнено"],
    ["weighted_share", "доля успешных (взвеш.)"],
    ["unassigned", "нераспределено"],
    ["road_h", "в дороге, ч"],
    ["restocks", "заездов в СЦ"],
  ];
  return (
    <table className="tbl">
      <thead><tr><th>{label}</th><th>база</th><th>сценарий</th><th>Δ</th></tr></thead>
      <tbody>
        {keys.map(([k, name]) => {
          const bv = Number(b[k] ?? 0);
          const sv = Number(s[k] ?? (k === "road_h" ? s.travel_h : 0));
          const fmt = (x: number) => (k === "weighted_share" ? `${(x * 100).toFixed(1)} %` : String(Math.round(x * 10) / 10));
          return (
            <tr key={k}><td>{name}</td><td>{fmt(bv)}</td><td>{fmt(sv)}</td><td>{k === "weighted_share" ? `${((sv - bv) * 100).toFixed(1)} п.п.` : (Math.round((sv - bv) * 10) / 10).toString()}</td></tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ScenarioView({ r }: { r: ScenarioResult }) {
  return (
    <div className="card" data-testid="scn-result">
      <b>{r.conclusion}</b>
      {r.synthetic && <span className="tag warn">синтетика</span>}
      {row(`Нарядов на начало дня: ${r.orders}`, r.base, r.scenario)}
      {r.kind === "fewer_staff" && r.runs && (
        <div className="muted">
          Прогоны (случайный выбор отсутствующих): {r.runs.map((x) => `${x.absent?.join(", ")} → ${x.success}`).join("; ")}
        </div>
      )}
      {r.kind === "backpack" && r.recommended_start && (
        <div className="muted">
          Сейчас на старте: ONT {r.current_start?.ONT} / роутер {r.current_start?.ROUTER} / приставка {r.current_start?.STB};
          вместимость {r.capacity?.ONT}/{r.capacity?.ROUTER}/{r.capacity?.STB}. Рекомендуется: ONT {r.recommended_start.ONT} / роутер{" "}
          {r.recommended_start.ROUTER} / приставка {r.recommended_start.STB} (P90 потребности: {r.p90_need?.ONT}/{r.p90_need?.ROUTER}/{r.p90_need?.STB});
          маршрутов, которым не хватает текущего запаса: {r.routes_needing_more_than_current} из {r.routes}.
        </div>
      )}
      {r.kind === "traffic" && r.factors && (
        <div className="muted">Коэффициенты по часу отправления: {Object.entries(r.factors).map(([h, f]) => `${h}:00 × ${f}`).join(", ")}</div>
      )}
    </div>
  );
}
