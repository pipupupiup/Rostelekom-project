"""Прогноз фактической длительности работ по истории нарядов (доп. задание, D39).

История — наряды с полем `fact` из `orders_train.json` (`outcome = COMPLETED`,
`actual_duration_min`, `staff_id`). Оценка для пары (тип работ, сотрудник):

* `q`-квантиль (по умолчанию медиана — ожидаемая длительность; `q = 0.75` — осторожнее) фактических
  длительностей сотрудника по этому типу, сглаженный к квантилю по типу работ:
  `w·P_staff + (1 − w)·P_type`, `w = n_staff / (n_staff + k)`;
* мало истории по типу (< `min_type`) — норматив; прогноз не бывает меньше норматива.

Где применяется. План строится по нормативу — этого требует H5 спецификации
(`VISIT.end − VISIT.start = duration_min`). Прогноз используется в раннем предупреждении
(`risk.forecast_route`): если сотрудник по истории работает дольше норматива, прогноз начала
следующих визитов сдвигается и риск опоздания виден раньше. Плюс — в карточке наряда.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        raise ValueError("пустая выборка")
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


@dataclass(frozen=True, slots=True)
class DurationForecast:
    seconds: int
    norm_seconds: int
    n_staff: int
    n_type: int
    source: str
    """`staff+type` | `type` | `norm`."""

    def to_json(self) -> dict:
        return {
            "min": round(self.seconds / 60),
            "norm_min": round(self.norm_seconds / 60),
            "n_staff": self.n_staff,
            "n_type": self.n_type,
            "source": self.source,
        }


@dataclass(slots=True)
class DurationModel:
    q: float = 0.5
    k: float = 5.0
    min_type: int = 10
    by_type: dict[str, list[float]] = field(default_factory=dict)
    by_staff_type: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    source_label: str = ""
    """Откуда история (файл / синтетика) — показывается в UI."""

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.by_type.values())

    @classmethod
    def fit(cls, orders: Iterable[Mapping[str, Any]], q: float = 0.5, label: str = "") -> DurationModel:
        by_type: dict[str, list[float]] = defaultdict(list)
        by_st: dict[tuple[str, str], list[float]] = defaultdict(list)
        for o in orders:
            f = o.get("fact") if isinstance(o, Mapping) else None
            if not isinstance(f, Mapping) or f.get("outcome") != "COMPLETED":
                continue
            d, wt = f.get("actual_duration_min"), o.get("work_type_code")
            if not isinstance(d, int | float) or d <= 0 or not isinstance(wt, str):
                continue
            by_type[wt].append(float(d))
            if isinstance(f.get("staff_id"), str):
                by_st[(wt, f["staff_id"])].append(float(d))
        return cls(q=q, by_type=dict(by_type), by_staff_type=dict(by_st), source_label=label)

    def predict(self, work_type: str, staff_id: str | None, norm_sec: int) -> DurationForecast:
        t = self.by_type.get(work_type, [])
        if len(t) < self.min_type:
            return DurationForecast(norm_sec, norm_sec, 0, len(t), "norm")
        p_type = _quantile(t, self.q)
        st = self.by_staff_type.get((work_type, staff_id or ""), [])
        if st:
            w = len(st) / (len(st) + self.k)
            est, src = w * _quantile(st, self.q) + (1 - w) * p_type, "staff+type"
        else:
            est, src = p_type, "type"
        return DurationForecast(max(norm_sec, round(est * 60)), norm_sec, len(st), len(t), src)

    def summary(self) -> dict:
        return {
            "orders": self.size,
            "quantile": self.q,
            "source": self.source_label,
            "by_type": {
                wt: {
                    "n": len(v),
                    "p50_min": round(_quantile(v, 0.5)),
                    f"p{round(self.q * 100)}_min": round(_quantile(v, self.q)),
                }
                for wt, v in sorted(self.by_type.items())
            },
        }


__all__ = ["DurationForecast", "DurationModel"]
