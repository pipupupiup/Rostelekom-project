"""Настройки сервиса из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    port: int = 8080
    team: str = "ADO"
    database_url: str | None = None
    """Если не задан — сервис работает без PostgreSQL (только память). Оценка от БД не зависит."""
    plan_time_limit_sec: float = 300.0
    event_time_limit_sec: float = 10.0
    plan_reserve_sec: float = 15.0
    """Запас на валидацию и сериализацию из лимита 300 с."""
    event_reserve_sec: float = 2.0
    """Запас из лимита 10 с."""
    graphhopper_url: str | None = None
    """GraphHopper для линий маршрутов по дорогам на карте (D35, D37). Приоритетнее OSRM."""
    graphhopper_key: str | None = None
    """Ключ GraphHopper Directions API (только для облачного сервиса; своему серверу не нужен)."""
    graphhopper_profile: str = "car"
    history_file: str | None = None
    """D39: `orders_train.json` с полем `fact` — история для прогноза длительности работ."""
    traffic: bool = False
    """D38: учитывать `hourly_factors` матрицы (загруженность дорог по часам). По умолчанию выкл.:
    базовая оценка их не применяет, а учёт делает план осторожнее (меньше нарядов в смену)."""
    osrm_url: str | None = None
    """OSRM — альтернатива GraphHopper. Ни один не задан — карта сообщает, что дорог нет."""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            port=int(os.environ.get("ADO_PORT", "8080")),
            team=os.environ.get("ADO_TEAM", "ADO"),
            database_url=os.environ.get("ADO_DATABASE_URL") or None,
            graphhopper_url=os.environ.get("ADO_GRAPHHOPPER_URL") or None,
            graphhopper_key=os.environ.get("ADO_GRAPHHOPPER_KEY") or None,
            graphhopper_profile=os.environ.get("ADO_GRAPHHOPPER_PROFILE") or "car",
            osrm_url=os.environ.get("ADO_OSRM_URL") or None,
            history_file=os.environ.get("ADO_HISTORY_FILE") or None,
            traffic=os.environ.get("ADO_TRAFFIC", "").strip().lower() in ("1", "true", "yes", "on"),
        )
