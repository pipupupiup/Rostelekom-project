"""Минимальный ASGI-клиент для тестов (httpx в окружении разработки нет)."""

from __future__ import annotations

import asyncio
import json
from typing import Any


def call(app, method: str, path: str, body: Any = None, raw: bytes | None = None) -> tuple[int, Any]:
    payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    path, _, query = path.partition("?")

    async def run():
        sent = {"done": False}
        out: dict[str, Any] = {"status": None, "body": b""}

        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("t", 1),
            "server": ("t", 80),
            "scheme": "http",
        }
        await app(scope, receive, send)
        return out

    out = asyncio.run(run())
    data = out["body"]
    try:
        return out["status"], json.loads(data) if data else None
    except ValueError:
        return out["status"], data.decode()
