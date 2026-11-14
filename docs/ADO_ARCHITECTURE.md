# ADO_ARCHITECTURE — структура и ответственность компонентов

## 1. Слои и пакеты

```
src/ado/
  domain/        сущности, перечисления, время, АО, события, контракты модулей (Protocol)
  data/          ADO-01  вход API/файлов → домен, проверка ссылочной целостности
  routing/       ADO-02  матрица времени в пути (единая матрица организаторов), опц. OSRM;
                 geometry.py — линии по дорогам для карты (GraphHopper, запасной — OSRM; D35, D37)
  optimization/  pgp (03) · eligibility (04) · priority (05) · insertion (06, 08) ·
                 simulator (07, единственный конструктор расписаний) · state (RouteState,
                 WorkingPlan) · initial (09) + baseline · evaluation + validator (11, независимый
                 оракул H1–H10) · budget (14) · improvement (10) · risk (12) ·
                 breaks (21, разведение обедов смен, D36)
  synthetic.py   СИНТЕТИЧЕСКИЙ генератор данных в формате организаторов (тесты/замеры)
  orchestrator.py эмулятор оркестратора: сценарий дня с триггерами по нашему плану (ADO-14/17)
  events/        ADO-13  DaySession: применение событий, заморозка, ремонт, LATE_RISK, версии
  persistence/   ADO-15  журнал версий в PostgreSQL (вне горячего пути)
  api/           ADO-16  service.py (операции → код+тело), serialize.py (схемы), app.py (ASGI)
benchmarks/      ADO-17  сравнение Baseline / ADO Basic / ADO Full
tests/           pytest
spec/            api_spec.yaml — источник истины
```

Зависимости направлены только внутрь: `api → events/optimization/data/persistence → domain`.
`domain` ни от чего не зависит.

## 2. Состояние сервиса (в памяти)

```
ServiceState
  reference: ReferenceData | None          PUT /reference (сбрасывает всё остальное)
  travel:    TravelTimeProvider | None     PUT /reference/travel-matrix
  day:       DayState | None               один активный день (спецификация допускает)
DayState
  instance:  DayInstance                   наряды дня + now
  plan:      Plan                          текущая версия
  events:    {event_id → результат}        идемпотентность
  last_ts:   Seconds                       контроль порядка событий (409)
```

Запросы обрабатываются последовательно (оркестратор шлёт их по одному; `--workers 1`).
Запись версии в PostgreSQL — после формирования ответа (D5).

## 3. Поток первоначального планирования (`POST /days/{date}/plan`, ≤ 300 с)

```
DayInstance ─► ADO-03 detect_pgp ─► ПГП-наряды в pgp_groups (+ алерты PGP_DETECTED)
            ─► ADO-04 eligibility ─► нет допуска → unassigned(NO_QUALIFIED_STAFF)
            ─► ADO-05 prioritize
            ─► ADO-09 regret insertion  ── использует ADO-06 кандидаты, ADO-08 вставки,
            │                              ADO-07 симуляцию (единственная проверка H1–H8)
            ─► ADO-10 local search + ALNS (anytime, бюджет ADO-14)
            ─► ADO-11 validate (H1–H10) + evaluate
            ─► ADO-12 risks → алерты
            ─► причины для оставшихся unassigned (диагностика по ADO-07 Violation.unassigned_hint)
            ─► Plan v1
```

Бюджет: 300 с − резерв 15 с на валидацию и сериализацию.

## 4. Поток события (`POST /days/{date}/events`, ≤ 10 с)

```
event ─► проверки: порядок timestamp (409), дубликат (200, duplicate), ссылки (422)
      ─► now := timestamp; заморозка начатых остановок (H10)
      ─► применение: NEW_ORDER → ПГП-проверка → вставка; ORDER_CANCELLED → снять визит;
         VISIT_DELAYED/ORDER_TRANSFERRED → сдвиг end и хвоста; STAFF_UNAVAILABLE → снять хвост,
         перераспределить; TIME_TICK → только прогноз
      ─► пересчёт хвостов затронутых маршрутов (ADO-07)
      ─► проблемные наряды → уровни 1–4 расширения поиска (ADO-13)
      ─► LATE_RISK для каждого переназначенного/снятого наряда (D14)
      ─► validate; при провале — последняя корректная версия + объяснение
      ─► plan_version + 1
```

Бюджет: 10 с − резерв 2 с.

## 5. Производительность
- Матрица: `numpy` int32, индексы точек — словарь `id → int`; доступ O(1). ~1265² × 2 × 4 Б ≈ 13 МБ.
- Симуляция маршрута — O(длина маршрута), с инкрементальным пересчётом от точки вставки.
- Во внутренних циклах нет I/O, логирования на уровне INFO и аллокаций pydantic-моделей.
- Одно ядро; многопоточность — только если замеры ADO-17 покажут необходимость.

## 6. Развёртывание
`docker compose up --build`: сервис `app` (uvicorn, порт 8080, keep-alive 400 с) и `db`
(PostgreSQL 16). `GET /api/v1/health` отвечает без ожидания тяжёлой инициализации, лимит — 120 с.
