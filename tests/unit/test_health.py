from __future__ import annotations

from ado.api.app import create_app
from ado.config import Settings
from tests.asgi import call


def test_health_matches_spec_schema() -> None:
    status, body = call(create_app(Settings(team="Тест")), "GET", "/api/v1/health")
    assert status == 200
    assert body["status"] == "ok" and body["team"] == "Тест"
