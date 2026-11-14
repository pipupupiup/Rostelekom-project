"""ADO-03 — выявление подозрений на групповую проблему (ПГП).

Правило (D9, эталон организаторов): не менее `min_count` нарядов типа с `pgp_relevant = true`
с одного узла доступа, зарегистрированных в пределах `window_sec` (скользящее окно).

Алгоритм для каждого узла (наряды по `(registered_at, order_id)`):
1. **Пополнение**: ещё не сгруппированный наряд присоединяется к уже существующей группе узла,
   если разброс регистрации группы вместе с ним остаётся ≤ окна. `detected_at` не меняется.
2. **Новые группы**: двумя указателями отмечаются наряды, входящие хотя бы в одно окно длиной
   ≤ `window_sec` с ≥ `min_count` нарядами; отмеченные наряды объединяются в группы с разбросом
   регистрации ≤ окна (`detected_at = now`).

Наряды с начатым визитом засчитываются в порог (они свидетельствуют об инциденте), но в состав
группы не включаются и остаются в маршруте (спецификация, схема `PgpGroup`). Отменённые наряды и
наряды, зарегистрированные позже `now`, не учитываются. Повторный запуск на тех же данных не
создаёт дублей: результат детерминирован, существующие группы только пополняются.

Наряды группы передаются в станционное подразделение (`TRANSFER_TO_STATION`) и **не считаются
выполненными** — их учитывает только ПГП-метрика (ADO-11).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ado.domain.contracts import PgpDetectionResult
from ado.domain.models import Order, PgpGroup, ReferenceData
from ado.domain.timebase import Seconds

SuspectedPGP = PgpGroup
"""Псевдоним из задания ADO-03: объект подозрения на ПГП — это `PgpGroup` домена."""


@dataclass(frozen=True, slots=True)
class PgpConfig:
    min_count: int = 3
    window_sec: Seconds = 3600

    def __post_init__(self) -> None:
        if self.min_count < 2:
            raise ValueError("min_count должен быть не меньше 2 (order_ids.minItems = 2)")
        if self.window_sec <= 0:
            raise ValueError("window_sec должен быть положительным")


def _group_id(day: str, n: int) -> str:
    return f"PGP-{day.replace('-', '')}-{n:02d}"


class PgpDetector:
    def __init__(self, config: PgpConfig | None = None) -> None:
        self.config = config or PgpConfig()

    def detect(
        self,
        orders: Iterable[Order],
        existing: Sequence[PgpGroup],
        started_order_ids: frozenset[str],
        now: Seconds,
        reference: ReferenceData,
        *,
        day: str = "",
        cancelled_order_ids: frozenset[str] = frozenset(),
    ) -> PgpDetectionResult:
        cfg = self.config
        by_id: dict[str, Order] = {}
        for o in orders:
            by_id.setdefault(o.order_id, o)  # повтор того же наряда не дублирует его

        groups = [
            PgpGroup(g.group_id, g.access_node_id, list(g.order_ids), g.detected_at, g.action, g.comment)
            for g in existing
        ]
        grouped = {oid for g in groups for oid in g.order_ids}
        relevant: dict[str, list[Order]] = defaultdict(list)
        for o in by_id.values():
            wt = reference.work_types.get(o.work_type_code)
            if wt is None or not wt.pgp_relevant:
                continue
            if o.registered_at > now or o.order_id in cancelled_order_ids:
                continue
            relevant[o.access_node_id].append(o)

        new_ids: list[str] = []
        changed: set[str] = set()
        for node_id in sorted(relevant):
            node_orders = sorted(relevant[node_id], key=lambda o: (o.registered_at, o.order_id))
            self._attach(node_orders, groups, node_id, grouped, started_order_ids, by_id, changed)

            # 2. новые группы по скользящему окну среди несгруппированных
            free = [o for o in node_orders if o.order_id not in grouped]
            marked = [False] * len(free)
            left = 0
            for right in range(len(free)):
                while free[right].registered_at - free[left].registered_at > cfg.window_sec:
                    left += 1
                if right - left + 1 >= cfg.min_count:
                    for k in range(left, right + 1):
                        marked[k] = True
            clusters: list[list[Order]] = []
            for k, o in enumerate(free):
                if not marked[k]:
                    continue
                # разброс регистрации внутри группы ≤ окна (эталон: «в пределах 60 минут»)
                if clusters and o.registered_at - clusters[-1][0].registered_at <= cfg.window_sec:
                    clusters[-1].append(o)
                else:
                    clusters.append([o])
            node = reference.access_nodes.get(node_id)
            node_name = node.name if node else node_id
            for cluster in clusters:
                members = [o.order_id for o in cluster if o.order_id not in started_order_ids]
                if len(members) < 2:
                    continue
                span_min = (cluster[-1].registered_at - cluster[0].registered_at) // 60
                started_note = len(cluster) - len(members)
                comment = (
                    f"{len(cluster)} обращений с узла {node_id} ({node_name}) за {span_min} мин; "
                    f"порог {cfg.min_count} за {cfg.window_sec // 60} мин"
                    + (f"; {started_note} с начатым визитом остаются в маршруте" if started_note else "")
                )
                gid = _group_id(day, len(groups) + 1)
                groups.append(PgpGroup(gid, node_id, members, now, comment=comment))
                grouped.update(members)
                new_ids.append(gid)
            # пополнение и новых групп: результат не зависит от истории вызовов
            self._attach(node_orders, groups, node_id, grouped, started_order_ids, by_id, changed)

        for g in groups:
            g.order_ids.sort(key=lambda oid: (by_id[oid].registered_at, oid) if oid in by_id else (0, oid))
            if g.group_id in changed and g.group_id not in new_ids:
                g.comment = (g.comment or "") + f"; пополнена до {len(g.order_ids)} нарядов"
        return PgpDetectionResult(groups, new_ids, frozenset(grouped))

    def _attach(
        self,
        node_orders: list[Order],
        groups: list[PgpGroup],
        node_id: str,
        grouped: set[str],
        started: frozenset[str],
        by_id: dict[str, Order],
        changed: set[str],
    ) -> None:
        """Присоединить несгруппированные наряды узла к группам узла (итеративно — цепочки)."""
        node_groups = [g for g in groups if g.access_node_id == node_id]
        progress = bool(node_groups)
        while progress:
            progress = False
            for o in node_orders:
                if o.order_id in grouped or o.order_id in started:
                    continue
                for g in node_groups:
                    times = [by_id[m].registered_at for m in g.order_ids if m in by_id]
                    if (
                        times
                        and max([*times, o.registered_at]) - min([*times, o.registered_at])
                        <= self.config.window_sec
                    ):
                        g.order_ids.append(o.order_id)
                        grouped.add(o.order_id)
                        changed.add(g.group_id)
                        progress = True
                        break
