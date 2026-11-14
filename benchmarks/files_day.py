"""Полный день по КАТАЛОГУ ФАЙЛОВ (формат организаторов): справочники, матрица, наряды, сценарий.

    PYTHONPATH=src python benchmarks/files_day.py <каталог> [alns_sec] [варианты через запятую]

Файлы читаются тем же `prepare_data`, что и для данных организаторов; день и сценарий проигрываются
через оркестратор (`run_day`) против сервиса в процессе. Для ПГП эталона нет — точность/полнота
не считаются, выводятся найденные группы.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from ado.api.service import DispatchService
from ado.benchmark import analyze, variant_config
from ado.config import Settings
from ado.data.prepare import _read_json, prepare_data
from ado.orchestrator import run_day, service_client


def main() -> None:
    data_dir = Path(sys.argv[1])
    alns_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    variants = sys.argv[3].split(",") if len(sys.argv) > 3 else ["baseline", "basic", "full"]
    ds = prepare_data(data_dir)
    print(
        f"справочник: СЦ {len(ds.reference.service_centers)}, сотрудников {len(ds.reference.employees)}, "
        f"домов {len(ds.reference.buildings)}; дни: {sorted(ds.days)}; предупреждений {len(ds.warnings)}"
    )
    for w in ds.warnings[:10]:
        print("  !", w.field, w.message)
    scenario = _read_json(data_dir / "events_example.json")
    date = scenario["date"]
    now = scenario["now"]
    day = ds.days[date]
    day_input, late = day.day_input(now=__import__("datetime").datetime.fromisoformat(now).time())
    new_ids = {e["order"]["order_id"] for e in scenario["events"] if e["type"] == "NEW_ORDER"}
    print(
        f"день {date}: на {now[11:16]} {len(day_input['orders'])} нарядов, позже {len(late)} "
        f"(в сценарии NEW_ORDER: {len(new_ids)}, совпадают: {new_ids == {o['order_id'] for o in late}}); "
        f"событий {len(scenario['events'])}, триггеров {len(scenario.get('triggers', []))}"
    )
    stub = SimpleNamespace(pgp_incidents=[])
    rows = []
    for v in variants:
        svc = DispatchService(Settings(team=f"files-{v}"), variant_config(v, alns_sec))
        t0 = time.perf_counter()
        run = run_day(service_client(svc, date), ds.reference_raw, ds.travel_matrix_raw, day_input, scenario)
        res = analyze(svc.session, run, stub)
        res.pop("pgp_precision"), res.pop("pgp_recall")
        final = svc.session.plan
        res["pgp_groups"] = [sorted(g.order_ids) for g in final.pgp_groups]
        res["skipped_triggers"] = run.skipped_triggers
        res["wall_sec"] = round(time.perf_counter() - t0, 1)
        res["variant"] = v
        trace = res.pop("trace")
        res["final_version"] = trace[-1]["version"]
        rows.append(res)
        print(json.dumps({k: res[k] for k in res if k != "violations_sample"}, ensure_ascii=False))
        if res["violations_sample"]:
            print("  нарушения:", res["violations_sample"])
    out = Path(__file__).parent / "results" / "files_day.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out)


if __name__ == "__main__":
    main()
