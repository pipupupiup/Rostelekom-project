#!/bin/sh
# Первый запуск: при отсутствии OSM-выгрузки скачивает её (GH_OSM_URL), проверяет, что это
# настоящий .osm.pbf, затем строит граф (несколько минут, кеш в /data/graph-cache) и поднимает
# сервер на :8989. Вместо скачивания можно положить файл в папку osm вручную (из браузера).
set -eu
OSM_FILE="${GH_OSM_FILE:-/data/osm/ural-fed-district-latest.osm.pbf}"
OSM_URL="${GH_OSM_URL:-https://download.geofabrik.de/russia/ural-fed-district-latest.osm.pbf}"
MIN_BYTES=10000000

is_pbf() {  # настоящий PBF: больше 10 МБ и в заголовке есть блок OSMHeader
  [ -s "$1" ] && [ "$(wc -c < "$1")" -gt "$MIN_BYTES" ] && head -c 64 "$1" | grep -q OSMHeader
}

explain_bad() {
  echo "ОШИБКА: $1 — не карта OSM ($(wc -c < "$1") байт). Начало файла:"
  head -c 400 "$1"; echo
  echo "Скачайте файл вручную в браузере: $OSM_URL"
  echo "и положите его в папку osm рядом с compose.yaml под именем $(basename "$OSM_FILE"),"
  echo "затем: docker compose --profile graphhopper up -d graphhopper"
}

if [ -e "$OSM_FILE" ] && ! is_pbf "$OSM_FILE"; then
  explain_bad "$OSM_FILE"
  mv -f "$OSM_FILE" "$OSM_FILE.bad"
fi
if [ ! -e "$OSM_FILE" ]; then
  echo "OSM-выгрузки $OSM_FILE нет — скачиваем $OSM_URL"
  mkdir -p "$(dirname "$OSM_FILE")"
  if ! curl -fL --retry 3 -A "ADO-dispatcher/1.0 (graphhopper import)" -o "$OSM_FILE.part" "$OSM_URL"; then
    echo "ОШИБКА: не удалось скачать $OSM_URL"
    explain_bad "$OSM_FILE.part" 2>/dev/null || true
    sleep 600; exit 1
  fi
  if ! is_pbf "$OSM_FILE.part"; then
    explain_bad "$OSM_FILE.part"
    sleep 600; exit 1  # пауза: не скачивать заново каждые несколько секунд
  fi
  mv "$OSM_FILE.part" "$OSM_FILE"
fi
echo "Карта: $OSM_FILE ($(wc -c < "$OSM_FILE") байт). Строим граф — несколько минут при первом запуске."
exec java ${GH_JAVA_OPTS:--Xmx3g -Xms1g} \
  -Ddw.graphhopper.datareader.file="$OSM_FILE" \
  -jar /gh/graphhopper-web.jar server /gh/config.yml
