from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from backend.app.contracts.v1 import models as contract_models
from backend.app.policy import evaluation, opportunity
from backend.app.services import acquisition
from backend.app.services import agent as agent_service

_CODEC = "alphadecay.agent-value.v1"
_MODULES = (contract_models, evaluation, opportunity, acquisition, agent_service)


def encode_agent_value(value: object) -> dict[str, object]:
    encoded = _encode(value)
    if not isinstance(encoded, dict) or "$type" not in encoded:
        raise ValueError("AGENT_CODEC_ROOT_TYPE_INVALID")
    return {"codec": _CODEC, "value": encoded}


def decode_agent_value(payload: dict[str, object]) -> object:
    if payload.get("codec") != _CODEC or set(payload) != {"codec", "value"}:
        raise ValueError("AGENT_CODEC_ENVELOPE_INVALID")
    return _decode(payload["value"])


def _encode(value: object) -> object:
    if isinstance(value, Enum):
        return {"$type": "enum", "class": _class_key(type(value)), "value": value.value}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": "dataclass",
            "class": _class_key(type(value)),
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, BaseModel):
        return {
            "$type": "model",
            "class": _class_key(type(value)),
            "fields": {
                name: _encode(getattr(value, name)) for name in value.__class__.model_fields
            },
        }
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("AGENT_CODEC_DATETIME_NOT_UTC")
        return {"$type": "datetime", "value": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, timedelta):
        microseconds = value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
        return {"$type": "timedelta", "microseconds": microseconds}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("AGENT_CODEC_DECIMAL_INVALID")
        return {"$type": "decimal", "value": format(value, "f")}
    if isinstance(value, UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, tuple):
        return {"$type": "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return {"$type": "list", "items": [_encode(item) for item in value]}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("AGENT_CODEC_DICT_KEY_INVALID")
        return {
            "$type": "dict",
            "items": {key: _encode(value[key]) for key in sorted(value)},
        }
    if value is None or isinstance(value, bool | int | str):
        return value
    raise ValueError("AGENT_CODEC_TYPE_UNSUPPORTED")


def _decode(value: object) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if not isinstance(value, dict) or not isinstance(value.get("$type"), str):
        raise ValueError("AGENT_CODEC_VALUE_INVALID")
    kind = value["$type"]
    if kind == "datetime":
        decoded = datetime.fromisoformat(str(value["value"]))
        if decoded.tzinfo is None or decoded.utcoffset() != timedelta(0):
            raise ValueError("AGENT_CODEC_DATETIME_NOT_UTC")
        return decoded.astimezone(UTC)
    if kind == "date":
        return date.fromisoformat(str(value["value"]))
    if kind == "timedelta":
        return timedelta(microseconds=int(value["microseconds"]))
    if kind == "decimal":
        decoded_decimal = Decimal(str(value["value"]))
        if not decoded_decimal.is_finite():
            raise ValueError("AGENT_CODEC_DECIMAL_INVALID")
        return decoded_decimal
    if kind == "uuid":
        return UUID(str(value["value"]))
    if kind in {"tuple", "list"}:
        items = [_decode(item) for item in _items(value)]
        return tuple(items) if kind == "tuple" else items
    if kind == "dict":
        items = value.get("items")
        if not isinstance(items, dict):
            raise ValueError("AGENT_CODEC_VALUE_INVALID")
        return {str(key): _decode(item) for key, item in items.items()}
    cls = _registry().get(str(value.get("class")))
    if cls is None:
        raise ValueError("AGENT_CODEC_CLASS_INVALID")
    if kind == "enum" and issubclass(cls, Enum):
        return cls(value["value"])
    fields = value.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("AGENT_CODEC_VALUE_INVALID")
    decoded_fields = {str(name): _decode(item) for name, item in fields.items()}
    if kind == "dataclass" and dataclasses.is_dataclass(cls):
        return cls(**decoded_fields)
    if kind == "model" and issubclass(cls, BaseModel):
        return cls.model_validate(decoded_fields)
    raise ValueError("AGENT_CODEC_CLASS_KIND_INVALID")


def _items(value: dict[str, object]) -> list[object]:
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("AGENT_CODEC_VALUE_INVALID")
    return items


def _class_key(cls: type[object]) -> str:
    key = f"{cls.__module__}.{cls.__qualname__}"
    if key not in _registry():
        raise ValueError("AGENT_CODEC_CLASS_INVALID")
    return key


def _registry() -> dict[str, type[Any]]:
    classes: dict[str, type[Any]] = {}
    for module in _MODULES:
        for _, candidate in inspect.getmembers(module, inspect.isclass):
            if candidate.__module__ != module.__name__:
                continue
            if dataclasses.is_dataclass(candidate) or issubclass(candidate, Enum | BaseModel):
                classes[f"{candidate.__module__}.{candidate.__qualname__}"] = candidate
    return classes
