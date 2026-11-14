"""Замер ADO-09 vs baseline на СИНТЕТИЧЕСКОМ дне (не данные кейса).

python benchmarks/initial_plan.py [n_orders] [seed]
"""

from __future__ import annotations

import sys
import time

from ado.data.prepare import prepare_day, prepare_reference
from ado.optimization.baseline import build_baseline_plan
from ado.optimization.improvement import ImproveConfig, RouteImprover
from ado.optimization.initial import build_daily_plan
from ado.routing.matrix import MatrixTravelProvider, TravelMatrix
from ado.synthetic import generate_day


def main(n: int = 400, seed: int = 7, alns_sec: int = 60) -> None:
    t = time.perf_counter()
    syn = generate_day(n_orders=n, seed=seed)
    ref = prepare_reference(syn.reference)
    day = prepare_day(syn.day_input, ref)
    travel = MatrixTravelProvider(TravelMatrix.from_api(syn.matrix))
    print(
        f"данные: {len(day.orders)} нарядов на 08:30 (+{len(syn.late_orders)} позже), "
        f"{len(ref.employees)} сотрудников, матрица {len(travel.matrix)} точек; {time.perf_counter() - t:.1f} с"
    )
    for name, fn in (
        ("baseline", lambda: build_baseline_plan(day, travel)),
        ("ADO regret", lambda: build_daily_plan(day, travel)),
        (
            "ADO regret+LS",
            lambda: build_daily_plan(day, travel, improver=RouteImprover(ImproveConfig(use_alns=False))),
        ),
        (
            f"ADO regret+LS+ALNS {alns_sec}s",
            lambda: build_daily_plan(
                day, travel, improver=RouteImprover(ImproveConfig(time_limit_sec=alns_sec))
            ),
        ),
    ):
        r = fn()
        m = r.metrics
        print(f"\n== {name}: {r.stats['total_sec']:.2f} с, нарушений H: {len(r.issues)}")
        print(
            f"   успешно {m.success_count}/{m.orders_total} (взвеш. {m.weighted_success}/{m.weighted_total}, "
            f"{m.weighted_success_share:.1%}); ПГП {m.pgp_orders}; нераспр. {m.unassigned_count} "
            f"{m.unassigned_by_reason}"
        )
        print(
            f"   дорога {m.travel_sec_total / 3600:.1f} ч, CV загрузки {m.utilization_cv:.3f}, "
            f"переносов {m.reschedules}"
        )
        print("   этапы:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.stats.items()})
        for i in r.issues[:5]:
            print("   !", i)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:4]))
