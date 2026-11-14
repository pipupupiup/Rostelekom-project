"""Тонкая абстракция подключения к PostgreSQL (ADO-15).

`Database.transaction()` — контекст одной транзакции: COMMIT при успехе, ROLLBACK при любом
исключении. Внутри — `execute(sql, params) → list[dict]` с параметрами `:name`.

* `SqlAlchemyDatabase` — основной путь (Docker): SQLAlchemy 2 + psycopg 3, пул соединений;
* `PgWireDatabase` — резервный драйвер на чистом Python (тесты без psycopg).

Ошибки нарушения ограничений приводятся к `IntegrityViolation`, сериализации/блокировок —
к `TransientDbError`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from ado.persistence.pgwire import PgError, PgWireConnection


class DbError(Exception):
    pass


class IntegrityViolation(DbError):
    """Нарушено ограничение БД (уникальность, внешний ключ, CHECK)."""

    def __init__(self, message: str, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


class TransientDbError(DbError):
    """Конфликт сериализации / взаимоблокировка — можно повторить."""


class Executor(Protocol):
    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]: ...


class Database(Protocol):
    def transaction(self) -> contextlib.AbstractContextManager[Executor]: ...


def _translate_pg(e: PgError) -> DbError:
    state = e.sqlstate
    if state.startswith("23"):
        return IntegrityViolation(str(e), e.fields.get("n"))
    if state in ("40001", "40P01", "55P03"):
        return TransientDbError(str(e))
    return DbError(str(e))


class _PgExec:
    def __init__(self, conn: PgWireConnection) -> None:
        self.conn = conn

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            return self.conn.query(sql, params)
        except PgError as e:
            raise _translate_pg(e) from e


class PgWireDatabase:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "postgres",
        password: str = "",
        database: str = "postgres",
    ) -> None:
        self.kw = dict(host=host, port=port, user=user, password=password, database=database)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Executor]:
        conn = PgWireConnection(**self.kw)  # type: ignore[arg-type]
        try:
            conn.query("BEGIN")
            try:
                yield _PgExec(conn)
            except BaseException:
                conn.query("ROLLBACK")
                raise
            try:
                conn.query("COMMIT")
            except PgError as e:
                raise _translate_pg(e) from e
        finally:
            conn.close()


class SqlAlchemyDatabase:
    def __init__(self, url: str) -> None:
        from sqlalchemy import create_engine

        self.engine = create_engine(url, pool_pre_ping=True, pool_size=5, future=True)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Executor]:
        from sqlalchemy import exc, text

        class _Exec:
            def __init__(self, conn: Any) -> None:
                self.conn = conn

            def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
                try:
                    res = self.conn.execute(text(sql), dict(params or {}))
                except exc.IntegrityError as e:
                    raise IntegrityViolation(str(e.orig)) from e
                except exc.OperationalError as e:
                    code = getattr(getattr(e, "orig", None), "sqlstate", "") or ""
                    if code in ("40001", "40P01", "55P03"):
                        raise TransientDbError(str(e.orig)) from e
                    raise DbError(str(e.orig)) from e
                return [dict(r._mapping) for r in res] if res.returns_rows else []

        with self.engine.begin() as conn:
            yield _Exec(conn)


def database_from_url(url: str) -> Database:
    """`postgresql+psycopg://…` → SQLAlchemy, если установлен; иначе встроенный драйвер."""
    try:
        import sqlalchemy  # noqa: F401

        return SqlAlchemyDatabase(url)
    except ImportError:
        u = urlparse(url.replace("postgresql+psycopg", "postgresql"))
        return PgWireDatabase(
            u.hostname or "127.0.0.1",
            u.port or 5432,
            unquote(u.username or "postgres"),
            unquote(u.password or ""),
            (u.path or "/postgres").lstrip("/") or "postgres",
        )
