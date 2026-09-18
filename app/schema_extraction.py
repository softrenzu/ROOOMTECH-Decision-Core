from __future__ import annotations

import json
import re
import time
from typing import Any

from jsonschema import validators

from app.advanced_models import SchemaExtractionRequest, SchemaExtractionResponse

_MISSING = object()


def _normalize_key(value: str) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value)).casefold()


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _coerce_scalar(value: Any, schema: dict[str, Any]) -> Any:
    if value is _MISSING:
        return _MISSING
    if "const" in schema:
        return schema["const"]
    if value is None:
        return None
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        candidates = [item for item in schema_type if item != "null"]
        schema_type = candidates[0] if candidates else "null"
    if "enum" in schema:
        for option in schema["enum"]:
            if str(option).casefold() == str(value).strip().casefold():
                return option
    if schema_type == "string" or schema_type is None:
        return str(value).strip()
    if schema_type == "integer":
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).replace(",", "").strip()))
    if schema_type == "number":
        return float(str(value).replace(",", "").strip())
    if schema_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"true", "yes", "1", "on", "はい", "有", "あり"}:
            return True
        if normalized in {"false", "no", "0", "off", "いいえ", "無", "なし"}:
            return False
        raise ValueError(f"cannot coerce {value!r} to boolean")
    if schema_type == "null":
        return None
    return value


def _parse_key_values(text: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([^:=：]{1,120})\s*[:=：]\s*(.*?)\s*$", line)
        if match:
            values[match.group(1).strip()] = match.group(2).strip()
    return values


def _lookup(source: dict[str, Any], names: list[str]) -> Any:
    normalized = {_normalize_key(key): value for key, value in source.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized:
            return normalized[key]
    return _MISSING


def _project(schema: dict[str, Any], raw: Any, kv: dict[str, Any]) -> Any:
    if raw is _MISSING and "default" in schema:
        return schema["default"]
    for union_key in ("oneOf", "anyOf"):
        if union_key in schema:
            for candidate in schema[union_key]:
                try:
                    value = _project(candidate, raw, kv)
                    validator_cls = validators.validator_for(candidate)
                    validator_cls(candidate).validate(value)
                    return value
                except Exception:
                    continue
            return raw
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        source = raw if isinstance(raw, dict) else kv
        result: dict[str, Any] = {}
        for name, property_schema in schema.get("properties", {}).items():
            aliases = [name]
            title = property_schema.get("title")
            if title:
                aliases.append(str(title))
            aliases.extend(str(item) for item in property_schema.get("x-rtdc-aliases", []))
            candidate = _lookup(source, aliases) if isinstance(source, dict) else _MISSING
            if candidate is _MISSING and (property_schema.get("type") == "object" or "properties" in property_schema):
                candidate = source
            value = _project(property_schema, candidate, kv)
            if value is not _MISSING:
                result[name] = value
        return result
    if schema_type == "array":
        if raw is _MISSING:
            return _MISSING
        if isinstance(raw, list):
            values = raw
        elif isinstance(raw, str):
            values = [part.strip() for part in re.split(r"[,、;\n]", raw) if part.strip()]
        else:
            values = [raw]
        item_schema = schema.get("items", {})
        return [_project(item_schema, value, kv) for value in values]
    try:
        return _coerce_scalar(raw, schema)
    except Exception:
        return raw


def _validate(schema: dict[str, Any], data: Any) -> list[str]:
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(data), key=lambda item: list(item.absolute_path))
    output: list[str] = []
    for error in errors[:50]:
        path = ".".join(str(part) for part in error.absolute_path)
        output.append(f"{path or '$'}: {error.message}")
    return output


class SchemaExtractor:
    def __init__(self, model_provider=None):
        self.model_provider = model_provider

    def _heuristic(self, text: str, schema: dict[str, Any]) -> Any:
        raw: Any = _MISSING
        stripped = _strip_json_fence(text)
        if stripped[:1] in {"{", "[", '"'} or stripped in {"true", "false", "null"}:
            try:
                raw = json.loads(stripped)
            except Exception:
                raw = _MISSING
        kv = _parse_key_values(text)
        return _project(schema, raw, kv)

    async def extract(self, request: SchemaExtractionRequest) -> SchemaExtractionResponse:
        started = time.perf_counter()
        heuristic = self._heuristic(request.input, request.json_schema)
        heuristic_errors = _validate(request.json_schema, heuristic)
        if request.provider == "heuristic" or (request.provider == "auto" and not heuristic_errors):
            if request.strict and heuristic_errors:
                raise ValueError("schema validation failed: " + "; ".join(heuristic_errors[:5]))
            return SchemaExtractionResponse(
                data=heuristic,
                valid=not heuristic_errors,
                validation_errors=heuristic_errors,
                provider="heuristic",
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )

        if not self.model_provider or not self.model_provider.configured:
            if request.strict:
                raise RuntimeError("OpenAI-compatible provider is not configured and heuristic extraction did not satisfy the schema")
            return SchemaExtractionResponse(
                data=heuristic,
                valid=False,
                validation_errors=heuristic_errors,
                provider="heuristic",
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )

        data = await self.model_provider.extract_json(request.input, request.json_schema)
        errors = _validate(request.json_schema, data)
        if request.strict and errors:
            raise ValueError("schema validation failed: " + "; ".join(errors[:5]))
        return SchemaExtractionResponse(
            data=data,
            valid=not errors,
            validation_errors=errors,
            provider="openai_compatible",
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
