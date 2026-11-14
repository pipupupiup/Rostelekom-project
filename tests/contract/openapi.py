"""Проверка JSON по схемам OpenAPI 3.0 из spec/api_spec.yaml через jsonschema.

OpenAPI 3.0 → JSON Schema: `nullable: true` → `anyOf [..., null]`; служебные ключи удаляются.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

SPEC = Path(__file__).resolve().parents[2] / "spec" / "api_spec.yaml"
DROP = {"example", "examples", "discriminator", "xml", "externalDocs", "deprecated"}


def _convert(node: Any) -> Any:
    if isinstance(node, list):
        return [_convert(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {
        k: _convert(v)
        for k, v in node.items()
        if k not in DROP and not k.startswith("x-") and k != "nullable"
    }
    if node.get("nullable") is True:
        return {"anyOf": [out, {"type": "null"}]}
    return out


@lru_cache(maxsize=1)
def spec() -> dict:
    with SPEC.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def _components() -> dict:
    return {"schemas": _convert(spec()["components"]["schemas"])}


def errors(instance: Any, schema_name: str) -> list[str]:
    root = {"$ref": f"#/components/schemas/{schema_name}", "components": _components()}
    v = Draft202012Validator(root, format_checker=FormatChecker())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(instance)]


def response_schema(path: str, method: str, status: int) -> str | None:
    """Имя схемы ответа из спецификации (через $ref ответов components/responses)."""
    op = spec()["paths"][path][method]
    resp = op["responses"].get(str(status))
    if resp is None:
        return None
    if "$ref" in resp:
        resp = spec()["components"]["responses"][resp["$ref"].split("/")[-1]]
    ref = resp["content"]["application/json"]["schema"]["$ref"]
    return ref.split("/")[-1]
