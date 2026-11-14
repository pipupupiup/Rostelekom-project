import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { api, type RouteGeometry, type UiState } from "../api";
import { hhmm, type OrderRow, STATUS_COLOR, STATUS_LABEL, staffColor } from "../util";

interface Props {
  state: UiState;
  rows: OrderRow[];
  selectedStaff: string | null;
  selectedOrder: string | null;
  showAllRoutes: boolean;
  visibleOrders: Set<string>;
  onSelectStaff: (id: string | null) => void;
  onSelectOrder: (id: string | null) => void;
  children?: ReactNode;
}

/** Состояние дорожной геометрии для подписи на карте. */
export type RoadStatus =
  | { kind: "none" }
  | { kind: "loading" }
  | { kind: "ok"; source: string; partial: string | null }
  | { kind: "unavailable"; message: string };

/**
 * Карта: СЦ, наряды (цвет — статус, красная обводка — риск опоздания), маршруты сотрудников.
 *
 * Маршрут рисуется по дорогам, если сервер получил геометрию от GraphHopper или OSRM
 * (`/ui-api/route-geometry`, настройка `ADO_GRAPHHOPPER_URL` / `ADO_OSRM_URL`). Если движок не
 * настроен или недоступен, рисуется только тонкий пунктир
 * «порядок объезда» — прямые отрезки между остановками, и карта прямо пишет, что это не дороги.
 * Время в пути в плане всегда из матрицы на сервере; фронтенд его не пересчитывает.
 */
export function MapView(p: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layers = useRef<{ routes: L.LayerGroup; orders: L.LayerGroup; scs: L.LayerGroup } | null>(null);
  const fitted = useRef(false);
  const [geo, setGeo] = useState<RouteGeometry | null>(null);
  const [geoLoading, setGeoLoading] = useState(false);
  const [geoError, setGeoError] = useState<string | null>(null);

  const coords = useMemo(() => {
    const m = new Map<string, [number, number]>();
    for (const sc of p.state.service_centers ?? []) m.set(sc.service_center_id, [sc.lat, sc.lon]);
    for (const o of Object.values(p.state.orders ?? {})) m.set(o.building_id, [o.lat, o.lon]);
    return m;
  }, [p.state.service_centers, p.state.orders]);

  // какие маршруты показываем — те и запрашиваем у сервера
  const shown = useMemo(() => {
    const routes = p.state.plan?.routes ?? [];
    return routes
      .filter((r) => r.staff_id === p.selectedStaff || (p.showAllRoutes && !p.selectedStaff))
      .filter((r) => r.stops.length >= 2)
      .map((r) => r.staff_id);
  }, [p.state.plan, p.selectedStaff, p.showAllRoutes]);
  const shownKey = `${p.state.plan_version}|${shown.join(",")}`;

  useEffect(() => {
    if (!shown.length) return;
    let cancelled = false;
    setGeoLoading(true);
    const t = setTimeout(() => {
      api
        .routeGeometry(shown)
        .then((g) => {
          if (!cancelled) {
            setGeo(g);
            setGeoError(null);
          }
        })
        .catch((e) => !cancelled && setGeoError(e instanceof Error ? e.message : String(e)))
        .finally(() => !cancelled && setGeoLoading(false));
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [shownKey]);

  useEffect(() => {
    if (!el.current || map.current) return;
    const m = L.map(el.current, { zoomControl: true, preferCanvas: true }).setView([56.838, 60.597], 11);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: "© OpenStreetMap",
    }).addTo(m);
    layers.current = {
      routes: L.layerGroup().addTo(m),
      orders: L.layerGroup().addTo(m),
      scs: L.layerGroup().addTo(m),
    };
    map.current = m;
    const obs = new ResizeObserver(() => m.invalidateSize());
    obs.observe(el.current);
    return () => obs.disconnect();
  }, []);

  const geoFresh = geo && geo.plan_version === p.state.plan_version ? geo : null;
  const engine = geoFresh?.source?.split(" ")[0] ?? "сервис маршрутов";

  useEffect(() => {
    const m = map.current;
    const g = layers.current;
    if (!m || !g) return;
    g.routes.clearLayers();
    g.orders.clearLayers();
    g.scs.clearLayers();

    for (const sc of p.state.service_centers ?? []) {
      L.circleMarker([sc.lat, sc.lon], { radius: 9, color: "#111827", weight: 3, fillColor: "#fde047", fillOpacity: 1 })
        .bindTooltip(`${sc.service_center_id} · ${sc.name}`)
        .addTo(g.scs);
    }

    for (const r of p.state.plan?.routes ?? []) {
      if (!shown.includes(r.staff_id)) continue;
      const selected = r.staff_id === p.selectedStaff;
      const road = geoFresh?.routes[r.staff_id];
      if (road && road.coordinates.length >= 2) {
        const snap = road.max_snap_m > 300 ? `<br>⚠ точка остановки в ${road.max_snap_m} м от ближайшей дороги` : "";
        L.polyline(road.coordinates, {
          color: staffColor(r.staff_id),
          weight: selected ? 4 : 2,
          opacity: selected ? 0.95 : 0.5,
          dashArray: r.status === "UNAVAILABLE" ? "8 6" : undefined,
        })
          .bindTooltip(
            `${r.staff_id}: ${r.totals.visits} визитов<br>по дорогам (${engine}): ${road.distance_km} км, ≈${road.duration_min} мин` +
              `<br>в плане — время по матрице организаторов${snap}`,
          )
          .on("click", () => p.onSelectStaff(r.staff_id))
          .addTo(g.routes);
        continue;
      }
      // дорожной геометрии нет: только порядок объезда, явно помеченный как не дорога
      const pts = r.stops.map((s) => coords.get(s.location_id)).filter((x): x is [number, number] => !!x);
      if (pts.length < 2) continue;
      L.polyline(pts, { color: staffColor(r.staff_id), weight: selected ? 2 : 1, opacity: 0.7, dashArray: "2 6" })
        .bindTooltip(`${r.staff_id}: порядок объезда по прямой — НЕ дорожный маршрут`)
        .on("click", () => p.onSelectStaff(r.staff_id))
        .addTo(g.routes);
    }

    const orders = p.state.orders ?? {};
    for (const row of p.rows) {
      if (!p.visibleOrders.has(row.order_id)) continue;
      const o = orders[row.order_id];
      if (!o) continue;
      const dim = p.selectedStaff !== null && row.staff_id !== p.selectedStaff;
      const isSel = row.order_id === p.selectedOrder;
      L.circleMarker([o.lat, o.lon], {
        radius: isSel ? 9 : row.risk ? 7 : 5,
        color: row.risk ? "#b91c1c" : isSel ? "#111827" : "#ffffff",
        weight: row.risk || isSel ? 3 : 1,
        fillColor: STATUS_COLOR[row.status],
        fillOpacity: dim ? 0.25 : 0.9,
      })
        .bindTooltip(
          `${row.order_id} · ${o.work_type_code}<br>${STATUS_LABEL[row.status]}` +
            (row.staff_id ? ` · ${row.staff_id}` : "") +
            `<br>слот ${hhmm(o.slot.start)}–${hhmm(o.slot.end)}` +
            (row.stop ? ` · начало ${hhmm(row.stop.start)}` : ""),
        )
        .on("click", () => p.onSelectOrder(row.order_id))
        .addTo(g.orders);
    }

    if (!fitted.current && coords.size > 0) {
      m.fitBounds(L.latLngBounds([...coords.values()]), { padding: [20, 20] });
      fitted.current = true;
    }
  }, [p.state, p.rows, p.selectedStaff, p.selectedOrder, p.visibleOrders, coords, shown, geoFresh, engine]);

  useEffect(() => {
    const m = map.current;
    if (!m || !p.selectedStaff) return;
    const r = p.state.plan?.routes.find((x) => x.staff_id === p.selectedStaff);
    const pts = r?.stops.map((s) => coords.get(s.location_id)).filter((x): x is [number, number] => !!x) ?? [];
    if (pts.length > 1) m.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 14 });
  }, [p.selectedStaff]);

  let status: RoadStatus = { kind: "none" };
  if (shown.length) {
    if (geoError) status = { kind: "unavailable", message: `ошибка запроса геометрии: ${geoError}` };
    else if (!geoFresh) status = { kind: geoLoading ? "loading" : "none" };
    else if (Object.keys(geoFresh.routes).length)
      status = {
        kind: "ok",
        source: geoFresh.source ?? "сервис маршрутов",
        partial: Object.keys(geoFresh.errors).length ? geoFresh.message : null,
      };
    else if (Object.keys(geoFresh.errors).length || !geoFresh.configured)
      status = { kind: "unavailable", message: geoFresh.message ?? "дорожная геометрия недоступна" };
  }

  return (
    <div className="map-wrap">
      <div ref={el} className="map" data-testid="map" />
      <RoadNote status={status} />
      {p.children}
    </div>
  );
}

function RoadNote({ status }: { status: RoadStatus }) {
  if (status.kind === "none") return null;
  if (status.kind === "loading") return <div className="road-note">Загружаем маршруты по дорогам…</div>;
  if (status.kind === "ok")
    return (
      <div className="road-note ok" data-testid="road-note">
        Линии — по дорогам ({status.source}); время в плане — по матрице
        {status.partial ? <div className="bad-text">{status.partial}; для них показан пунктир «порядок объезда»</div> : null}
      </div>
    );
  return (
    <div className="road-note warn" data-testid="road-note">
      <b>Маршруты по дорогам недоступны:</b> {status.message}.
      <div>Пунктир — только порядок объезда по прямой, это не дорожный маршрут. Настройка — README, раздел «Карта и дороги».</div>
    </div>
  );
}
