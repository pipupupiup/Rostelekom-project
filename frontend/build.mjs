// Сборка интерфейса: esbuild → dist/. `--offline` — ТОЛЬКО для локальной визуальной проверки без
// доступа к npm-реестру: `leaflet` подменяется заглушкой dev/leaflet-offline.ts (рисует точки и линии
// без тайлов карты). Рабочая сборка (Docker) всегда использует настоящий Leaflet.
import { build } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const offline = process.argv.includes("--offline");
const dist = path.join(here, "dist");
rmSync(dist, { recursive: true, force: true });
mkdirSync(path.join(dist, "assets"), { recursive: true });

const alias = offline
  ? { leaflet: path.join(here, "dev/leaflet-offline.ts"), "leaflet/dist/leaflet.css": path.join(here, "dev/empty.css") }
  : {};

await build({
  entryPoints: [path.join(here, "src/main.tsx")],
  bundle: true,
  outdir: path.join(dist, "assets"),
  entryNames: "app",
  minify: !offline,
  sourcemap: offline,
  target: ["es2020"],
  jsx: "automatic",
  loader: { ".png": "dataurl", ".svg": "dataurl" },
  alias,
  nodePaths: process.env.NODE_PATH ? process.env.NODE_PATH.split(path.delimiter) : [],
  define: { "process.env.NODE_ENV": JSON.stringify(offline ? "development" : "production"), __OFFLINE__: String(offline) },
  logLevel: "info",
});
cpSync(path.join(here, "index.html"), path.join(dist, "index.html"));
console.log(offline ? "СБОРКА OFFLINE (заглушка карты)" : "сборка готова");
