"""Демонстрация ПГП (задача 4) через публичный API — для защиты.

    PYTHONPATH=src python scripts/demo_pgp.py                 # сервис в процессе, ничего не трогает
    PYTHONPATH=src python scripts/demo_pgp.py --url http://localhost:8080   # против запущенного сервера

ВНИМАНИЕ: с `--url` скрипт загружает СИНТЕТИЧЕСКИЙ день через PUT /reference и PUT /days — по
спецификации это сбрасывает загруженный на сервере день, а в PostgreSQL заменяет историю этой
даты. Для защиты используйте отдельный стенд или режим без `--url`.

Что видно: группы `pgp_groups` в плане (узел, состав, `detected_at`), предупреждения
`PGP_DETECTED`, наряды группы не попадают в маршруты; затем поток событий — новые инциденты
выявляются по мере прихода нарядов (`NEW_ORDER`). В конце — сравнение с эталонной разметкой
синтетики (точность и полнота, как в оценке кейса).
"""

from __future__ import annotations

import argparse
import sys

from ado.api.service import DispatchService
from ado.config import Settings
from ado.orchestrator import ScenarioPlayer, http_client, service_client, synthetic_scenario
from ado.synthetic import generate_day


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--orders", type=int, default=400)
    a = ap.parse_args()

    syn = generate_day(n_orders=a.orders, seed=a.seed, pgp_incidents=3)
    date = syn.day_input["date"]
    req = http_client(a.url) if a.url else service_client(DispatchService(Settings()), date)
    for m, p, b in (
        ("PUT", "/api/v1/reference", syn.reference),
        ("PUT", "/api/v1/reference/travel-matrix", syn.matrix),
        ("PUT", f"/api/v1/days/{date}", syn.day_input),
    ):
        st, res = req(m, p, b)
        if st != 200:
            sys.exit(f"{m} {p}: HTTP {st} {res}")
    st, plan = req("POST", f"/api/v1/days/{date}/plan", {"time_limit_sec": 20})
    print(f"План v{plan['plan_version']}: групп ПГП {len(plan['pgp_groups'])}")
    show(plan)

    scenario = synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=a.seed)
    player = ScenarioPlayer(req, date, syn.day_input["now"], scenario, plan)
    seen = {g["group_id"] for g in plan["pgp_groups"]}
    while (r := player.step()) is not None:
        ev, res = r
        st, cur = req("GET", f"/api/v1/days/{date}/plan", None)
        new = [g for g in cur["pgp_groups"] if g["group_id"] not in seen]
        for g in new:
            print(
                f"\nСобытие {ev['event_id']} ({ev['type']}, {ev['timestamp'][11:16]}) → выявлена новая группа:"
            )
            show({"pgp_groups": [g], "alerts": cur["alerts"]})
            seen.add(g["group_id"])
    final = req("GET", f"/api/v1/days/{date}/plan", None)[1]
    found = [set(g["order_ids"]) for g in final["pgp_groups"]]
    truth = [set(x) for x in syn.pgp_incidents]
    hit = sum(1 for g in found if any(g <= t or len(g & t) >= 3 for t in truth))
    cov = sum(1 for t in truth if any(len(g & t) >= 3 for g in found))
    print(
        f"\nИтог: найдено групп {len(found)}, эталонных инцидентов {len(truth)}; "
        f"групп, совпавших с инцидентом: {hit}/{len(found)}; инцидентов найдено: {cov}/{len(truth)} (СИНТЕТИКА)"
    )
    in_routes = {s["order_id"] for r in final["routes"] for s in r["stops"] if s.get("order_id")}
    leaked = [o for g in found for o in g if o in in_routes]
    print(
        f"Наряды ПГП в маршрутах выездных бригад: {len(leaked)} (начатые до выявления остаются по правилам)"
    )


def show(plan: dict) -> None:
    for g in plan["pgp_groups"]:
        print(
            f"  {g['group_id']}: узел {g['access_node_id']}, выявлено {g['detected_at'][11:16]}, "
            f"нарядов {len(g['order_ids'])}: {', '.join(o[-4:] for o in g['order_ids'])} → {g['action']}"
        )
        al = [
            x
            for x in plan.get("alerts", [])
            if x.get("group_id") == g["group_id"] and x["type"] == "PGP_DETECTED"
        ]
        for x in al[:1]:
            print(f"    alert {x['alert_id']} PGP_DETECTED: {x['message']}")


if __name__ == "__main__":
    main()
