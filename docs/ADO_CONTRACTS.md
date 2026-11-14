# ADO_CONTRACTS — общие модели и интерфейсы

Код: `src/ado/domain/`. Изменение контрактов — только вместе с зависимыми модулями, тестами и
этим документом.

## 1. Единицы
| Величина | Тип | Единица |
|---|---|---|
| момент времени | `Seconds` (`int`) | секунды от местной полуночи дня плана (`TimeBase`) |
| длительность | `Seconds` | секунды; `*_min` из справочников переводятся через `minutes()` |
| расстояние | `int` | метры |
| время суток (`HH:MM`) | `Seconds` | `parse_local_time()` |

## 2. Соответствие домена схемам спецификации
| Домен (`ado.domain.models`) | Схема `api_spec.yaml` | Отличия |
|---|---|---|
| `ServiceCenter` | `ServiceCenter` | `opening_hours` → `opening: TimeWindow`; `restock_duration_min` → `restock_duration` (с) |
| `Employee` | `Staff` | `profiles`/`technologies` — `frozenset` |
| `Shift` | `Shift` | `break_duration_min` → `break_duration` (с) |
| `Building` | `Building` | — |
| `AccessNode` | `AccessNode` | `buildings[]` → `building_ids` (сами дома — в `ReferenceData.buildings`) |
| `WorkType` | `WorkType` (строка CSV) | `duration_min`, `max_start_delay_min` → секунды |
| `Order` | `Order` | моменты в секундах; `fact` игнорируется |
| `DayInstance` | `DayInput` + справочники | — |
| `RouteStop` | `Stop` | `seq` вычисляется (D13); `status` — `status_at(now)` |
| `Route` | `Route` | `totals` вычисляются при сериализации |
| `Plan` | `Plan` | `unassigned`/`cancelled` — словари по `order_id`; `summary` вычисляется |
| `UnassignedReason` | `UnassignedOrder` | `reason_code` → `code`, `reason_text` → `text` |
| `PgpGroup`, `CancelledOrder`, `Alert` | одноимённые | моменты в секундах |
| `EquipmentInventory` | `Equipment` | `from_api()` / `to_api()` |
| `events.*Event` | `*Event` | `type` — свойство класса |

Совпадение перечислений и покрытие обязательных полей проверяет `tests/unit/test_spec_enums.py`.

## 3. Результаты модулей
- `Violation(code: ConstraintCode, message, stop_index, order_id, unassigned_hint)` — нарушение
  H1–H10/структуры; `unassigned_hint` связывает нарушение с `UnassignedReasonCode`.
- `SimulationResult(feasible, stops, violations, travel_sec, distance_m, work_sec, wait_sec)`.
- `InsertionOption(order_id, staff_id, position, delta_travel_sec, with_restock, simulation, score)` —
  меньше `score` — лучше.
- `PlanMetrics(...)`, сравнение по `key()` (D7): больше — лучше.
- `RiskItem(order_id, staff_id, forecast_start, latest_start)`, `slack = latest_start − forecast_start`.

## 4. Интерфейсы (`ado.domain.contracts`)
| Модуль | Protocol | Ключевой метод |
|---|---|---|
| ADO-01 | `DataPreparer` | `prepare_reference(raw)`, `prepare_day(raw, reference)` |
| ADO-02 | `TravelTimeProvider` | `travel_sec(a, b)`, `distance_m(a, b)`, `has_point(id)` |
| ADO-03 | `PgpDetector` | `detect(orders, existing, started_order_ids, now, reference)` |
| ADO-04 | `EligibilityFilter` | `eligible_staff(order, employees)`, `explain_ineligible(...)` |
| ADO-05 | `OrderPrioritizer` | `prioritize(orders, eligibility, now)` |
| ADO-06 | `CandidateSearch` | `candidates(order, eligible, plan, now)` |
| ADO-07 | `RouteSimulator` | `simulate(employee, draft, day, travel)` |
| ADO-08 | `InsertionFinder` | `insertions(order, staff_ids, plan, day, now)` |
| ADO-09 | `InitialOptimizer` | `build(day, budget)` |
| ADO-10 | `RouteImprover` | `improve(plan, day, now, budget)` |
| ADO-11 | `PlanEvaluator`, `PlanValidator` | `evaluate(...)`, `validate(plan, previous, day)` |
| ADO-12 | `DelayPredictor` | `risks(plan, day, now)` |
| ADO-13 | `Reoptimizer` | `apply(event, plan, day, budget) → ReplanResult` |
| ADO-14 | `TimeBudgetLike` | `remaining()`, `expired()`, `sub_budget(s)` — реализован `TimeBudget` |
| ADO-15 | `PlanRepository` | `save_version(plan, expected_previous_version)`, `current(date)` |

## 5. Вход и выход симуляции (ADO-07, зафиксировано)

`RouteDraft(staff_id, items, frozen_prefix, earliest_departure, resume_from_depot_at, blocked,
not_before, end_at_depot)`; `items` — кортеж `VisitItem(order_id)` / `RestockItem(service_center_id)`.
`BREAK` в `items` не входит — его размещает симулятор (D12, D17).

`simulate_route(employee, draft, day, travel) → SimulationResult(feasible, stops, violations,
travel_sec, distance_m, work_sec, wait_sec, deadline_misses)`. Первое нарушение — `.violation`,
с `unassigned_hint` для причины нераспределения.

`TravelTimeProvider.travel_sec()` возвращает `None` для недостижимой пары; неизвестная точка —
`KeyError` (`UnknownPointError`).

## 6. Рабочее состояние оптимизатора

`RouteState` (маршрут + кеши предпроверки) и `WorkingPlan` (все маршруты, unassigned, ПГП,
отменённые, алерты, объяснения; `to_plan()` → доменный `Plan`) — `ado.optimization.state`.
Все оптимизаторы (ADO-09, 10, 13) работают с `WorkingPlan` и записывают в `RouteState` только
расписания, подтверждённые симулятором (`set_schedule` отклоняет недопустимые).

## 7. Подготовка данных (ADO-01)

`prepare_reference(raw) → ReferenceData`, `prepare_day(raw, reference) → DayInstance`,
`prepare_event(raw, day) → AnyEvent`, `prepare_scenario(raw)`, `prepare_data(dir) → Dataset`.
Варианты `*_report` возвращают `Prepared(value, warnings)`. Ошибки: `InputFormatError` (→ 400),
`BusinessValidationError` (→ 422), обе с `issues: [Issue(field, message)]`.

## 8. Сессия дня и API (ADO-13, ADO-16)

`DaySession(day, travel, ReoptConfig)`: `build_plan(time_limit_sec) → Plan` (v1; повтор после
событий — `PlanAlreadyReplanned`, 409), `apply(event) → EventOutcome`, `current_plan()`.
Ошибки: `PlanNotFound` (404), `EventOutOfOrder` / `PlanAlreadyReplanned` (409),
`EventRejected` (422: `UNKNOWN_ORDER`, `UNKNOWN_STAFF`, `DUPLICATE_ORDER`, `STATE_CONFLICT`…).
`snapshots` — все версии с фактическими окончаниями визитов, `not_before` и множеством
известных нарядов — для независимой проверки каждой версии.

`DispatchService` — по методу на операцию спецификации, возвращает `(status, body)`; тела строго
по схемам (`ado.api.serialize`). `ado.api.app.create_app()` — ASGI-приложение на `/api/v1`.

`ado.orchestrator.run_day(request, reference, matrix, day_input, scenario)` — сценарий оценки
одного дня с разворачиванием триггеров по текущему плану.

## 9. Служебный API интерфейса (вне спецификации, ADO-18)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/ui-api/state?since_version=N` | состояние экрана; `breaks` — отчёт D36; `staff[].break_window` (вход) и `break_window_planned` |
| POST | `/ui-api/demo/start` | синтетический демо-день в отдельном сервисе без БД (D34) |
| POST | `/ui-api/demo/step` | следующее событие демо |
| POST | `/ui-api/demo/exit` | выйти из демо → `{exited, showing: api_day\|start, date}`; ничего не удаляет |
| GET | `/ui-api/route-geometry?staff_id=A,B` | линии по дорогам (GraphHopper или OSRM, D35/D37), `source` — «GraphHopper http://…»: `{available, configured, source, message, routes{sid: {coordinates[[lat,lon]], distance_km, duration_min, max_snap_m}}, errors{sid}}` |

`DaySession.break_plan: BreakPlan | None` — группы смен, входные и суженные окна, `issues`.

Переменные: `ADO_GRAPHHOPPER_URL`, `ADO_GRAPHHOPPER_KEY`, `ADO_GRAPHHOPPER_PROFILE` (по умолчанию `car`), `ADO_OSRM_URL`.

| Метод | Путь | Назначение (доп. задания) |
|---|---|---|
| POST | `/ui-api/history` | история нарядов (`orders_train.json` или `{orders, label}`) → модель длительностей (D39) |
| POST | `/ui-api/scenario` | `{kind: fewer_staff\|backpack\|traffic, n?, samples?, time_limit_sec?}` (D40) |
| POST | `/ui-api/tech/mark` | `{staff_id, order_id, kind: call\|arrive\|done}` — отметка сотрудника (D42) |
| POST | `/ui-api/tech/event` | `{kind: delay\|unavailable, staff_id, order_id?, extra_min?}` — событие API, только демо (D42) |

`/ui-api/state` дополнительно: `durations`, `duration_forecast`, `traffic`, `joint_orders`, `marks`.
Переменные: `ADO_TRAFFIC`, `ADO_HISTORY_FILE`.
