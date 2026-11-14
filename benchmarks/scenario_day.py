"""ADO-14/17: полный день по сценарию оркестратора, вариант ADO Full, СИНТЕТИКА (не данные кейса).

PYTHONPATH=src python benchmarks/scenario_day.py [seed] [n_orders] [alns_sec]
Результат: benchmarks/results/scenario_<seed>.json. Сравнение вариантов — benchmarks/compare.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ado.benchmark import run_variant
from ado.orchestrator import synthetic_scenario
from ado.synthetic import generate_day

OUT = Path(__file__).parent / "results"


def main(seed: int = 7, n_orders: int = 400, alns_sec: int = 60) -> dict:
    syn = generate_day(n_orders=n_orders, seed=seed)
    scenario = synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=seed)
    report = run_variant("full", syn, scenario, alns_sec)
    report["data"] = "SYNTHETIC"
    report["seed"] = seed
    OUT.mkdir(exist_ok=True)
    (OUT / f"scenario_{seed}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {k: v for k, v in report.items() if k != "trace"}


if __name__ == "__main__":
    print(json.dumps(main(*[int(a) for a in sys.argv[1:4]]), ensure_ascii=False, indent=1))
