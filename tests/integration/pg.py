"""Локальный PostgreSQL для интеграционных тестов ADO-15 (настоящий сервер, не SQLite)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

PGBIN = Path(os.environ.get("ADO_PGBIN", "/usr/lib/postgresql/16/bin"))


def available() -> bool:
    return (PGBIN / "initdb").exists() and shutil.which("su") is not None and os.geteuid() == 0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LocalPostgres:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ado_pg_"))
        self.port = _free_port()
        shutil.chown(self.dir, "postgres")
        self._su(f"{PGBIN}/initdb -D {self.dir}/data --auth=trust -U postgres -E UTF8 >/dev/null")
        self._su(
            f"{PGBIN}/pg_ctl -D {self.dir}/data -o '-p {self.port} -k {self.dir} -c listen_addresses=127.0.0.1 "
            f"-c fsync=off' -l {self.dir}/log -w start >/dev/null"
        )

    def _su(self, cmd: str) -> None:
        subprocess.run(["su", "postgres", "-s", "/bin/sh", "-c", cmd], check=True)

    def stop(self) -> None:
        self._su(f"{PGBIN}/pg_ctl -D {self.dir}/data stop -m immediate >/dev/null")
        shutil.rmtree(self.dir, ignore_errors=True)
