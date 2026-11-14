"""ADO-19: репетиция развёртывания без Docker — «с чистого окружения».

1. чистый PostgreSQL 16 (локальный кластер) → 2. миграции (`python -m ado.persistence.migrate`;
в Docker — `alembic upgrade head`, тот же DDL) → 3. сервер `uvicorn` как в контейнере →
4. время до `200 /api/v1/health` → 5. `/ui/` → 6. полный день по HTTP (оркестратор, СИНТЕТИКА) →
7. версии в БД = версиям сервиса → отчёт benchmarks/results/rehearsal.json.

PYTHONPATH=src python scripts/rehearsal.py [n_orders]
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.integration.pg import LocalPostgres  # noqa: E402

from ado.orchestrator import http_client, run_day, synthetic_scenario  # noqa: E402
from ado.persistence.db import PgWireDatabase  # noqa: E402
from ado.persistence.pgwire import PgWireConnection  # noqa: E402
from ado.persistence.store import PlanStore  # noqa: E402
from ado.synthetic import generate_day  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main(n_orders: int = 400) -> dict:
    report: dict = {"data": "SYNTHETIC"}
    pg = LocalPostgres()
    server = None
    try:
        c = PgWireConnection(port=pg.port)
        c.query("CREATE DATABASE ado")
        c.close()
        env = dict(
            os.environ,
            PYTHONPATH=str(ROOT / "src"),
            ADO_DATABASE_URL=f"postgresql://postgres@127.0.0.1:{pg.port}/ado",
            ADO_TEAM="ADO-rehearsal",
        )
        t = time.perf_counter()
        subprocess.run([sys.executable, "-m", "ado.persistence.migrate"], env=env, check=True)
        report["migrate_sec"] = round(time.perf_counter() - t, 2)
        if not (ROOT / "frontend/dist/index.html").exists():
            subprocess.run(["node", "build.mjs", "--offline"], cwd=ROOT / "frontend", check=True)
        port = free_port()
        t = time.perf_counter()
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ado.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-keep-alive",
                "400",
                "--workers",
                "1",
                "--log-level",
                "warning",
            ],
            env=env,
        )
        base = f"http://127.0.0.1:{port}"
        while True:
            try:
                with urllib.request.urlopen(base + "/api/v1/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
            if time.perf_counter() - t > 120:
                raise RuntimeError("health не ответил за 120 с")
        report["health_ready_sec"] = round(time.perf_counter() - t, 2)
        with urllib.request.urlopen(base + "/ui/", timeout=5) as r:
            report["ui_status"] = r.status

        syn = generate_day(n_orders=n_orders, seed=7)
        scenario = synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=7)
        run = run_day(
            http_client(base), syn.reference, syn.matrix, syn.day_input, scenario, time_limit_sec=300
        )
        calls = {c.name: c for c in run.calls}
        ev = [c.seconds for c in run.calls if c.name.startswith("event")]
        report.update(
            http_errors=run.errors,
            put_reference_sec=round(calls["reference"].seconds, 2),
            put_matrix_sec=round(calls["matrix"].seconds, 2),
            put_day_sec=round(calls["day"].seconds, 2),
            plan_sec=round(calls["plan"].seconds, 2),
            events=len(ev),
            event_http_sec_max=round(max(ev), 3),
            final_version=run.final_plan["plan_version"],
        )
        with urllib.request.urlopen(base + "/ui-api/state", timeout=10) as r:
            st = json.load(r)
        report["persistence"] = st["persistence"]
        store = PlanStore(PgWireDatabase(port=pg.port, database="ado"))
        versions = [v["version"] for v in store.versions(syn.day_input["date"])]
        report["db_versions"] = len(versions)
        report["db_matches_service"] = versions == list(range(1, report["final_version"] + 1))
        report["db_events"] = len({e["event_id"] for e in run.events_sent})
        report["db_audit_rows"] = len(store.audit(syn.day_input["date"]))
    finally:
        if server is not None:
            server.terminate()
            server.wait(10)
        pg.stop()
    out = ROOT / "benchmarks/results/rehearsal.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(main(int(sys.argv[1]) if len(sys.argv) > 1 else 400), ensure_ascii=False, indent=1))
