"""ADO-17: Baseline / Basic / Full на одинаковых СИНТЕТИЧЕСКИХ днях (не данные кейса).

PYTHONPATH=src python benchmarks/compare.py [n_orders] [alns_sec] [seed ...]
Результаты: benchmarks/results/compare.json (всё, включая качество после каждого события),
compare_runs.csv (строка на день×вариант), compare_trace.csv (после каждого события),
compare_summary.md (таблица для презентации).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from ado.benchmark import compare, summarize

OUT = Path(__file__).parent / "results"
LABEL = {"baseline": "Baseline", "basic": "ADO Basic", "full": "ADO Full"}


def main(n_orders: int = 400, alns_sec: int = 60, seeds: list[int] | None = None) -> None:
    seeds = seeds or [7, 11, 23]
    rows = compare(seeds, n_orders, alns_sec)
    summary = summarize(rows)
    OUT.mkdir(exist_ok=True)
    (OUT / "compare.json").write_text(
        json.dumps(
            {
                "data": "SYNTHETIC",
                "seeds": seeds,
                "n_orders": n_orders,
                "alns_sec": alns_sec,
                "summary": summary,
                "runs": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    flat = [k for k in rows[0] if k not in ("trace", "errors", "violations_sample")]
    with (OUT / "compare_runs.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=flat, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with (OUT / "compare_trace.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "seed",
                "variant",
                "version",
                "event",
                "as_of_sec",
                "weighted",
                "weighted_total",
                "unassigned",
                "travel_sec",
            ]
        )
        for r in rows:
            for t in r["trace"]:
                w.writerow(
                    [
                        r["seed"],
                        r["variant"],
                        t["version"],
                        t["event"],
                        t["as_of"],
                        t["weighted"],
                        t["weighted_total"],
                        t["unassigned"],
                        t["travel_sec"],
                    ]
                )
    lines = [
        f"# Сравнение вариантов (СИНТЕТИКА: seeds {seeds}, {n_orders} нарядов, ALNS {alns_sec} с)",
        "",
        "| Показатель | " + " | ".join(LABEL[v] for v in summary) + " |",
        "|---|" + "---|" * len(summary),
    ]
    names = {
        "plan_sec": "время плана, с",
        "event_sec_max": "макс. ответ на событие, с",
        "initial_weighted_share": "доля успешных (взвеш.) на 08:30",
        "final_weighted_share": "доля успешных (взвеш.) в конце дня",
        "final_travel_h": "время в пути, ч",
        "final_cv": "CV загрузки",
        "final_unassigned": "нераспределено",
        "reschedules": "переносы",
        "late_risk_timely_share": "своевременные LATE_RISK (из возможных)",
        "pgp_precision": "ПГП точность",
        "pgp_recall": "ПГП полнота",
        "violations_algorithm": "нарушений по вине алгоритма",
        "violations_event": "нарушений, вызванных событием",
    }
    for k, label in names.items():
        lines.append(f"| {label} | " + " | ".join(str(summary[v][k]) for v in summary) + " |")
    (OUT / "compare_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:]]
    main(a[0] if a else 400, a[1] if len(a) > 1 else 60, a[2:] or None)
