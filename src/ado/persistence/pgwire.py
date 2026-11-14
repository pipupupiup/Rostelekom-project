"""Минимальный клиент протокола PostgreSQL v3 на чистом Python (ADO-15).

Назначение: интеграционные тесты транзакционной логики на **настоящем** PostgreSQL в окружении,
где нет psycopg/SQLAlchemy, и резервный драйвер. В Docker-образе основной путь — SQLAlchemy +
psycopg (`ado.persistence.db.SqlAlchemyDatabase`); SQL-запросы у обоих одинаковые.

Поддерживается: простой протокол запросов (`Q`), аутентификация trust / cleartext / md5,
подстановка параметров `:name` литералами с экранированием (`standard_conforming_strings = on`),
преобразование типов по OID (bool, целые, числа, json/jsonb, текст, время — строкой).
SCRAM-SHA-256 не поддерживается — для него используйте psycopg.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import struct
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

PARAM = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
INT_OIDS = {20, 21, 23, 26}
FLOAT_OIDS = {700, 701, 1700}
JSON_OIDS = {114, 3802}


class PgError(Exception):
    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields
        self.sqlstate = fields.get("C", "")
        super().__init__(f"{fields.get('S', 'ERROR')} {self.sqlstate}: {fields.get('M', '')}")


def literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("нечисловое значение float")
        return repr(v)
    if isinstance(v, datetime | date):
        v = v.isoformat()
    if isinstance(v, dict | list | tuple):
        v = json.dumps(v, ensure_ascii=False)
    s = str(v)
    if "\x00" in s:
        raise ValueError("строка содержит NUL")
    return "'" + s.replace("'", "''") + "'"


def bind(sql: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return sql

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"нет параметра :{name}")
        return literal(params[name])

    return PARAM.sub(repl, sql)


class PgWireConnection:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "postgres",
        password: str = "",
        database: str = "postgres",
        timeout: float = 10.0,
    ) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.user, self.password = user, password
        self.status = "I"
        self._buf = b""
        body = (
            struct.pack("!I", 196608)
            + b"".join(
                k.encode() + b"\0" + v.encode() + b"\0"
                for k, v in (("user", user), ("database", database), ("client_encoding", "UTF8"))
            )
            + b"\0"
        )
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        self._auth()

    # ---------------------------------------------------------------- низкий уровень
    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("соединение с PostgreSQL закрыто")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _msg(self) -> tuple[bytes, bytes]:
        head = self._recv(5)
        t, ln = head[:1], struct.unpack("!I", head[1:])[0]
        return t, self._recv(ln - 4)

    def _send(self, t: bytes, body: bytes) -> None:
        self.sock.sendall(t + struct.pack("!I", len(body) + 4) + body)

    @staticmethod
    def _fields(body: bytes) -> dict[str, str]:
        out = {}
        for part in body.split(b"\0"):
            if part:
                out[chr(part[0])] = part[1:].decode("utf-8", "replace")
        return out

    def _auth(self) -> None:
        while True:
            t, body = self._msg()
            if t == b"R":
                code = struct.unpack("!I", body[:4])[0]
                if code == 0:
                    continue
                if code == 3:
                    self._send(b"p", self.password.encode() + b"\0")
                elif code == 5:
                    salt = body[4:8]
                    inner = hashlib.md5((self.password + self.user).encode()).hexdigest().encode()
                    self._send(b"p", b"md5" + hashlib.md5(inner + salt).hexdigest().encode() + b"\0")
                else:
                    raise PgError({"M": f"метод аутентификации {code} не поддерживается (нужен psycopg)"})
            elif t == b"E":
                raise PgError(self._fields(body))
            elif t == b"Z":
                self.status = chr(body[0])
                return

    # ---------------------------------------------------------------- запросы
    def query(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        self._send(b"Q", bind(sql, params).encode("utf-8") + b"\0")
        cols: list[tuple[str, int]] = []
        rows: list[dict[str, Any]] = []
        error: PgError | None = None
        while True:
            t, body = self._msg()
            if t == b"T":
                n = struct.unpack("!H", body[:2])[0]
                pos, cols = 2, []
                for _ in range(n):
                    end = body.index(b"\0", pos)
                    name = body[pos:end].decode()
                    oid = struct.unpack("!I", body[end + 7 : end + 11])[0]
                    cols.append((name, oid))
                    pos = end + 19
            elif t == b"D":
                n = struct.unpack("!H", body[:2])[0]
                pos, row = 2, {}
                for i in range(n):
                    ln = struct.unpack("!i", body[pos : pos + 4])[0]
                    pos += 4
                    if ln < 0:
                        val = None
                    else:
                        raw = body[pos : pos + ln].decode("utf-8")
                        pos += ln
                        val = self._convert(raw, cols[i][1])
                    row[cols[i][0]] = val
                rows.append(row)
            elif t == b"E":
                error = PgError(self._fields(body))
            elif t == b"Z":
                self.status = chr(body[0])
                if error is not None:
                    raise error
                return rows
            # C, I, N, S — пропускаем

    @staticmethod
    def _convert(raw: str, oid: int) -> Any:
        if oid == 16:
            return raw == "t"
        if oid in INT_OIDS:
            return int(raw)
        if oid in FLOAT_OIDS:
            return float(raw)
        if oid in JSON_OIDS:
            return json.loads(raw)
        return raw

    def close(self) -> None:
        try:
            self._send(b"X", b"")
        finally:
            self.sock.close()
