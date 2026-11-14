// ТОЛЬКО ДЛЯ ЛОКАЛЬНОЙ ВИЗУАЛЬНОЙ ПРОВЕРКИ БЕЗ СЕТИ (npm run build:offline).
// Минимальная замена подмножества API Leaflet, которое использует MapView: map, tileLayer,
// layerGroup, circleMarker, polyline, latLngBounds. Рисует SVG в проекции Меркатора без тайлов.
// В рабочую сборку не попадает: build.mjs подставляет её только с флагом --offline.

type LatLng = [number, number];
type Handler = () => void;

class Bounds {
  constructor(public pts: LatLng[]) {}
}

abstract class Layer {
  group: LayerGroup | null = null;
  tip = "";
  handlers: Handler[] = [];
  bindTooltip(t: string) { this.tip = t.replace(/<br>/g, "\n"); return this; }
  on(_e: string, h: Handler) { this.handlers.push(h); return this; }
  addTo(g: LayerGroup | OfflineMap) { (g instanceof OfflineMap ? g.root : g).add(this); return this; }
  abstract points(): LatLng[];
  abstract svg(project: (p: LatLng) => [number, number]): SVGElement;
}

class CircleMarker extends Layer {
  constructor(public ll: LatLng, public o: Record<string, unknown>) { super(); }
  points() { return [this.ll]; }
  svg(project: (p: LatLng) => [number, number]) {
    const [x, y] = project(this.ll);
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("cx", String(x)); c.setAttribute("cy", String(y));
    c.setAttribute("r", String(this.o.radius ?? 5));
    c.setAttribute("fill", String(this.o.fillColor ?? "#2563eb"));
    c.setAttribute("fill-opacity", String(this.o.fillOpacity ?? 0.9));
    c.setAttribute("stroke", String(this.o.color ?? "#fff"));
    c.setAttribute("stroke-width", String(this.o.weight ?? 1));
    return c;
  }
}

class Polyline extends Layer {
  constructor(public lls: LatLng[], public o: Record<string, unknown>) { super(); }
  points() { return this.lls; }
  svg(project: (p: LatLng) => [number, number]) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    p.setAttribute("points", this.lls.map((ll) => project(ll).join(",")).join(" "));
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", String(this.o.color ?? "#000"));
    p.setAttribute("stroke-width", String(this.o.weight ?? 2));
    p.setAttribute("stroke-opacity", String(this.o.opacity ?? 1));
    if (this.o.dashArray) p.setAttribute("stroke-dasharray", String(this.o.dashArray));
    return p;
  }
}

class LayerGroup {
  layers: Layer[] = [];
  map: OfflineMap | null = null;
  add(l: Layer) { this.layers.push(l); this.map?.schedule(); }
  clearLayers() { this.layers = []; this.map?.schedule(); }
  addTo(m: OfflineMap) { m.groups.push(this); this.map = m; m.schedule(); return this; }
}

class OfflineMap {
  svg: SVGSVGElement;
  groups: LayerGroup[] = [];
  root = new LayerGroup();
  view: Bounds | null = null;
  private pending = false;
  constructor(public el: HTMLElement) {
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.setAttribute("width", "100%"); this.svg.setAttribute("height", "100%");
    this.svg.style.background = "#e5e7eb";
    el.appendChild(this.svg);
    const note = document.createElement("div");
    note.textContent = "офлайн-заглушка карты (без тайлов) — только для локальной проверки";
    note.style.cssText = "position:absolute;left:6px;bottom:4px;font-size:11px;color:#6b7280;pointer-events:none";
    el.style.position = "relative";
    el.appendChild(note);
    this.root.addTo(this);
  }
  setView(c: LatLng, _z: number) { this.view = new Bounds([[c[0] - 0.08, c[1] - 0.15], [c[0] + 0.08, c[1] + 0.15]]); this.schedule(); return this; }
  fitBounds(b: Bounds, _o?: unknown) { this.view = b; this.schedule(); return this; }
  invalidateSize() { this.schedule(); }
  schedule() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => { this.pending = false; this.render(); });
  }
  render() {
    const w = this.el.clientWidth || 600, h = this.el.clientHeight || 400;
    const pts = this.view?.pts ?? [];
    const merc = (ll: LatLng): [number, number] => [ll[1], Math.log(Math.tan(Math.PI / 4 + (ll[0] * Math.PI) / 360)) * (180 / Math.PI)];
    const xs = pts.map((p) => merc(p)[0]), ys = pts.map((p) => merc(p)[1]);
    const [x0, x1, y0, y1] = xs.length ? [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)] : [60.4, 60.8, 70, 70.3];
    const pad = 20, k = Math.min((w - 2 * pad) / Math.max(x1 - x0, 1e-6), (h - 2 * pad) / Math.max(y1 - y0, 1e-6));
    const project = (ll: LatLng): [number, number] => {
      const [x, y] = merc(ll);
      return [pad + (x - x0) * k, h - pad - (y - y0) * k];
    };
    while (this.svg.firstChild) this.svg.removeChild(this.svg.firstChild);
    for (const g of this.groups) for (const l of g.layers) {
      const node = l.svg(project);
      if (l.tip) { const t = document.createElementNS("http://www.w3.org/2000/svg", "title"); t.textContent = l.tip; node.appendChild(t); }
      if (l.handlers.length) { node.style.cursor = "pointer"; node.addEventListener("click", () => l.handlers.forEach((f) => f())); }
      this.svg.appendChild(node);
    }
  }
}

const L = {
  map: (el: HTMLElement, _o?: unknown) => new OfflineMap(el),
  tileLayer: (_url: string, _o?: unknown) => ({ addTo: (m: OfflineMap) => m }),
  layerGroup: () => new LayerGroup(),
  circleMarker: (ll: LatLng, o: Record<string, unknown>) => new CircleMarker(ll, o),
  polyline: (lls: LatLng[], o: Record<string, unknown>) => new Polyline(lls, o),
  latLngBounds: (pts: LatLng[]) => new Bounds(pts),
};
export default L;
