"""Строгое чтение полей JSON с путём к полю в сообщениях об ошибках.

Функции возвращают `MISSING` при ошибке и записывают её в `IssueCollector`, чтобы за один проход
собрать все проблемы входа, а не только первую.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from ado.data.errors import IssueCollector
from ado.domain.timebase import parse_local_time


class _Missing:
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()
E = TypeVar("E", bound=StrEnum)


class Reader:
    def __init__(self, issues: IssueCollector) -> None:
        self.issues = issues

    # ---- контейнеры
    def obj(self, value: Any, path: str) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            self.issues.format(path, f"ожидается объект, получено {type(value).__name__}")
            return None
        return value

    def array(self, value: Any, path: str, *, allow_empty: bool = True) -> list[Any] | None:
        if not isinstance(value, list):
            self.issues.format(path, f"ожидается массив, получено {type(value).__name__}")
            return None
        if not allow_empty and not value:
            self.issues.format(path, "массив не может быть пустым")
            return None
        return value

    def _get(self, obj: Mapping[str, Any], key: str, path: str, required: bool) -> Any:
        if key not in obj or (obj[key] is None and required):
            if required:
                self.issues.format(f"{path}.{key}" if path else key, "обязательное поле отсутствует")
            return MISSING
        return obj[key]

    # ---- скаляры
    def string(
        self,
        obj: Mapping[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
        pattern: str | None = None,
    ) -> Any:
        v = self._get(obj, key, path, required)
        if v is MISSING or (v is None and not required):
            return None if not required else MISSING
        p = f"{path}.{key}" if path else key
        if not isinstance(v, str):
            self.issues.format(p, "ожидается строка")
            return MISSING
        if pattern and not re.fullmatch(pattern, v):
            self.issues.format(p, f"значение {v!r} не соответствует шаблону {pattern}")
            return MISSING
        return v

    def boolean(self, obj: Mapping[str, Any], key: str, path: str) -> Any:
        v = self._get(obj, key, path, True)
        if v is MISSING:
            return MISSING
        if not isinstance(v, bool):
            self.issues.format(f"{path}.{key}", "ожидается boolean")
            return MISSING
        return v

    def integer(
        self,
        obj: Mapping[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
        minimum: int | None = None,
        nullable: bool = False,
    ) -> Any:
        if nullable and key in obj and obj[key] is None:
            return None
        v = self._get(obj, key, path, required)
        if v is MISSING or (v is None and not required):
            return None if not required else MISSING
        p = f"{path}.{key}"
        if isinstance(v, bool) or not isinstance(v, int):
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            else:
                self.issues.format(p, "ожидается целое число")
                return MISSING
        if minimum is not None and v < minimum:
            self.issues.format(p, f"значение {v} меньше минимума {minimum}")
            return MISSING
        return v

    def number(
        self,
        obj: Mapping[str, Any],
        key: str,
        path: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        required: bool = True,
    ) -> Any:
        v = self._get(obj, key, path, required)
        if v is MISSING or (v is None and not required):
            return None if not required else MISSING
        p = f"{path}.{key}"
        if isinstance(v, bool) or not isinstance(v, int | float):
            self.issues.format(p, "ожидается число")
            return MISSING
        if v != v or v in (float("inf"), float("-inf")):
            self.issues.format(p, "число должно быть конечным")
            return MISSING
        if (minimum is not None and v < minimum) or (maximum is not None and v > maximum):
            self.issues.format(p, f"значение {v} вне диапазона [{minimum}, {maximum}]")
            return MISSING
        return float(v)

    def enum(self, obj: Mapping[str, Any], key: str, path: str, enum_cls: type[E]) -> Any:
        v = self._get(obj, key, path, True)
        if v is MISSING:
            return MISSING
        try:
            return enum_cls(v)
        except ValueError:
            allowed = ", ".join(m.value for m in enum_cls)
            self.issues.format(f"{path}.{key}", f"значение {v!r} не входит в [{allowed}]")
            return MISSING

    def enum_set(self, obj: Mapping[str, Any], key: str, path: str, enum_cls: type[E]) -> Any:
        v = self._get(obj, key, path, True)
        if v is MISSING:
            return MISSING
        p = f"{path}.{key}"
        if not isinstance(v, list) or not v:
            self.issues.format(p, "ожидается непустой массив")
            return MISSING
        out = []
        for i, item in enumerate(v):
            try:
                out.append(enum_cls(item))
            except ValueError:
                allowed = ", ".join(m.value for m in enum_cls)
                self.issues.format(f"{p}[{i}]", f"значение {item!r} не входит в [{allowed}]")
                return MISSING
        if len(set(out)) != len(out):
            self.issues.format(p, "значения должны быть уникальными")
            return MISSING
        return frozenset(out)

    def datetime(self, obj: Mapping[str, Any], key: str, path: str, *, required: bool = True) -> Any:
        v = self._get(obj, key, path, required)
        if v is MISSING or (v is None and not required):
            return None if not required else MISSING
        p = f"{path}.{key}"
        if not isinstance(v, str):
            self.issues.format(p, "ожидается date-time (строка ISO 8601)")
            return MISSING
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            self.issues.format(p, f"ожидается date-time, получено {v!r}")
            return MISSING
        if dt.tzinfo is None:
            self.issues.format(p, "date-time должен содержать смещение часового пояса")
            return MISSING
        return dt

    def local_time(self, obj: Mapping[str, Any], key: str, path: str) -> Any:
        v = self.string(obj, key, path)
        if v is MISSING:
            return MISSING
        try:
            return parse_local_time(v)
        except ValueError as e:
            self.issues.format(f"{path}.{key}", str(e))
            return MISSING


def ok(*values: Any) -> bool:
    """Все значения прочитаны без ошибок."""
    return all(v is not MISSING for v in values)
