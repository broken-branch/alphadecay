from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Literal, Protocol, Union, get_args, get_origin
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from alpaca.common.enums import Sort
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order, Position, TradeAccount
from alpaca.trading.requests import GetOrdersRequest
from httpx import URL
from pydantic import BaseModel

from backend.app.alpaca.mcp import EXPOSED_TOOL_SURFACE, AlpacaMCPResearchClient
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor
from backend.app.services.agent import (
    AgentDecision,
    AgentDecisionRepository,
    AgentRunService,
    AgentTick,
    PersistedAgentDecision,
    RuntimeCompositionPort,
)

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
CLI_VERSION = "0.0.13"
ARCHIVE_SHA256 = "50cd254d81b6bbc541259eeeb4bb1a8f7c319557fa49fc3b2765cddd72a66a82"
BINARY_SHA256 = "502bb6a8c87f0b6791669861853168caf41f228767bd89e88f6eabe5f1e8cc1c"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_ARTIFACT_ROOT = REPOSITORY_ROOT / ".private" / "provider-rehearsals"
OFFICIAL_CLI_ARCHIVE = (
    REPOSITORY_ROOT / ".private" / "operator-tools" / "alpaca-cli-0.0.13-linux-amd64.tar.gz"
)
MAX_PROVIDER_STRING = 256
MAX_PROVIDER_ITEMS = 10_000
MAX_PROVIDER_NUMBER_RENDER = 64
MAX_PROVIDER_ABS = Decimal("1e18")
MIN_PROVIDER_ABS = Decimal("1e-18")
MAX_ACTIVITY_PAGES = 100
ACTIVITY_PAGE_SIZE = 100
MAX_CLI_OUTPUT = 64 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
READ_HTTP_METHODS = frozenset({"GET"})
ALLOWED_PROVIDER_READ_PATHS = frozenset(
    {
        ("paper-api.alpaca.markets", "/v2/account"),
        ("paper-api.alpaca.markets", "/v2/account/activities"),
        ("paper-api.alpaca.markets", "/v2/orders"),
        ("paper-api.alpaca.markets", "/v2/positions"),
    }
)
ProviderRecordKind = Literal["account", "position", "order"]
MAX_TRANSPORT_OPERATIONS = 20_000
PROVIDER_NUMBER = re.compile(r"^[+-]?[0-9]{1,32}(?:\.[0-9]{0,32})?(?:[eE][+-]?[0-9]{1,3})?$")
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_PRIVATE_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_PUBLIC_ARTIFACT_BYTES = 64 * 1024
MAX_ARTIFACT_JSON_DEPTH = 32
MAX_ARTIFACT_JSON_ITEMS = 100_000
MAX_ARTIFACT_NUMERIC_BYTES = 32 * 1024
MAX_ARTIFACT_ABS_NUMBER = 10**18
MIN_ARTIFACT_ABS_FLOAT = 1e-18
REHEARSAL_SCHEMA = "alphadecay.provider-rehearsal.operator.v6"
FIXTURE_SCHEMA = "alphadecay.provider-rehearsal.fixture.v6"


class RehearsalError(RuntimeError):
    pass


_EnumAuthority = tuple[str, Enum, str, bool | str | int]


class _ProviderSdkAuthority:
    def __init__(self) -> None:
        self.base = BaseModel
        self.base_dump = BaseModel.model_dump
        self.types: dict[ProviderRecordKind, type[object]] = {
            "account": TradeAccount,
            "position": Position,
            "order": Order,
        }
        self.registry: Mapping[ProviderRecordKind, type[object]] = MappingProxyType(self.types)
        self.fields = {
            kind: tuple(official_type.model_fields.items())
            for kind, official_type in self.types.items()
        }
        self.annotations = {
            kind: MappingProxyType({name: field.annotation for name, field in fields})
            for kind, fields in self.fields.items()
        }
        self.field_mappings = {
            kind: official_type.model_fields for kind, official_type in self.types.items()
        }
        self.model_dumps = {
            kind: official_type.model_dump for kind, official_type in self.types.items()
        }
        self.serializers = {
            kind: official_type.__pydantic_serializer__
            for kind, official_type in self.types.items()
        }
        self.modules = {
            kind: (sys.modules[official_type.__module__], official_type.__name__)
            for kind, official_type in self.types.items()
        }
        self.nested_fields: dict[type[BaseModel], tuple[tuple[str, object], ...]] = {}
        self.nested_annotations: dict[type[BaseModel], Mapping[str, object]] = {}
        self.nested_field_mappings: dict[type[BaseModel], object] = {}
        self.nested_model_dumps: dict[type[BaseModel], object] = {}
        self.nested_serializers: dict[type[BaseModel], object] = {}
        self.nested_modules: dict[type[BaseModel], tuple[object, str]] = {}
        self.enum_members: dict[type[Enum], tuple[_EnumAuthority, ...]] = {}
        self.enum_member_maps: dict[type[Enum], object] = {}
        self.enum_modules: dict[type[Enum], tuple[object, str]] = {}
        self.enum_value_maps: dict[
            type[Enum], tuple[object, tuple[tuple[bool | str | int, Enum], ...]]
        ] = {}
        for field_annotations in self.annotations.values():
            for annotation in field_annotations.values():
                self.collect_annotation(annotation)

    def collect_annotation(self, annotation: object) -> None:
        for argument in get_args(annotation):
            self.collect_annotation(argument)
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            self._collect_enum(annotation)
            return
        if self._is_new_nested_model(annotation):
            self._collect_nested_model(annotation)

    def _collect_enum(self, annotation: type[Enum]) -> None:
        if annotation in self.enum_members:
            return
        member_map = vars(annotation).get("_member_map_")
        value_map = vars(annotation).get("_value2member_map_")
        if type(member_map) is not dict or type(value_map) is not dict:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        self.enum_member_maps[annotation] = member_map
        self.enum_modules[annotation] = (sys.modules[annotation.__module__], annotation.__name__)
        self.enum_members[annotation] = tuple(
            self._freeze_enum_member(name, member) for name, member in member_map.items()
        )
        self.enum_value_maps[annotation] = (value_map, tuple(value_map.items()))

    @staticmethod
    def _freeze_enum_member(name: str, member: Enum) -> _EnumAuthority:
        member_name = object.__getattribute__(member, "_name_")
        member_value = object.__getattribute__(member, "_value_")
        if type(member_name) is not str or type(member_value) not in {bool, str, int}:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return name, member, member_name, member_value

    def _is_new_nested_model(self, annotation: object) -> bool:
        return (
            isinstance(annotation, type)
            and issubclass(annotation, self.base)
            and annotation not in self.types.values()
            and annotation not in self.nested_fields
        )

    def _collect_nested_model(self, annotation: type[BaseModel]) -> None:
        fields = tuple(annotation.model_fields.items())
        annotations = MappingProxyType({name: field.annotation for name, field in fields})
        self.nested_fields[annotation] = fields
        self.nested_annotations[annotation] = annotations
        self.nested_field_mappings[annotation] = annotation.model_fields
        self.nested_model_dumps[annotation] = annotation.model_dump
        self.nested_serializers[annotation] = annotation.__pydantic_serializer__
        self.nested_modules[annotation] = (
            sys.modules[annotation.__module__],
            annotation.__name__,
        )
        for nested_annotation in annotations.values():
            self.collect_annotation(nested_annotation)

    def verify_enum(self, official_type: type[Enum]) -> tuple[_EnumAuthority, ...]:
        module, type_name = self.enum_modules[official_type]
        member_map = self.enum_member_maps[official_type]
        members = self.enum_members[official_type]
        value_map, value_items = self.enum_value_maps[official_type]
        if (
            getattr(module, type_name, None) is not official_type
            or vars(official_type).get("_member_map_") is not member_map
            or vars(official_type).get("_value2member_map_") is not value_map
            or not self._enum_values_match(value_map, value_items)
            or not self._enum_members_match(member_map, members)
        ):
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return members

    @staticmethod
    def _enum_values_match(
        current: object, frozen: tuple[tuple[bool | str | int, Enum], ...]
    ) -> bool:
        if type(current) is not dict or len(current) != len(frozen):
            return False
        return all(
            type(current_value) is type(frozen_value)
            and current_value == frozen_value
            and current_member is frozen_member
            for (current_value, current_member), (frozen_value, frozen_member) in zip(
                current.items(), frozen, strict=True
            )
        )

    @staticmethod
    def _enum_members_match(current: object, frozen: tuple[_EnumAuthority, ...]) -> bool:
        if type(current) is not dict or len(current) != len(frozen):
            return False
        return all(
            _enum_member_matches(current_item, frozen_item)
            for current_item, frozen_item in zip(current.items(), frozen, strict=True)
        )

    def verify_reachable_enums(self) -> None:
        for official_type in self.enum_members:
            self.verify_enum(official_type)

    def verify(self, record_kind: ProviderRecordKind) -> type[object]:
        official_type = self.types[record_kind]
        exported_types = (TradeAccount, Position, Order)
        expected_types = (self.types["account"], self.types["position"], self.types["order"])
        if (
            BaseModel is not self.base
            or BaseModel.model_dump is not self.base_dump
            or OFFICIAL_PROVIDER_RECORD_TYPES is not self.registry
            or any(
                current is not expected
                for current, expected in zip(exported_types, expected_types, strict=True)
            )
            or not self._root_model_matches(record_kind, official_type)
        ):
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return official_type

    def _root_model_matches(
        self, record_kind: ProviderRecordKind, official_type: type[object]
    ) -> bool:
        return _model_authority_matches(
            official_type,
            self.fields[record_kind],
            self.annotations[record_kind],
            self.field_mappings[record_kind],
            self.model_dumps[record_kind],
            self.serializers[record_kind],
            self.modules[record_kind],
        )

    def validate(self, value: object, record_kind: ProviderRecordKind) -> frozenset[str]:
        official_type = self.verify(record_kind)
        fields = self.fields[record_kind]
        if type(value) is not official_type:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return _validate_sdk_instance(value, fields)

    def validate_nested(self, value: object, official_type: type[BaseModel]) -> None:
        fields = self.nested_fields[official_type]
        if (
            BaseModel is not self.base
            or BaseModel.model_dump is not self.base_dump
            or type(value) is not official_type
            or not _model_authority_matches(
                official_type,
                fields,
                self.nested_annotations[official_type],
                self.nested_field_mappings[official_type],
                self.nested_model_dumps[official_type],
                self.nested_serializers[official_type],
                self.nested_modules[official_type],
            )
        ):
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        _validate_sdk_instance(value, fields)

    def extract_nested_record(
        self, value: object, official_type: type[BaseModel], depth: int
    ) -> dict[str, object]:
        self.validate_nested(value, official_type)
        state = vars(value)
        result = {
            name: self.extract_value(
                state[name],
                self.nested_annotations[official_type][name],
                depth + 1,
                enforce_string_bound=True,
            )
            for name, _field in self.nested_fields[official_type]
        }
        self.validate_nested(value, official_type)
        return result

    def extract_value(
        self,
        value: object,
        annotation: object,
        depth: int,
        *,
        enforce_string_bound: bool = False,
    ) -> object:
        if depth > 8:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        origin = get_origin(annotation)
        alternatives = get_args(annotation)
        if origin in {Union, UnionType}:
            return self._extract_union(value, alternatives, depth, enforce_string_bound)
        if annotation in {type(None), bool, int, str, float, Decimal, UUID, datetime, date}:
            return _extract_sdk_scalar(value, annotation, enforce_string_bound)
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return self._extract_enum(value, annotation)
        if origin is list:
            return self._extract_list(value, alternatives, depth, enforce_string_bound)
        if origin is dict:
            return self._extract_dict(value, alternatives, depth, enforce_string_bound)
        if annotation is self.types["order"]:
            return self.extract_record(value, "order", depth + 1)
        if annotation in self.nested_fields:
            return self.extract_nested_record(value, annotation, depth + 1)
        raise RehearsalError("PROVIDER_RECORD_INVALID")

    def _extract_union(
        self,
        value: object,
        alternatives: tuple[object, ...],
        depth: int,
        enforce_string_bound: bool,
    ) -> object:
        results: list[object] = []
        for alternative in alternatives:
            try:
                results.append(
                    self.extract_value(
                        value,
                        alternative,
                        depth + 1,
                        enforce_string_bound=enforce_string_bound,
                    )
                )
            except RehearsalError as exc:
                if str(exc) in {"PROVIDER_COLLECTION_TOO_LARGE", "PROVIDER_STRING_TOO_LONG"}:
                    raise
        if len(results) != 1:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return results[0]

    def _extract_enum(self, value: object, annotation: type[Enum]) -> bool | str | int:
        members = self.verify_enum(annotation)
        if type(value) is not annotation:
            if type(value) is str and len(value) > MAX_PROVIDER_STRING:
                raise RehearsalError("PROVIDER_STRING_TOO_LONG")
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        matching = [frozen for _name, member, _member_name, frozen in members if value is member]
        if not matching:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        enum_value = matching[0]
        if type(enum_value) is str and len(enum_value) > MAX_PROVIDER_STRING:
            raise RehearsalError("PROVIDER_STRING_TOO_LONG")
        return enum_value

    def _extract_list(
        self,
        value: object,
        alternatives: tuple[object, ...],
        depth: int,
        enforce_string_bound: bool,
    ) -> list[object]:
        if type(value) is not list or len(alternatives) != 1:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        if len(value) > MAX_PROVIDER_ITEMS:
            raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
        return [
            self.extract_value(
                item,
                alternatives[0],
                depth + 1,
                enforce_string_bound=enforce_string_bound,
            )
            for item in value
        ]

    def _extract_dict(
        self,
        value: object,
        alternatives: tuple[object, ...],
        depth: int,
        enforce_string_bound: bool,
    ) -> dict[str, object]:
        if type(value) is not dict or len(alternatives) != 2:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        if len(value) > MAX_PROVIDER_ITEMS or alternatives[0] is not str:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        if any(type(key) is not str for key in value):
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        if enforce_string_bound and any(len(key) > MAX_PROVIDER_STRING for key in value):
            raise RehearsalError("PROVIDER_STRING_TOO_LONG")
        return {
            key: self.extract_value(
                item,
                alternatives[1],
                depth + 1,
                enforce_string_bound=enforce_string_bound,
            )
            for key, item in value.items()
        }

    def extract_record(
        self, value: object, record_kind: ProviderRecordKind, depth: int = 0
    ) -> dict[str, object]:
        self.validate(value, record_kind)
        self.verify_reachable_enums()
        state = vars(value)
        result = {
            name: self.extract_value(state[name], self.annotations[record_kind][name], depth + 1)
            for name, _field in self.fields[record_kind]
        }
        self.validate(value, record_kind)
        return result

    def serialize(self, value: object, record_kind: ProviderRecordKind) -> dict[str, object]:
        result = self.extract_record(value, record_kind)
        repeated = self.extract_record(value, record_kind)
        if repeated != result:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return result


def _enum_member_matches(current: tuple[object, object], frozen: _EnumAuthority) -> bool:
    current_name, current_member = current
    frozen_name, frozen_member, frozen_member_name, frozen_value = frozen
    return (
        type(current_name) is type(frozen_name)
        and current_name == frozen_name
        and current_member is frozen_member
        and type(object.__getattribute__(current_member, "_name_")) is type(frozen_member_name)
        and object.__getattribute__(current_member, "_name_") == frozen_member_name
        and type(object.__getattribute__(current_member, "_value_")) is type(frozen_value)
        and object.__getattribute__(current_member, "_value_") == frozen_value
    )


def _model_authority_matches(
    official_type: type[object],
    fields: tuple[tuple[str, object], ...],
    annotations: Mapping[str, object],
    field_mapping: object,
    model_dump: object,
    serializer: object,
    module_authority: tuple[object, str],
) -> bool:
    module, type_name = module_authority
    current_fields = official_type.model_fields
    return (
        getattr(module, type_name, None) is official_type
        and official_type.model_dump is model_dump
        and official_type.__pydantic_serializer__ is serializer
        and current_fields is field_mapping
        and len(current_fields) == len(fields)
        and all(
            current_fields.get(name) is field and field.annotation is annotations[name]
            for name, field in fields
        )
    )


def _validate_sdk_instance(value: object, fields: tuple[tuple[str, object], ...]) -> frozenset[str]:
    field_names = frozenset(name for name, _field in fields)
    state = vars(value)
    if type(state) is not dict or frozenset(state) != field_names:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
    if (
        type(fields_set) is not set
        or not fields_set <= field_names
        or object.__getattribute__(value, "__pydantic_extra__") is not None
        or object.__getattribute__(value, "__pydantic_private__") is not None
    ):
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    return field_names


def _extract_sdk_scalar(value: object, annotation: object, enforce_string_bound: bool) -> object:
    if annotation in {type(None), bool, int, str}:
        return _extract_sdk_basic(value, annotation, enforce_string_bound)
    if annotation in {float, Decimal}:
        return _extract_sdk_number(value, annotation)
    if annotation is UUID:
        if type(value) is not UUID:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return str(value)
    if annotation is datetime:
        return _extract_sdk_datetime(value)
    if type(value) is not date:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    return value.isoformat()


def _extract_sdk_basic(value: object, annotation: object, enforce_string_bound: bool) -> object:
    if annotation is type(None):
        if value is not None:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return value
    if annotation in {bool, int}:
        if type(value) is not annotation:
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return value
    if annotation is str:
        return _extract_sdk_string(value, enforce_string_bound)
    raise RehearsalError("PROVIDER_RECORD_INVALID")


def _extract_sdk_number(value: object, annotation: object) -> object:
    if annotation is float:
        if type(value) is not float or not math.isfinite(value):
            raise RehearsalError("PROVIDER_RECORD_INVALID")
        return value
    if type(value) is not Decimal or not value.is_finite():
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    return str(value)


def _extract_sdk_string(value: object, enforce_bound: bool) -> str:
    if type(value) is not str:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    if enforce_bound and len(value) > MAX_PROVIDER_STRING:
        raise RehearsalError("PROVIDER_STRING_TOO_LONG")
    return value


def _extract_sdk_datetime(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    rendered = value.isoformat()
    return f"{rendered[:-6]}Z" if rendered.endswith("+00:00") else rendered


def _capture_provider_sdk_authority() -> tuple[
    Mapping[ProviderRecordKind, type[object]],
    Callable[[object, ProviderRecordKind], frozenset[str]],
    Callable[[object, ProviderRecordKind], dict[str, object]],
]:
    authority = _ProviderSdkAuthority()

    def validate(value: object, record_kind: ProviderRecordKind) -> frozenset[str]:
        return authority.validate(value, record_kind)

    def serialize(value: object, record_kind: ProviderRecordKind) -> dict[str, object]:
        return authority.serialize(value, record_kind)

    return authority.registry, validate, serialize


(
    OFFICIAL_PROVIDER_RECORD_TYPES,
    _validate_trusted_sdk_record,
    _serialize_trusted_sdk_record,
) = _capture_provider_sdk_authority()


class Mode(StrEnum):
    DEVELOPMENT = "development"
    SUBMISSION_NO_TRADE = "submission-no-trade"


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    secret_key: str

    def __post_init__(self) -> None:
        if not self.api_key or not self.secret_key:
            raise RehearsalError("CREDENTIALS_REQUIRED")


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    payload: object
    redirect_location: str | None
    next_page_token: str | None


@dataclass(frozen=True)
class HttpOperation:
    method: str
    url: str


class TransportLedger:
    """Record the actual HTTP send boundary and reject writes before transmission."""

    def __init__(self) -> None:
        self._operations: list[HttpOperation] = []
        self._rejected_writes = 0
        self._instrumented: set[int] = set()

    @property
    def operations(self) -> tuple[HttpOperation, ...]:
        return tuple(self._operations)

    @property
    def rejected_writes(self) -> int:
        return self._rejected_writes

    def instrument_requests_session(self, session: object) -> None:
        identity = id(session)
        if identity in self._instrumented:
            return
        original = getattr(session, "send", None)
        if not callable(original):
            raise RehearsalError("PROVIDER_TRANSPORT_UNAVAILABLE")

        def send(request: object, *args: object, **kwargs: object) -> object:
            bounded_kwargs = dict(kwargs)
            for key in ("allow_redirects", "follow_redirects"):
                if key in bounded_kwargs:
                    bounded_kwargs[key] = False
            self._record(
                getattr(request, "method", None),
                getattr(request, "url", None),
                bounded_kwargs,
            )
            response = original(request, *args, **bounded_kwargs)
            status = getattr(response, "status_code", None)
            history = getattr(response, "history", ())
            if (isinstance(status, int) and 300 <= status < 400) or history:
                raise RehearsalError("PROVIDER_REDIRECT_FORBIDDEN")
            return response

        try:
            session.send = send
        except (AttributeError, TypeError):
            raise RehearsalError("PROVIDER_TRANSPORT_UNAVAILABLE") from None
        self._instrumented.add(identity)

    def _record(self, method: object, url: object, kwargs: Mapping[str, object]) -> None:
        if type(method) is not str or type(url) not in {str, URL}:
            raise RehearsalError("PROVIDER_ENDPOINT_INVALID")
        normalized_method = method.upper()
        if normalized_method not in READ_HTTP_METHODS:
            self._rejected_writes += 1
            raise RehearsalError("MUTATING_HTTP_METHOD_FORBIDDEN")
        if kwargs.get("allow_redirects") is True or kwargs.get("follow_redirects") is True:
            raise RehearsalError("PROVIDER_REDIRECT_FORBIDDEN")
        normalized_url = url if type(url) is str else str(url)
        if not normalized_url or len(normalized_url.encode()) > 2_048:
            raise RehearsalError("PROVIDER_ENDPOINT_INVALID")
        try:
            parsed = urlsplit(normalized_url)
            port = parsed.port
        except ValueError:
            raise RehearsalError("PROVIDER_ENDPOINT_INVALID") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"paper-api.alpaca.markets", "data.alpaca.markets"}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or port is not None
            or (parsed.hostname, parsed.path) not in ALLOWED_PROVIDER_READ_PATHS
        ):
            raise RehearsalError("PROVIDER_ENDPOINT_INVALID")
        if len(self._operations) >= MAX_TRANSPORT_OPERATIONS:
            raise RehearsalError("PROVIDER_OPERATION_LIMIT_EXCEEDED")
        self._operations.append(HttpOperation(normalized_method, normalized_url))


class ActivityHttpBoundary:
    """Constrain the injected HTTP client to one exact read-only activity endpoint."""

    def __init__(
        self,
        client: object,
        ledger: TransportLedger,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._headers = None if headers is None else dict(headers)
        ledger.instrument_requests_session(client)

    def request(self, **kwargs: object) -> HttpResponse:
        if kwargs.get("method") != "GET" or kwargs.get("follow_redirects") is not False:
            raise RehearsalError("MUTATING_HTTP_METHOD_FORBIDDEN")
        if kwargs.get("url") != f"{PAPER_ENDPOINT}/v2/account/activities":
            raise RehearsalError("ACTIVITY_ENDPOINT_INVALID")
        if "headers" in kwargs:
            raise RehearsalError("ACTIVITY_HEADERS_INVALID")
        if self._headers is not None:
            kwargs["headers"] = self._headers
        response = self._client.request(**kwargs)
        if type(response) is HttpResponse:
            if (
                type(response.status_code) is not int
                or type(response.payload) is not list
                or type(response.redirect_location) not in {str, type(None)}
                or type(response.next_page_token) not in {str, type(None)}
            ):
                raise RehearsalError("ACTIVITY_RESPONSE_INVALID")
            return response
        history = getattr(response, "history", ())
        location = getattr(response, "headers", {}).get("location")
        if history:
            location = location or "redirect-followed"
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise RehearsalError("ACTIVITY_RESPONSE_INVALID") from None
        status_code = getattr(response, "status_code", None)
        if type(status_code) is not int:
            raise RehearsalError("ACTIVITY_RESPONSE_INVALID")
        return HttpResponse(status_code, payload, location, None)


@dataclass(frozen=True)
class BookCapture:
    role: str
    account_fingerprint: str
    account: Mapping[str, object]
    positions: tuple[Mapping[str, object], ...]
    orders: tuple[Mapping[str, object], ...]
    activities: tuple[Mapping[str, object], ...]
    sdk_trace: tuple[str, ...]
    http_trace: tuple[str, ...]
    pagination_terminal: bool


class TradingClient(Protocol):
    def get_account(self) -> object: ...

    def get_all_positions(self) -> Sequence[object]: ...

    def get_orders(self, request: object | None = None) -> Sequence[object]: ...


class TradingBoundary:
    """Own every operation used to capture a complete read-only broker book."""

    def __init__(
        self,
        *,
        client_factory: Callable[[Credentials], TradingClient],
        activity_http: ActivityHttpBoundary,
        endpoint: str,
        ledger: TransportLedger,
        role: AccountRole,
    ) -> None:
        if endpoint != PAPER_ENDPOINT:
            raise RehearsalError("PAPER_ENDPOINT_REQUIRED")
        self._client_factory = client_factory
        self._activity_http = activity_http
        self._endpoint = endpoint
        self._ledger = ledger
        if role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}:
            raise RehearsalError("REHEARSAL_ROLE_INVALID")
        self._role = role
        self._sdk_trace: list[str] = []
        self._http_trace: list[str] = []
        self._client: TradingClient | None = None
        self._credential_binding: bytes | None = None

    @property
    def transport(self) -> TransportLedger:
        return self._ledger

    @property
    def role(self) -> AccountRole:
        return self._role

    def capture(self, credentials: Credentials) -> BookCapture:
        self._sdk_trace = []
        self._http_trace = []
        binding = hashlib.sha256(
            credentials.api_key.encode() + b"\0" + credentials.secret_key.encode()
        ).digest()
        if self._client is None:
            self._client = self._client_factory(credentials)
            self._credential_binding = binding
            self._ledger.instrument_requests_session(getattr(self._client, "_session", None))
        elif self._credential_binding != binding:
            raise RehearsalError("TRADING_CREDENTIAL_BINDING_CHANGED")
        client = self._client
        account = self._read(client, "get_account")
        positions = self._read(client, "get_all_positions")
        orders = self._read(
            client,
            "get_orders",
            GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                limit=500,
                direction=Sort.DESC,
                nested=True,
            ),
        )
        if type(orders) in {list, tuple} and len(orders) >= 500:
            raise RehearsalError("ORDER_CAPTURE_INCOMPLETE")
        activities = self._activities()
        _validate_sdk_record(account, "account")
        position_records = _validate_sdk_collection(positions, "position")
        order_records = _validate_sdk_orders(orders)
        account_row = _dump_sdk_record(account, "account")
        normalized_account = _normalize_account(account_row)
        normalized_positions = _normalize_collection(
            tuple(_dump_sdk_record(position, "position") for position in position_records),
            _normalize_position,
            ("asset_id",),
        )
        normalized_orders = _normalize_collection(
            tuple(_dump_sdk_order(order) for order in order_records),
            _normalize_root_order,
            ("id",),
        )
        order_ids: set[str] = set()
        for order in normalized_orders:
            identities = [order["id"], *(leg["id"] for leg in order["legs"])]
            if any(identity in order_ids for identity in identities) or len(identities) != len(
                set(identities)
            ):
                raise RehearsalError("PROVIDER_DUPLICATE_ITEM")
            order_ids.update(identities)
        normalized_activities = _normalize_collection(activities, _normalize_activity, ("id",))
        try:
            observed_account_value = UUID(
                _bounded_string(account_row.get("id"), required=True)
            )
        except ValueError:
            raise RehearsalError("PROVIDER_ACCOUNT_ID_INVALID") from None
        fingerprint = hashlib.sha256(f"{observed_account_value}\n".encode()).hexdigest()
        return BookCapture(
            role=self._role.value,
            account_fingerprint=fingerprint,
            account=MappingProxyType(normalized_account),
            positions=normalized_positions,
            orders=normalized_orders,
            activities=normalized_activities,
            sdk_trace=tuple(self._sdk_trace),
            http_trace=tuple(self._http_trace),
            pagination_terminal=True,
        )

    def _read(self, client: TradingClient, method: str, *args: object) -> object:
        if method not in {"get_account", "get_all_positions", "get_orders"}:
            raise RehearsalError("PROVIDER_WRITE_METHOD_FORBIDDEN")
        self._sdk_trace.append(method)
        try:
            return getattr(client, method)(*args)
        except (AttributeError, OSError, TypeError, ValueError):
            raise RehearsalError("TRADING_CAPTURE_FAILED") from None

    def _activities(self) -> list[object]:
        activities: list[object] = []
        page_cursor: str | None = None
        for _page in range(MAX_ACTIVITY_PAGES):
            self._http_trace.append("GET")
            try:
                response = self._activity_http.request(
                    method="GET",
                    url=f"{self._endpoint}/v2/account/activities",
                    params={
                        "page_size": ACTIVITY_PAGE_SIZE,
                        "page_token": page_cursor,
                        "direction": "desc",
                    },
                    follow_redirects=False,
                )
            except (OSError, TypeError, ValueError):
                raise RehearsalError("TRADING_CAPTURE_FAILED") from None
            if response.redirect_location is not None or 300 <= response.status_code < 400:
                raise RehearsalError("PROVIDER_REDIRECT_FORBIDDEN")
            if response.status_code != 200 or type(response.payload) is not list:
                raise RehearsalError("ACTIVITY_RESPONSE_INVALID")
            if len(response.payload) > ACTIVITY_PAGE_SIZE:
                raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
            activities.extend(response.payload)
            if len(activities) > MAX_PROVIDER_ITEMS:
                raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
            if len(response.payload) < ACTIVITY_PAGE_SIZE:
                return activities
            next_cursor = response.next_page_token
            if next_cursor is None and response.payload:
                next_cursor = _bounded_string(
                    _record(response.payload[-1]).get("id"), required=True
                )
            if next_cursor == page_cursor:
                raise RehearsalError("ACTIVITY_PAGINATION_INCOMPLETE")
            page_cursor = _bounded_string(next_cursor, required=True)
        raise RehearsalError("ACTIVITY_PAGINATION_INCOMPLETE")


def _record(value: object) -> dict[str, object]:
    if type(value) is dict:
        result = value.copy()
    else:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    if type(result) is dict and len(result) <= MAX_PROVIDER_ITEMS:
        return result
    raise RehearsalError("PROVIDER_RECORD_INVALID")


def _validate_sdk_record(value: object, record_kind: ProviderRecordKind) -> None:
    _validate_trusted_sdk_record(value, record_kind)


def _validate_sdk_collection(values: object, record_kind: ProviderRecordKind) -> tuple[object, ...]:
    if type(values) not in {list, tuple}:
        raise RehearsalError("PROVIDER_COLLECTION_INVALID")
    if len(values) > MAX_PROVIDER_ITEMS:
        raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
    result = tuple(values)
    for value in result:
        _validate_sdk_record(value, record_kind)
    return result


def _validate_sdk_orders(values: object) -> tuple[object, ...]:
    orders = _validate_sdk_collection(values, "order")
    for order in orders:
        legs = order.legs
        if legs is None:
            continue
        if type(legs) not in {list, tuple} or len(legs) > 4:
            raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
        for leg in legs:
            _validate_sdk_record(leg, "order")
            nested_legs = leg.legs
            if nested_legs is not None and type(nested_legs) not in {list, tuple}:
                raise RehearsalError("PROVIDER_COLLECTION_INVALID")
            if nested_legs:
                raise RehearsalError("PROVIDER_ORDER_LEG_TREE_INVALID")
    return orders


def _dump_sdk_record(value: object, record_kind: ProviderRecordKind) -> dict[str, object]:
    _validate_sdk_record(value, record_kind)
    identity_field = "asset_id" if record_kind == "position" else "id"
    expected_identity = object.__getattribute__(value, identity_field)
    if type(expected_identity) is not UUID:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    result = _serialize_trusted_sdk_record(value, record_kind)
    _validate_sdk_record(value, record_kind)
    if type(result) is not dict or len(result) > MAX_PROVIDER_ITEMS:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    serialized_identity = result.get(identity_field)
    if (
        object.__getattribute__(value, identity_field) != expected_identity
        or type(serialized_identity) is not str
        or serialized_identity != str(expected_identity)
    ):
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    return result


def _dump_sdk_order(value: object) -> dict[str, object]:
    expected_legs, expected_leg_ids = _sdk_order_legs(value)
    result = _dump_sdk_record(value, "order")
    actual_legs = _serialized_sdk_order_legs(result)
    if len(actual_legs) != len(expected_legs):
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    for expected_leg, expected_id, serialized_leg in zip(
        expected_legs, expected_leg_ids, actual_legs, strict=True
    ):
        _validate_serialized_sdk_leg(expected_leg, expected_id, serialized_leg)
    _validate_sdk_orders((value,))
    _validate_unchanged_sdk_legs(value, expected_legs)
    return result


def _sdk_order_legs(value: object) -> tuple[tuple[object, ...], tuple[object, ...]]:
    raw_legs = object.__getattribute__(value, "legs")
    expected_legs = () if raw_legs is None else tuple(raw_legs)
    expected_leg_ids = tuple(object.__getattribute__(leg, "id") for leg in expected_legs)
    if any(type(identity) is not UUID for identity in expected_leg_ids):
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    return expected_legs, expected_leg_ids


def _serialized_sdk_order_legs(result: dict[str, object]) -> list[object]:
    serialized_legs = result.get("legs")
    if serialized_legs is None:
        return []
    if type(serialized_legs) is not list:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    return serialized_legs


def _validate_serialized_sdk_leg(
    expected_leg: object, expected_id: object, serialized_leg: object
) -> None:
    _validate_sdk_record(expected_leg, "order")
    if type(serialized_leg) is not dict:
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    if serialized_leg != _serialize_trusted_sdk_record(expected_leg, "order"):
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    if (
        object.__getattribute__(expected_leg, "id") != expected_id
        or type(serialized_leg.get("id")) is not str
        or serialized_leg["id"] != str(expected_id)
    ):
        raise RehearsalError("PROVIDER_RECORD_INVALID")
    nested_legs = serialized_leg.get("legs")
    if nested_legs is not None and (type(nested_legs) is not list or nested_legs):
        raise RehearsalError("PROVIDER_RECORD_INVALID")


def _validate_unchanged_sdk_legs(value: object, expected_legs: tuple[object, ...]) -> None:
    current_raw_legs = object.__getattribute__(value, "legs")
    current_legs = () if current_raw_legs is None else tuple(current_raw_legs)
    if len(current_legs) != len(expected_legs) or any(
        current is not expected
        for current, expected in zip(current_legs, expected_legs, strict=True)
    ):
        raise RehearsalError("PROVIDER_RECORD_INVALID")


def _bounded_string(value: object, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if type(value) not in {str, int}:
        raise RehearsalError("PROVIDER_STRING_INVALID")
    result = str(value)
    if (required and not result) or len(result) > MAX_PROVIDER_STRING:
        code = (
            "PROVIDER_STRING_TOO_LONG"
            if len(result) > MAX_PROVIDER_STRING
            else "PROVIDER_STRING_INVALID"
        )
        raise RehearsalError(code)
    return result


def _number(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    encoded = _provider_number_bytes(value)
    result = _provider_decimal(encoded)
    _validate_provider_decimal(result)
    return _render_provider_decimal(result)


def _provider_number_bytes(value: object) -> bytes:
    if type(value) not in {str, int, Decimal}:
        raise RehearsalError("PROVIDER_NUMBER_INVALID")
    if type(value) is int and value.bit_length() > 128:
        raise RehearsalError("PROVIDER_NUMBER_INVALID")
    try:
        encoded = str(value).encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise RehearsalError("PROVIDER_NUMBER_INVALID") from None
    if len(encoded) > 72 or PROVIDER_NUMBER.fullmatch(encoded.decode()) is None:
        raise RehearsalError("PROVIDER_NUMBER_INVALID")
    return encoded


def _provider_decimal(encoded: bytes) -> Decimal:
    try:
        return Decimal(encoded.decode())
    except (InvalidOperation, TypeError, ValueError):
        raise RehearsalError("PROVIDER_NUMBER_INVALID") from None


def _validate_provider_decimal(result: Decimal) -> None:
    magnitude = abs(result)
    if (
        not result.is_finite()
        or magnitude > MAX_PROVIDER_ABS
        or (magnitude != 0 and magnitude < MIN_PROVIDER_ABS)
    ):
        raise RehearsalError("PROVIDER_NUMBER_INVALID")


def _render_provider_decimal(result: Decimal) -> str:
    rendered = format(result, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0", "+0"}:
        rendered = "0"
    if len(rendered.encode("ascii")) > MAX_PROVIDER_NUMBER_RENDER:
        raise RehearsalError("PROVIDER_NUMBER_INVALID")
    return rendered


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise RehearsalError("PROVIDER_BOOLEAN_INVALID")
    return value


def _optional_boolean(value: object) -> bool | None:
    return None if value is None else _boolean(value)


def _normalize_account(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": _bounded_string(row.get("status"), required=True),
        "currency": _bounded_string(row.get("currency")),
        "crypto_status": _bounded_string(row.get("crypto_status")),
        "created_at": _bounded_string(row.get("created_at")),
        "account_number_hash": hashlib.sha256(
            _bounded_string(row.get("account_number")).encode()
        ).hexdigest(),
        **{
            key: _number(row.get(key), optional=key != "equity")
            for key in (
                "equity",
                "cash",
                "last_equity",
                "portfolio_value",
                "long_market_value",
                "short_market_value",
                "initial_margin",
                "maintenance_margin",
                "last_maintenance_margin",
                "sma",
                "daytrade_count",
                "buying_power",
                "daytrading_buying_power",
                "regt_buying_power",
                "non_marginable_buying_power",
                "options_buying_power",
                "pending_transfer_in",
                "pending_transfer_out",
                "accrued_fees",
                "multiplier",
                "options_approved_level",
                "options_trading_level",
            )
        },
        "trading_blocked": _boolean(row.get("trading_blocked")),
        "transfers_blocked": _boolean(row.get("transfers_blocked")),
        "account_blocked": _boolean(row.get("account_blocked")),
        "trade_suspended_by_user": _boolean(row.get("trade_suspended_by_user")),
        "shorting_enabled": _optional_boolean(row.get("shorting_enabled")),
        "pattern_day_trader": _optional_boolean(row.get("pattern_day_trader")),
    }


def _normalize_position(row: Mapping[str, object]) -> dict[str, object]:
    return (
        {
            key: _bounded_string(row.get(key), required=key in {"asset_id", "symbol"})
            for key in (
                "asset_id",
                "symbol",
                "asset_class",
                "exchange",
                "side",
                "contract_type",
                "expiration_date",
            )
        }
        | {
            key: _number(row.get(key), optional=True)
            for key in (
                "qty",
                "qty_available",
                "avg_entry_price",
                "current_price",
                "market_value",
                "cost_basis",
                "unrealized_pl",
                "unrealized_plpc",
                "unrealized_intraday_pl",
                "unrealized_intraday_plpc",
                "lastday_price",
                "change_today",
                "strike_price",
                "multiplier",
                "avg_entry_swap_rate",
                "swap_rate",
            )
        }
        | {"asset_marginable": _optional_boolean(row.get("asset_marginable"))}
    )


def _normalize_root_order(row: Mapping[str, object]) -> dict[str, object]:
    return _normalize_order(row, allow_legs=True)


def _normalize_leaf_order(row: Mapping[str, object]) -> dict[str, object]:
    return _normalize_order(row, allow_legs=False)


def _normalize_order(row: Mapping[str, object], *, allow_legs: bool) -> dict[str, object]:
    raw_legs = row.get("legs")
    legs = [] if raw_legs is None else raw_legs
    if type(legs) not in {list, tuple} or len(legs) > 4:
        raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
    if not allow_legs and legs:
        raise RehearsalError("PROVIDER_ORDER_LEG_TREE_INVALID")
    return (
        {
            key: _bounded_string(row.get(key), required=key == "id")
            for key in (
                "id",
                "client_order_id",
                "symbol",
                "asset_id",
                "asset_class",
                "status",
                "order_class",
                "type",
                "order_type",
                "position_intent",
                "side",
                "time_in_force",
                "submitted_at",
                "created_at",
                "updated_at",
                "filled_at",
                "expired_at",
                "canceled_at",
                "failed_at",
                "expires_at",
                "replaced_at",
                "replaced_by",
                "replaces",
            )
        }
        | {
            key: _number(row.get(key), optional=True)
            for key in (
                "qty",
                "filled_qty",
                "limit_price",
                "stop_price",
                "filled_avg_price",
                "notional",
                "trail_price",
                "trail_percent",
                "ratio_qty",
                "hwm",
            )
        }
        | {
            "extended_hours": _optional_boolean(row.get("extended_hours")),
            "legs": _normalize_collection(
                legs,
                _normalize_leaf_order,
                ("id",),
            ),
        }
    )


def _normalize_activity(row: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _bounded_string(row.get(key), required=key in {"id", "activity_type"})
        for key in (
            "id",
            "account_id",
            "activity_type",
            "transaction_time",
            "date",
            "symbol",
            "side",
            "order_id",
            "client_order_id",
            "type",
            "status",
            "order_status",
            "description",
        )
    } | {
        key: _number(row.get(key), optional=True)
        for key in (
            "qty",
            "price",
            "net_amount",
            "per_share_amount",
            "cum_qty",
            "leaves_qty",
        )
    }


def _normalize_collection(
    values: object,
    normalize: Callable[[Mapping[str, object]], dict[str, object]],
    identity_fields: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    if type(values) not in {list, tuple}:
        raise RehearsalError("PROVIDER_COLLECTION_INVALID")
    if len(values) > MAX_PROVIDER_ITEMS:
        raise RehearsalError("PROVIDER_COLLECTION_TOO_LARGE")
    result: list[Mapping[str, object]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        row = _record(value)
        identity = tuple(_bounded_string(row.get(field)) for field in identity_fields)
        if identity in seen:
            raise RehearsalError("PROVIDER_DUPLICATE_ITEM")
        seen.add(identity)
        result.append(MappingProxyType(normalize(row)))
    return tuple(sorted(result, key=lambda item: _canonical_json(_json_safe(item))))


@dataclass(frozen=True)
class CliPin:
    version: str
    archive_sha256: str
    binary_sha256: str
    archive_member: str


PRODUCTION_CLI_PIN = CliPin(CLI_VERSION, ARCHIVE_SHA256, BINARY_SHA256, "alpaca")


class ProcessPort(Protocol):
    def run_fd(
        self, descriptor: int, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> str: ...


class BoundedSubprocess:
    def run_fd(self, descriptor: int, argv: tuple[str, ...], environment: Mapping[str, str]) -> str:
        executable = f"/proc/self/fd/{descriptor}"
        if argv[0] != executable:
            raise RehearsalError("CLI_EXECUTABLE_MISMATCH")
        try:
            process = subprocess.Popen(
                list(argv),
                executable=executable,
                env=dict(environment),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                pass_fds=(descriptor,),
                start_new_session=True,
            )
            assert process.stdout is not None
            output = bytearray()
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RehearsalError("CLI_PROCESS_FAILED")
                    if not selector.select(remaining):
                        raise RehearsalError("CLI_PROCESS_FAILED")
                    chunk = os.read(process.stdout.fileno(), 16 * 1024)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_CLI_OUTPUT:
                        raise RehearsalError("CLI_OUTPUT_TOO_LARGE")
            if process.wait(timeout=max(deadline - time.monotonic(), 0.1)) != 0:
                raise RehearsalError("CLI_PROCESS_FAILED")
            return bytes(output).decode("utf-8", errors="strict")
        except RehearsalError:
            if "process" in locals() and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise
        except (OSError, UnicodeError, subprocess.SubprocessError):
            if "process" in locals() and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise RehearsalError("CLI_PROCESS_FAILED") from None


class VerifiedCli:
    def __init__(self, descriptor: int, pin: CliPin, process: ProcessPort) -> None:
        self._descriptor = descriptor
        self._pin = pin
        self._process = process
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @classmethod
    def from_archive(cls, archive: Path, pin: CliPin, process: ProcessPort) -> VerifiedCli:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(archive, flags)
        except OSError:
            raise RehearsalError("CLI_ARCHIVE_INVALID") from None
        try:
            archive_bytes = _read_fd(descriptor, MAX_ARCHIVE_BYTES)
        finally:
            os.close(descriptor)
        if hashlib.sha256(archive_bytes).hexdigest() != pin.archive_sha256:
            raise RehearsalError("CLI_ARCHIVE_DIGEST_MISMATCH")
        try:
            with tarfile.open(fileobj=_BytesReader(archive_bytes), mode="r:*") as bundle:
                members = bundle.getmembers()
                matches = [member for member in members if member.name == pin.archive_member]
                if len(matches) != 1 or not matches[0].isfile():
                    raise RehearsalError("CLI_ARCHIVE_MEMBER_INVALID")
                source = bundle.extractfile(matches[0])
                if source is None:
                    raise RehearsalError("CLI_ARCHIVE_MEMBER_INVALID")
                binary_bytes = source.read(MAX_ARCHIVE_BYTES + 1)
        except RehearsalError:
            raise
        except (OSError, tarfile.TarError):
            raise RehearsalError("CLI_ARCHIVE_INVALID") from None
        if (
            len(binary_bytes) > MAX_ARCHIVE_BYTES
            or hashlib.sha256(binary_bytes).hexdigest() != pin.binary_sha256
        ):
            raise RehearsalError("CLI_BINARY_DIGEST_MISMATCH")
        with tempfile.TemporaryFile() as binary:
            binary.write(binary_bytes)
            binary.flush()
            os.fchmod(binary.fileno(), 0o500)
            descriptor = os.open(f"/proc/self/fd/{binary.fileno()}", os.O_RDONLY)
        return cls(descriptor, pin, process)

    def probe(self, credentials: Credentials) -> dict[str, object]:
        if self._closed:
            raise RehearsalError("CLI_ALREADY_CLOSED")
        try:
            return self._probe(credentials)
        finally:
            self.close()

    def _probe(self, credentials: Credentials) -> dict[str, object]:
        version = self._run(("version",), _child_environment(credentials)).strip()
        if version != self._pin.version:
            raise RehearsalError("CLI_VERSION_MISMATCH")
        doctor_output = self._run(("doctor", "--quiet"), _child_environment(credentials))
        if not _doctor_verified(doctor_output):
            raise RehearsalError("CLI_PAPER_HOST_NOT_VERIFIED")
        dry_run = _json_object(
            self._run(_dry_run_args(), _child_environment(credentials)),
            "CLI_DRY_RUN_INVALID",
        )
        allowed = set(_expected_dry_run()) | {"advanced_instructions"}
        if set(dry_run) - allowed or any(
            dry_run.get(key) != value for key, value in _expected_dry_run().items()
        ):
            raise RehearsalError("CLI_DRY_RUN_INVALID")
        if dry_run.get("advanced_instructions") not in (None, [], {}, ""):
            raise RehearsalError("CLI_DRY_RUN_INVALID")
        return {
            "version": self._pin.version,
            "archive_sha256": self._pin.archive_sha256,
            "binary_sha256": self._pin.binary_sha256,
            "paper_host_verified": True,
            "dry_run": True,
        }

    def _run(self, arguments: tuple[str, ...], environment: Mapping[str, str]) -> str:
        if (
            set(environment)
            != {
                "ALPACA_API_KEY",
                "ALPACA_SECRET_KEY",
                "ALPACA_LIVE_TRADE",
                "LC_ALL",
            }
            or environment["ALPACA_LIVE_TRADE"] != "false"
        ):
            raise RehearsalError("CLI_ENVIRONMENT_INVALID")
        descriptor = self._descriptor
        if (
            hashlib.sha256(_read_fd(descriptor, MAX_ARCHIVE_BYTES)).hexdigest()
            != self._pin.binary_sha256
        ):
            raise RehearsalError("CLI_BINARY_CHANGED")
        executable = f"/proc/self/fd/{descriptor}"
        output = self._process.run_fd(descriptor, (executable, *arguments), environment)
        if len(output.encode()) > MAX_CLI_OUTPUT:
            raise RehearsalError("CLI_OUTPUT_TOO_LARGE")
        return output

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True


def _child_environment(credentials: Credentials) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "ALPACA_API_KEY": credentials.api_key,
            "ALPACA_SECRET_KEY": credentials.secret_key,
            "ALPACA_LIVE_TRADE": "false",
            "LC_ALL": "C.UTF-8",
        }
    )


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        import io

        self._value = io.BytesIO(value)

    def __getattr__(self, name: str) -> object:
        return getattr(self._value, name)


def _read_fd(descriptor: int, limit: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    result = bytearray()
    while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - len(result))):
        result.extend(chunk)
        if len(result) > limit:
            raise RehearsalError("CLI_RELEASE_FILE_TOO_LARGE")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return bytes(result)


def _json_object(value: str, code: str) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise RehearsalError(code)
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
    except RehearsalError:
        raise
    except (TypeError, ValueError):
        raise RehearsalError(code) from None
    if not isinstance(parsed, dict):
        raise RehearsalError(code)
    return parsed


def _doctor_verified(value: str) -> bool:
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        return False
    lines = tuple(line.strip() for line in value.splitlines() if line.strip())
    required = (
        "Alpaca CLI 0.0.13",
        "✓ no saved profiles configured (using env var credentials)",
        "✓ active profile: paper",
        "✓ API key credentials from env (ALPACA_API_KEY + ALPACA_SECRET_KEY)",
        "Connectivity:",
        f"Trading:  {PAPER_ENDPOINT}",
        "✓ trading API: connected",
        "Data:     https://data.alpaca.markets",
        "✓ data API: connected",
        "All checks passed.",
    )
    positions: list[int] = []
    for expected in required:
        matches = [index for index, line in enumerate(lines) if line == expected]
        if len(matches) != 1:
            return False
        positions.append(matches[0])
    profile_lines = {required[1], required[2]}
    credential_line = required[3]
    allowed_authentication_lines = {required[1], credential_line}
    trading_url_line = required[5]
    trading_connected_line = required[6]
    data_url_line = required[7]
    data_connected_line = required[8]
    allowed_urls = {PAPER_ENDPOINT, "https://data.alpaca.markets"}
    failure_markers = re.compile(
        r"\b(?:denied|disabled|disconnected|error|failed|failure|missing|skipped|"
        r"unavailable|unknown|warn|warning)\b|\bnot\s+ok\b",
        re.IGNORECASE,
    )
    authority_lines_valid = all(
        (not line.casefold().startswith("trading:") or line == trading_url_line)
        and (not line.casefold().startswith("data:") or line == data_url_line)
        and ("profile" not in line.casefold() or line in profile_lines)
        and ("credential" not in line.casefold() or line in allowed_authentication_lines)
        and (
            not any(
                marker in line.casefold()
                for marker in (
                    "api key credentials from",
                    "oauth token from",
                    "credentials configured",
                )
            )
            or line == credential_line
        )
        and ("trading api:" not in line.casefold() or line == trading_connected_line)
        and ("data api:" not in line.casefold() or line == data_connected_line)
        and re.search(r"\blive\b", line, flags=re.IGNORECASE) is None
        and "oauth" not in line.casefold()
        and ("alpaca.markets" not in line.casefold() or line in {trading_url_line, data_url_line})
        and failure_markers.search(line) is None
        and all(
            url.casefold() in allowed_urls
            for url in re.findall(r"https?://[^\s]+", line, flags=re.IGNORECASE)
        )
        for line in lines
    )
    return (
        positions == sorted(positions)
        and lines[-1] == required[-1]
        and authority_lines_valid
        and not any(line.startswith("✗") or line == "some checks failed" for line in lines)
    )


def _expected_dry_run() -> dict[str, object]:
    return {
        "client_order_id": "dry",
        "legs": [
            {"symbol": "ZZZZ991231C00001000", "ratio_qty": "1", "position_intent": "buy_to_open"},
            {"symbol": "ZZZZ991231C00002000", "ratio_qty": "1", "position_intent": "sell_to_open"},
        ],
        "limit_price": "0.01",
        "order_class": "mleg",
        "qty": "1",
        "time_in_force": "day",
        "type": "limit",
    }


def _dry_run_args() -> tuple[str, ...]:
    expected = _expected_dry_run()
    return (
        "order",
        "submit",
        "--quiet",
        "--order-class",
        "mleg",
        "--qty",
        "1",
        "--type",
        "limit",
        "--time-in-force",
        "day",
        "--limit-price",
        "0.01",
        "--client-order-id",
        str(expected["client_order_id"]),
        "--legs",
        json.dumps(expected["legs"], separators=(",", ":")),
        "--dry-run",
    )


@dataclass(frozen=True)
class MCPEvidence:
    tool_surface_count: int
    call_trace: tuple[tuple[str, Mapping[str, object]], ...]
    duration_ms: int
    result_summary_hash: str


class MCPBoundary:
    def __init__(
        self,
        client_factory: Callable[[], object],
        *,
        now: Callable[[], datetime],
        owns_client_lifecycle: bool = True,
    ) -> None:
        self._client_factory = client_factory
        self._now = now
        self._owns_client_lifecycle = owns_client_lifecycle

    @classmethod
    def from_retained_client(
        cls,
        client: object,
        *,
        now: Callable[[], datetime],
    ) -> MCPBoundary:
        return cls(
            lambda: client,
            now=now,
            owns_client_lifecycle=False,
        )

    @classmethod
    def from_official_client(
        cls,
        credentials: Credentials,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> MCPBoundary:
        return cls(
            lambda: AlpacaMCPResearchClient(
                api_key=credentials.api_key,
                secret_key=credentials.secret_key,
                now=now,
            ),
            now=now,
        )

    async def probe(self) -> MCPEvidence:
        client = self._client_factory()
        started = self._now()
        try:
            if self._owns_client_lifecycle:
                async with client as entered:
                    result = await entered.call("get_clock", {})
            else:
                result = await client.call("get_clock", {})
        except (OSError, RuntimeError, TypeError, ValueError):
            raise RehearsalError("MCP_PROBE_FAILED") from None
        elapsed = int((self._now() - started).total_seconds() * 1000)
        digest = getattr(getattr(result, "audit", None), "result_summary_hash", None)
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RehearsalError("MCP_PROBE_FAILED")
        return MCPEvidence(len(EXPOSED_TOOL_SURFACE), (("get_clock", {}),), max(elapsed, 0), digest)


class SafetyCounters:
    def __init__(self) -> None:
        self._values = {
            "mcp_sessions": 0,
            "mcp_calls": 0,
            "model_calls": 0,
            "repository_calls": 0,
            "runtime_execution_calls": 0,
        }

    def record(self, name: str) -> None:
        if name not in self._values:
            raise RehearsalError("PROVIDER_COUNTER_INVALID")
        self._values[name] += 1

    def snapshot(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self._values))


class _CountedMCPClient:
    def __init__(self, delegate: object, counters: SafetyCounters) -> None:
        self._delegate = delegate
        self._counters = counters
        self._closed = False

    async def __aenter__(self):
        if self._closed:
            raise RehearsalError("MCP_RESOURCE_ALREADY_CLOSED")
        self._counters.record("mcp_sessions")
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if not self._closed:
            self._closed = True
            await self._delegate.__aexit__(*args)

    async def call(self, tool_name: str, arguments: Mapping[str, object]):
        self._counters.record("mcp_calls")
        return await self._delegate.call(tool_name, arguments)


class _CountedModelTransport:
    def __init__(self, delegate: object, counters: SafetyCounters) -> None:
        self._delegate = delegate
        self._counters = counters

    def generate(self, *args: object, **kwargs: object):
        self._counters.record("model_calls")
        return self._delegate.generate(*args, **kwargs)

    def close(self) -> None:
        self._delegate.close()


@dataclass(frozen=True)
class FixtureSubmissionEvidence:
    terminal_code: str
    counts: Mapping[str, int]
    account_fingerprint: str


class _CountedAuthority:
    def __init__(self, delegate: object, counts: dict[str, int]) -> None:
        self._delegate = delegate
        self._counts = counts
        self.observed: object | None = None

    def observe(self):
        self._counts["account_authority"] += 1
        self.observed = self._delegate.observe()
        return self.observed


class _CountedAcquisition:
    def __init__(self, delegate: object, counts: dict[str, int]) -> None:
        self._delegate = delegate
        self._counts = counts

    async def acquire(self, *args: object, **kwargs: object) -> object:
        self._counts["acquisition"] += 1
        result = await self._delegate.acquire(*args, **kwargs)
        return result


class _NeverSubmissionAcquisition:
    async def acquire(self, *_args: object) -> object:
        raise RehearsalError("SUBMISSION_ACQUISITION_REACHED")


class _CountedDecisions:
    def __init__(self, delegate: AgentDecisionRepository, counts: dict[str, int]) -> None:
        self._delegate = delegate
        self._counts = counts

    def begin_tick(self, *args: object):
        self._counts["repository"] += 1
        return self._delegate.begin_tick(*args)

    def permanent_latch(self, *args: object):
        self._counts["repository"] += 1
        return self._delegate.permanent_latch(*args)

    def persist_decision(self, tick: AgentTick, decision: AgentDecision, proposal: object):
        self._counts["repository"] += 1
        if proposal is not None:
            self._counts["proposal"] += 1
        persisted: PersistedAgentDecision = self._delegate.persist_decision(
            tick, decision, proposal
        )
        if persisted.approved_intent is not None:
            self._counts["intent"] += 1
        return persisted

    def complete_tick(self, *args: object):
        self._counts["repository"] += 1
        return self._delegate.complete_tick(*args)


class _CountedExecution:
    def __init__(self, delegate: object, counts: dict[str, int]) -> None:
        self._delegate = delegate
        self._counts = counts

    def execute(self, *args: object):
        self._counts["execution"] += 1
        self._counts["provider_write"] += 1
        return self._delegate.execute(*args)


class _CountedRuntime:
    def __init__(self, delegate: RuntimeCompositionPort, counts: dict[str, int]) -> None:
        self.execution = _CountedExecution(delegate.execution, counts)


class FixtureSubmissionBoundary:
    def __init__(
        self,
        *,
        account_authority: object,
        clock: object,
        calibration: object,
        decisions: AgentDecisionRepository,
        runtime: RuntimeCompositionPort,
        server_autonomy_enabled: bool = False,
    ) -> None:
        counts = {
            key: 0
            for key in (
                "acquisition",
                "mcp",
                "gemini",
                "proposal",
                "intent",
                "execution",
                "provider_write",
                "account_authority",
                "repository",
            )
        }
        self._counts = counts
        self._authority = _CountedAuthority(account_authority, counts)
        self._service = AgentRunService(
            account_authority=self._authority,
            clock=clock,
            calibration=calibration,
            acquisition=_CountedAcquisition(_NeverSubmissionAcquisition(), counts),
            decisions=_CountedDecisions(decisions, counts),
            runtime=_CountedRuntime(runtime, counts),
            server_autonomy_enabled=server_autonomy_enabled,
        )

    async def probe(self) -> FixtureSubmissionEvidence:
        result = await self._service.run(Actor.SCHEDULER)
        side_effect_counts = {
            key: value
            for key, value in self._counts.items()
            if key not in {"account_authority", "repository"}
        }
        observed = self._authority.observed
        fingerprint = getattr(observed, "account_fingerprint", None)
        if (
            result.terminal_code != "CALIBRATION_BINDING_NO_TRADE"
            or any(side_effect_counts.values())
            or self._counts["account_authority"] != 1
            or self._counts["repository"] != 4
            or not isinstance(fingerprint, str)
        ):
            raise RehearsalError("SUBMISSION_NO_TRADE_NOT_PROVEN")
        return FixtureSubmissionEvidence(
            result.terminal_code,
            MappingProxyType(side_effect_counts | {"repository": self._counts["repository"]}),
            fingerprint,
        )


@dataclass(frozen=True)
class RehearsalResult:
    artifact_directory: ArtifactHandle
    summary: Mapping[str, object]


@dataclass(frozen=True)
class _ProviderRehearsalArtifact:
    mode: Mode
    captured_at: datetime
    private: Mapping[str, object]
    public: Mapping[str, object]


@dataclass(frozen=True)
class _ArtifactObject:
    items: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class _ArtifactArray:
    items: tuple[object, ...]


@dataclass
class _ArtifactBounds:
    items: int = 0
    estimated_bytes: int = 0
    numeric_bytes: int = 0


class ArtifactHandle:
    def __init__(
        self,
        root: Path,
        root_identity: tuple[int, int],
        name: str,
        descriptor: int,
    ) -> None:
        self._root = root
        self._root_identity = root_identity
        self.name = name
        self._descriptor = descriptor
        details = os.fstat(descriptor)
        self._identity = (details.st_dev, details.st_ino)
        self._closed = False

    @property
    def path(self) -> Path:
        if self._closed:
            raise RehearsalError("ARTIFACT_HANDLE_CLOSED")
        try:
            root = os.stat(self._root, follow_symlinks=False)
            target = os.stat(self._root / self.name, follow_symlinks=False)
        except OSError:
            raise RehearsalError("ARTIFACT_PATH_IDENTITY_CHANGED") from None
        if (root.st_dev, root.st_ino) != self._root_identity or (
            target.st_dev,
            target.st_ino,
        ) != self._identity:
            raise RehearsalError("ARTIFACT_PATH_IDENTITY_CHANGED")
        return self._root / self.name

    def read_public(self) -> bytes:
        if self._closed:
            raise RehearsalError("ARTIFACT_HANDLE_CLOSED")
        details = os.fstat(self._descriptor)
        if (details.st_dev, details.st_ino) != self._identity:
            raise RehearsalError("ARTIFACT_PATH_IDENTITY_CHANGED")
        return _verify_artifact_directory(self._descriptor)[0]

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True


class ArtifactStore:
    def __init__(
        self, root: Path, descriptor: int, *, fail_after_writes: int | None = None
    ) -> None:
        self._root_path = root
        self._descriptor = descriptor
        details = os.fstat(descriptor)
        self._identity = (details.st_dev, details.st_ino)
        self._fail_after_writes = fail_after_writes
        self._writes = 0
        self._last_public_summary: Mapping[str, object] | None = None
        self._locked = False
        self._closed = False

    @classmethod
    def open(cls, root: Path, *, fail_after_writes: int | None = None) -> ArtifactStore:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(root, flags)
            details = os.fstat(descriptor)
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
                raise RehearsalError("ARTIFACT_ROOT_NOT_PRIVATE")
        except RehearsalError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise RehearsalError("ARTIFACT_ROOT_INVALID") from None
        return cls(root, descriptor, fail_after_writes=fail_after_writes)

    @classmethod
    def open_fixed(cls) -> ArtifactStore:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            repository = os.open(REPOSITORY_ROOT, flags)
            try:
                private = _open_or_create_directory(repository, ".private")
                try:
                    artifacts = _open_or_create_directory(private, "provider-rehearsals")
                finally:
                    os.close(private)
            finally:
                os.close(repository)
        except OSError:
            raise RehearsalError("ARTIFACT_ROOT_INVALID") from None
        return cls(PRIVATE_ARTIFACT_ROOT, artifacts)

    def seal_fixture(
        self,
        mode: Mode,
        captured_at: datetime,
        private: Mapping[str, object],
        public: Mapping[str, object],
        *,
        run_id: str | None = None,
    ) -> ArtifactHandle:
        if type(private) is not dict or type(public) is not dict:
            raise RehearsalError("ARTIFACT_JSON_INVALID")
        forbidden = {
            "artifact_class",
            "competition_evidence",
            "paper_endpoint_verified",
            "production_factory",
            "invocation",
            "provider_rehearsal",
        }
        for payload, limit, size_code in (
            (private, MAX_PRIVATE_ARTIFACT_BYTES, "PRIVATE_ARTIFACT_TOO_LARGE"),
            (public, MAX_PUBLIC_ARTIFACT_BYTES, "PUBLIC_ARTIFACT_TOO_LARGE"),
        ):
            try:
                _artifact_payload_bytes(payload, limit, size_code)
            except RehearsalError as error:
                if str(error) == "COMPETITION_EVIDENCE_FORBIDDEN":
                    raise RehearsalError("FIXTURE_ARTIFACT_FIELD_FORBIDDEN") from None
                raise
        if forbidden.intersection(public):
            raise RehearsalError("FIXTURE_ARTIFACT_FIELD_FORBIDDEN")
        return self._seal(
            mode,
            captured_at,
            {"fixture_evidence": private.copy()},
            {
                "artifact_class": "FIXTURE_TEST_ONLY",
                "provider_rehearsal": False,
                "competition_evidence": False,
                "details": public.copy(),
            },
            schema=FIXTURE_SCHEMA,
            run_id=run_id,
        )

    def _seal_provider_rehearsal(
        self,
        artifact: _ProviderRehearsalArtifact,
        *,
        run_id: str | None = None,
    ) -> ArtifactHandle:
        if type(artifact) is not _ProviderRehearsalArtifact:
            raise RehearsalError("PROVIDER_REHEARSAL_ARTIFACT_INVALID")
        return self._seal(
            artifact.mode,
            artifact.captured_at,
            artifact.private,
            artifact.public,
            schema=REHEARSAL_SCHEMA,
            run_id=run_id,
        )

    def _seal(
        self,
        mode: Mode,
        captured_at: datetime,
        private: Mapping[str, object],
        public: Mapping[str, object],
        *,
        schema: str,
        run_id: str | None,
    ) -> ArtifactHandle:
        if (
            type(mode) is not Mode
            or type(captured_at) is not datetime
            or (run_id is not None and type(run_id) is not str)
            or type(private) is not dict
            or type(public) is not dict
        ):
            raise RehearsalError("ARTIFACT_JSON_INVALID")
        if self._closed:
            raise RehearsalError("ARTIFACT_ROOT_INVALID")
        try:
            root_details = os.fstat(self._descriptor)
        except OSError:
            raise RehearsalError("ARTIFACT_ROOT_INVALID") from None
        if (
            not stat.S_ISDIR(root_details.st_mode)
            or (root_details.st_dev, root_details.st_ino) != self._identity
            or root_details.st_uid != os.getuid()
            or stat.S_IMODE(root_details.st_mode) & 0o077
        ):
            raise RehearsalError("ARTIFACT_ROOT_NOT_PRIVATE")
        if not self._locked:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX)
            self._locked = True
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise RehearsalError("REHEARSAL_TIME_INVALID")
        identifier = run_id or str(uuid4())
        try:
            if str(UUID(identifier)) != identifier:
                raise ValueError
        except ValueError:
            raise RehearsalError("ARTIFACT_RUN_ID_INVALID") from None
        timestamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{timestamp}-{mode.value}-{identifier}"
        self._require_name_available(name)
        temporary_name = f".staging-{identifier}"
        captured = captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        private_core = private.copy() | {
            "schema": schema,
            "run_id": identifier,
            "captured_at": captured,
            "mode": mode.value,
        }
        private_core_bytes = _artifact_payload_bytes(
            private_core,
            MAX_PRIVATE_ARTIFACT_BYTES,
            "PRIVATE_ARTIFACT_TOO_LARGE",
        )
        private_evidence_hash = hashlib.sha256(private_core_bytes).hexdigest()
        public_payload = public.copy() | {
            "schema": schema,
            "run_id": identifier,
            "captured_at": captured,
            "mode": mode.value,
            "private_evidence_sha256": private_evidence_hash,
        }
        public_bytes = _artifact_payload_bytes(
            public_payload,
            MAX_PUBLIC_ARTIFACT_BYTES,
            "PUBLIC_ARTIFACT_TOO_LARGE",
        )
        private_payload = private_core | {
            "public_summary_sha256": hashlib.sha256(public_bytes).hexdigest()
        }
        private_bytes = _artifact_payload_bytes(
            private_payload,
            MAX_PRIVATE_ARTIFACT_BYTES,
            "PRIVATE_ARTIFACT_TOO_LARGE",
        )
        directory_created = False
        try:
            os.mkdir(temporary_name, mode=0o700, dir_fd=self._descriptor)
            directory_created = True
            run_fd = os.open(
                temporary_name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._descriptor,
            )
        except OSError:
            if directory_created:
                with contextlib.suppress(OSError):
                    os.rmdir(temporary_name, dir_fd=self._descriptor)
            raise RehearsalError("ARTIFACT_DIRECTORY_CREATE_FAILED") from None
        published = False
        try:
            self._write(run_fd, "manifest.private.json", private_bytes)
            self._write(run_fd, "summary.public.json", public_bytes)
            os.fsync(run_fd)
            os.chmod("manifest.private.json", 0o400, dir_fd=run_fd, follow_symlinks=False)
            os.chmod("summary.public.json", 0o400, dir_fd=run_fd, follow_symlinks=False)
            os.fchmod(run_fd, 0o500)
            verified_public, verified_summary = _verify_artifact_directory(run_fd)
            if verified_public != public_bytes or verified_summary != public_payload:
                raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
            self._require_name_available(name)
            self._require_directory_identity(temporary_name, run_fd)
            _rename_directory_noreplace(
                self._descriptor,
                temporary_name,
                self._descriptor,
                name,
            )
            published = True
            self._require_directory_identity(name, run_fd)
            os.fsync(self._descriptor)
        except (OSError, RehearsalError) as error:
            with contextlib.suppress(OSError):
                os.fchmod(run_fd, 0o700)
            for filename in ("manifest.private.json", "summary.public.json"):
                with contextlib.suppress(OSError):
                    os.unlink(filename, dir_fd=run_fd)
            os.close(run_fd)
            with contextlib.suppress(OSError):
                os.rmdir(name if published else temporary_name, dir_fd=self._descriptor)
            with contextlib.suppress(OSError):
                os.fsync(self._descriptor)
            if isinstance(error, RehearsalError) and str(error) in {
                "ARTIFACT_DIRECTORY_CREATE_FAILED",
                "ATOMIC_ARTIFACT_PUBLISH_UNAVAILABLE",
            }:
                raise error
            raise RehearsalError("ARTIFACT_WRITE_FAILED") from None
        self._last_public_summary = MappingProxyType(dict(public_payload))
        root = os.fstat(self._descriptor)
        return ArtifactHandle(
            self._root_path,
            (root.st_dev, root.st_ino),
            name,
            run_fd,
        )

    def _require_name_available(self, name: str) -> None:
        try:
            os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise RehearsalError("ARTIFACT_DIRECTORY_CREATE_FAILED") from None
        raise RehearsalError("ARTIFACT_DIRECTORY_CREATE_FAILED")

    def _require_directory_identity(self, name: str, descriptor: int) -> None:
        try:
            named = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
            opened = os.fstat(descriptor)
        except OSError:
            raise RehearsalError("ARTIFACT_PATH_IDENTITY_CHANGED") from None
        if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise RehearsalError("ARTIFACT_PATH_IDENTITY_CHANGED")

    def _write(self, directory: int, name: str, value: bytes) -> None:
        if self._fail_after_writes is not None and self._writes >= self._fail_after_writes:
            raise RehearsalError("ARTIFACT_WRITE_FAILED")
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            view = memoryview(value)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise RehearsalError("ARTIFACT_WRITE_FAILED")
                view = view[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._writes += 1

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True

    @property
    def last_public_summary(self) -> Mapping[str, object]:
        if self._last_public_summary is None:
            raise RehearsalError("ARTIFACT_NOT_SEALED")
        return dict(self._last_public_summary)


def _verify_artifact_directory(descriptor: int) -> tuple[bytes, dict[str, object]]:
    details = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o500
    ):
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
    if _artifact_directory_names(descriptor, details) != [
        "manifest.private.json",
        "summary.public.json",
    ]:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
    public_bytes = _read_named_artifact(
        descriptor, "summary.public.json", MAX_PUBLIC_ARTIFACT_BYTES
    )
    private_bytes = _read_named_artifact(
        descriptor, "manifest.private.json", MAX_PRIVATE_ARTIFACT_BYTES
    )
    try:
        public_payload = _json_object(public_bytes.decode(), "ARTIFACT_VERIFICATION_FAILED")
        private_payload = _json_object(private_bytes.decode(), "ARTIFACT_VERIFICATION_FAILED")
    except (RehearsalError, UnicodeError):
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED") from None
    public_hash = private_payload.pop("public_summary_sha256", None)
    private_hash = public_payload.get("private_evidence_sha256")
    try:
        verified_public_bytes = _artifact_payload_bytes(
            public_payload,
            MAX_PUBLIC_ARTIFACT_BYTES,
            "PUBLIC_ARTIFACT_TOO_LARGE",
        )
        verified_private_core_bytes = _artifact_payload_bytes(
            private_payload,
            MAX_PRIVATE_ARTIFACT_BYTES,
            "PRIVATE_ARTIFACT_TOO_LARGE",
        )
    except RehearsalError:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED") from None
    if (
        public_bytes != verified_public_bytes
        or public_hash != hashlib.sha256(public_bytes).hexdigest()
        or private_hash != hashlib.sha256(verified_private_core_bytes).hexdigest()
        or public_payload.get("schema") != private_payload.get("schema")
        or public_payload.get("run_id") != private_payload.get("run_id")
        or public_payload.get("captured_at") != private_payload.get("captured_at")
        or public_payload.get("mode") != private_payload.get("mode")
    ):
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
    schema = public_payload.get("schema")
    if schema == FIXTURE_SCHEMA:
        if (
            public_payload.get("artifact_class") != "FIXTURE_TEST_ONLY"
            or public_payload.get("provider_rehearsal") is not False
            or public_payload.get("competition_evidence") is not False
        ):
            raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
    elif schema == REHEARSAL_SCHEMA:
        _verify_provider_rehearsal_public(public_payload)
    else:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
    return public_bytes, public_payload


def _artifact_directory_names(descriptor: int, details: os.stat_result) -> list[str]:
    scan_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    scan_descriptor: int | None = None
    try:
        scan_descriptor = os.open(".", scan_flags, dir_fd=descriptor)
        scan_details = os.fstat(scan_descriptor)
        if (scan_details.st_dev, scan_details.st_ino) != (
            details.st_dev,
            details.st_ino,
        ):
            raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
        names = sorted(os.listdir(scan_descriptor))
    except OSError:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED") from None
    finally:
        if scan_descriptor is not None:
            os.close(scan_descriptor)
    return names


def _read_named_artifact(directory: int, name: str, limit: int) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
    except OSError:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED") from None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o400
        ):
            raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")
        return _read_fd(descriptor, limit)
    except RehearsalError:
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED") from None
    finally:
        os.close(descriptor)


def _verify_provider_rehearsal_public(payload: Mapping[str, object]) -> None:
    mode = payload.get("mode")
    expected_invocation = {
        Mode.DEVELOPMENT.value: "python -m ops.launch.provider_rehearsal development",
        Mode.SUBMISSION_NO_TRADE.value: (
            "python -m ops.launch.provider_rehearsal submission-no-trade"
        ),
    }.get(mode)
    source_hash = payload.get("source_sha256")
    if (
        payload.get("artifact_class") != "PROVIDER_REHEARSAL"
        or payload.get("provider_rehearsal") is not True
        or payload.get("competition_evidence") is not False
        or payload.get("paper_endpoint_verified") is not True
        or payload.get("book_unchanged") is not True
        or payload.get("production_factory") != "backend.app.runtime.build_production_agent"
        or payload.get("invocation") != expected_invocation
        or not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        raise RehearsalError("ARTIFACT_VERIFICATION_FAILED")


def build_public_rehearsal_receipt(payload: Mapping[str, object]) -> dict[str, object]:
    _verify_provider_rehearsal_public(payload)
    cli = payload.get("cli")
    mcp = payload.get("mcp")
    service = payload.get("service")
    service_counts = service.get("counts") if isinstance(service, dict) else None
    if (
        payload.get("mode") != Mode.DEVELOPMENT.value
        or payload.get("clean_100k_baseline_verified") is not False
        or payload.get("provider_write_calls") != 0
        or type(payload.get("provider_request_count")) is not int
        or payload["provider_request_count"] < 1
        or not isinstance(cli, dict)
        or cli
        != {
            "version": "0.0.13",
            "archive_verified": True,
            "binary_verified": True,
            "paper_host_verified": True,
            "dry_run": True,
        }
        or not isinstance(mcp, dict)
        or mcp.get("call") != "get_clock"
        or mcp.get("call_count") != 1
        or type(mcp.get("tool_surface_count")) is not int
        or not isinstance(service, dict)
        or service.get("terminal_code") != "PROVIDER_FAILURE_NO_ACTION"
        or service.get("failure_code") != "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE"
        or service_counts
        != {
            "acquisition": 1,
            "mcp": 0,
            "gemini": 0,
            "proposal": 0,
            "intent": 0,
            "execution": 0,
            "provider_write": 0,
            "repository": 4,
        }
    ):
        raise RehearsalError("PUBLIC_RECEIPT_INVALID")
    summary_bytes = _artifact_payload_bytes(
        dict(payload),
        MAX_PUBLIC_ARTIFACT_BYTES,
        "PUBLIC_ARTIFACT_TOO_LARGE",
    )
    return {
        "schema_version": 1,
        "captured_at": payload["captured_at"],
        "account_role": "DEVELOPMENT",
        "competition_evidence": False,
        "paper_host_verified": True,
        "book_unchanged": True,
        "provider_request_count": payload["provider_request_count"],
        "provider_write_calls": 0,
        "production_factory": payload["production_factory"],
        "service_terminal_code": service["terminal_code"],
        "safe_stop_reason": service["failure_code"],
        "cli": {
            "version": cli.get("version"),
            "archive_verified": cli.get("archive_verified"),
            "binary_verified": cli.get("binary_verified"),
            "dry_run": cli.get("dry_run"),
            "paper_host_verified": cli.get("paper_host_verified"),
        },
        "mcp": {
            "call": mcp.get("call"),
            "call_count": mcp.get("call_count"),
            "tool_surface_count": mcp.get("tool_surface_count"),
        },
        "operator_source_sha256": payload["source_sha256"],
        "sealed_summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
    }


def _open_or_create_directory(parent: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(name, flags, dir_fd=parent)
    details = os.fstat(descriptor)
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
        os.close(descriptor)
        raise RehearsalError("ARTIFACT_ROOT_NOT_PRIVATE")
    return descriptor


def _rename_directory_noreplace(
    source_directory: int,
    source_name: str,
    target_directory: int,
    target_name: str,
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise RehearsalError("ATOMIC_ARTIFACT_PUBLISH_UNAVAILABLE") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_directory,
        os.fsencode(source_name),
        target_directory,
        os.fsencode(target_name),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise RehearsalError("ARTIFACT_DIRECTORY_CREATE_FAILED")
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise RehearsalError("ATOMIC_ARTIFACT_PUBLISH_UNAVAILABLE")
    raise RehearsalError("ARTIFACT_WRITE_FAILED")


def _freeze_artifact_json(
    value: object,
    *,
    byte_limit: int,
    size_code: str,
) -> object:
    bounds = _ArtifactBounds()

    def add_bytes(count: int, *, numeric: bool = False) -> None:
        bounds.estimated_bytes += count
        if numeric:
            bounds.numeric_bytes += count
        if bounds.numeric_bytes > MAX_ARTIFACT_NUMERIC_BYTES:
            raise RehearsalError("ARTIFACT_JSON_BOUNDS_EXCEEDED")
        if bounds.estimated_bytes > byte_limit:
            raise RehearsalError(size_code)

    def freeze(current: object, depth: int) -> object:
        bounds.items += 1
        if bounds.items > MAX_ARTIFACT_JSON_ITEMS or depth > MAX_ARTIFACT_JSON_DEPTH:
            raise RehearsalError("ARTIFACT_JSON_BOUNDS_EXCEEDED")
        if current is None:
            add_bytes(4)
            return None
        if type(current) is bool:
            add_bytes(5 if current is False else 4)
            return current
        if type(current) is str:
            try:
                encoded_length = len(current.encode("utf-8"))
            except UnicodeEncodeError:
                raise RehearsalError("ARTIFACT_JSON_INVALID") from None
            if encoded_length > byte_limit:
                raise RehearsalError(size_code)
            add_bytes(len(json.encoder.encode_basestring_ascii(current)))
            return current
        if type(current) is int:
            if abs(current) > MAX_ARTIFACT_ABS_NUMBER:
                raise RehearsalError("ARTIFACT_JSON_BOUNDS_EXCEEDED")
            rendered = str(current)
            if len(rendered) > MAX_PROVIDER_NUMBER_RENDER:
                raise RehearsalError("ARTIFACT_JSON_BOUNDS_EXCEEDED")
            add_bytes(len(rendered), numeric=True)
            return current
        if type(current) is float:
            magnitude = abs(current)
            rendered = repr(current)
            if (
                not math.isfinite(current)
                or magnitude > MAX_ARTIFACT_ABS_NUMBER
                or (magnitude != 0 and magnitude < MIN_ARTIFACT_ABS_FLOAT)
                or len(rendered.encode("ascii")) > MAX_PROVIDER_NUMBER_RENDER
            ):
                raise RehearsalError("ARTIFACT_JSON_BOUNDS_EXCEEDED")
            add_bytes(len(rendered), numeric=True)
            return current
        if type(current) is dict:
            add_bytes(2 + max(len(current) - 1, 0))
            items: list[tuple[str, object]] = []
            for key, item in current.items():
                if type(key) is not str:
                    raise RehearsalError("ARTIFACT_JSON_INVALID")
                try:
                    encoded_length = len(key.encode("utf-8"))
                except UnicodeEncodeError:
                    raise RehearsalError("ARTIFACT_JSON_INVALID") from None
                if encoded_length > byte_limit:
                    raise RehearsalError(size_code)
                add_bytes(1 + len(json.encoder.encode_basestring_ascii(key)))
                items.append((key, freeze(item, depth + 1)))
            return _ArtifactObject(tuple(sorted(items, key=lambda pair: pair[0])))
        if type(current) is list:
            add_bytes(2 + max(len(current) - 1, 0))
            return _ArtifactArray(tuple(freeze(item, depth + 1) for item in current))
        raise RehearsalError("ARTIFACT_JSON_INVALID")

    return freeze(value, 0)


def _artifact_tree_contains_competition_claim(value: object) -> bool:
    if isinstance(value, _ArtifactObject):
        for key, item in value.items:
            if key == "competition_evidence" and item is not False:
                return True
            if _artifact_tree_contains_competition_claim(item):
                return True
    elif isinstance(value, _ArtifactArray):
        return any(_artifact_tree_contains_competition_claim(item) for item in value.items)
    return False


def _thaw_artifact_json(value: object) -> object:
    if isinstance(value, _ArtifactObject):
        return {key: _thaw_artifact_json(item) for key, item in value.items}
    if isinstance(value, _ArtifactArray):
        return [_thaw_artifact_json(item) for item in value.items]
    return value


def _artifact_payload_bytes(value: object, byte_limit: int, size_code: str) -> bytes:
    tree = _freeze_artifact_json(value, byte_limit=byte_limit, size_code=size_code)
    if _artifact_tree_contains_competition_claim(tree):
        raise RehearsalError("COMPETITION_EVIDENCE_FORBIDDEN")
    try:
        encoded = json.dumps(
            _thaw_artifact_json(tree),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError):
        raise RehearsalError("ARTIFACT_JSON_INVALID") from None
    result = encoded + b"\n"
    if len(result) > byte_limit:
        raise RehearsalError(size_code)
    if b'"competition_evidence":true' in result:
        raise RehearsalError("COMPETITION_EVIDENCE_FORBIDDEN")
    return result


def _canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            _json_safe(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise RehearsalError("ARTIFACT_JSON_INVALID") from None
    return (encoded + "\n").encode()


def _json_safe(value: object) -> object:
    if type(value) is dict or type(value) is MappingProxyType:
        if any(type(key) is not str for key in value):
            raise RehearsalError("ARTIFACT_JSON_INVALID")
        return {key: _json_safe(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_json_safe(item) for item in value]
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise RehearsalError("ARTIFACT_JSON_INVALID")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if type(value) in {Decimal, UUID}:
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or type(value) in {bool, str, int, float}:
        return value
    raise RehearsalError("ARTIFACT_JSON_INVALID")


def _capture_payload(capture: BookCapture) -> dict[str, object]:
    payload = {
        "role": capture.role,
        "account_fingerprint": capture.account_fingerprint,
        "account": _json_safe(capture.account),
        "positions": _json_safe(capture.positions),
        "orders": _json_safe(capture.orders),
        "activities": _json_safe(capture.activities),
        "sdk_trace": list(capture.sdk_trace),
        "http_trace": list(capture.http_trace),
        "pagination_terminal": capture.pagination_terminal,
    }
    if len(_canonical_json(payload)) > MAX_CAPTURE_BYTES:
        raise RehearsalError("PROVIDER_CAPTURE_TOO_LARGE")
    return payload


def _book_digest(capture: BookCapture) -> str:
    stable = _capture_payload(capture)
    stable.pop("sdk_trace")
    stable.pop("http_trace")
    return hashlib.sha256(_canonical_json(stable)).hexdigest()


def _build_instrumented_production_resources(
    settings: object,
    transport: TransportLedger,
    counters: SafetyCounters,
):
    import httpx
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

    from backend.app.alpaca.mcp import AlpacaMCPResearchClient
    from backend.app.evidence.gemini import GeminiStructuredTransport
    from backend.app.runtime.providers import build_production_resources

    def trading_factory(**kwargs: object) -> object:
        client = TradingClient(**kwargs)
        transport.instrument_requests_session(client._session)
        return client

    def option_factory(**kwargs: object) -> object:
        client = OptionHistoricalDataClient(**kwargs)
        transport.instrument_requests_session(client._session)
        return client

    def stock_factory(**kwargs: object) -> object:
        client = StockHistoricalDataClient(**kwargs)
        transport.instrument_requests_session(client._session)
        return client

    def http_factory(**kwargs: object) -> object:
        client = httpx.Client(**kwargs)
        transport.instrument_requests_session(client)
        return client

    def model_factory(api_key: str) -> object:
        return _CountedModelTransport(
            GeminiStructuredTransport.from_api_key(api_key),
            counters,
        )

    def mcp_factory(**kwargs: object) -> object:
        return _CountedMCPClient(AlpacaMCPResearchClient(**kwargs), counters)

    return build_production_resources(
        settings,
        trading_factory=trading_factory,
        option_data_factory=option_factory,
        stock_data_factory=stock_factory,
        http_factory=http_factory,
        model_factory=model_factory,
        mcp_factory=mcp_factory,
    )


async def _close_production_resources(resources: object) -> None:
    candidates = (
        resources.mcp_research,
        resources.model_transport,
        resources.providers.activities.resource,
        resources.providers.option_snapshots.resource,
        resources.providers.stock_market_data.resource,
        resources.providers.trading.resource,
    )
    seen: set[int] = set()
    failure: BaseException | None = None
    for resource in candidates:
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        try:
            if resource.aclose is not None:
                await resource.aclose()
            elif resource.close is not None:
                resource.close()
        except BaseException as error:
            failure = failure or error
    if failure is not None:
        raise RehearsalError("PRODUCTION_RESOURCE_CLEANUP_FAILED") from failure


@dataclass(frozen=True)
class DurableAuthorityEvidence:
    role: AccountRole
    account_fingerprint: str
    baseline_evidence_hash: str | None


class _ExistingAccountRepository:
    def __init__(self, delegate: object, authority: DurableAuthorityEvidence) -> None:
        self._delegate = delegate
        self._authority = authority

    def register_account(
        self,
        *,
        role: AccountRole,
        fingerprint: str,
        equity: Decimal,
        autonomous_enabled: bool,
    ) -> None:
        if (
            role is not self._authority.role
            or fingerprint != self._authority.account_fingerprint
            or not equity.is_finite()
            or equity <= 0
            or autonomous_enabled is not False
        ):
            raise RehearsalError("PREEXISTING_ACCOUNT_AUTHORITY_MISMATCH")

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


def _validate_preexisting_authority(
    persistence: object,
    *,
    role: AccountRole,
    account_fingerprint: str,
) -> DurableAuthorityEvidence:
    from sqlalchemy import select

    from backend.app.persistence.sqlalchemy_models import AccountRoleRow, SubmissionBaselineRow

    def valid_hash(value: object) -> bool:
        return (
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    if type(role) is not AccountRole or not valid_hash(account_fingerprint):
        raise RehearsalError("PREEXISTING_ACCOUNT_AUTHORITY_REQUIRED")
    with persistence.sessions() as session:
        account = session.get(AccountRoleRow, role.value)
        if (
            account is None
            or type(account.role) is not str
            or account.role != role.value
            or not valid_hash(account.account_fingerprint)
            or account.account_fingerprint != account_fingerprint
            or type(account.equity) is not Decimal
            or not account.equity.is_finite()
            or account.equity <= 0
        ):
            raise RehearsalError("PREEXISTING_ACCOUNT_AUTHORITY_REQUIRED")
        if role is AccountRole.DEVELOPMENT:
            return DurableAuthorityEvidence(role, account_fingerprint, None)
        baseline = session.scalar(
            select(SubmissionBaselineRow).where(
                SubmissionBaselineRow.account_role == AccountRole.SUBMISSION.value
            )
        )
        if (
            baseline is None
            or baseline.account_fingerprint != account_fingerprint
            or baseline.account_role != AccountRole.SUBMISSION.value
            or baseline.equity != Decimal("100000")
            or baseline.contaminated is not False
            or type(baseline.captured_at) is not datetime
            or baseline.captured_at.tzinfo is None
            or not all(
                valid_hash(value)
                for value in (
                    baseline.positions_hash,
                    baseline.orders_hash,
                    baseline.activities_hash,
                )
            )
        ):
            raise RehearsalError("CLEAN_SUBMISSION_BASELINE_REQUIRED")
        baseline_hash = hashlib.sha256(
            _canonical_json(
                {
                    "role": baseline.account_role,
                    "account_fingerprint": baseline.account_fingerprint,
                    "equity": str(baseline.equity),
                    "captured_at": baseline.captured_at,
                    "positions_hash": baseline.positions_hash,
                    "orders_hash": baseline.orders_hash,
                    "activities_hash": baseline.activities_hash,
                    "contaminated": baseline.contaminated,
                }
            )
        ).hexdigest()
    return DurableAuthorityEvidence(role, account_fingerprint, baseline_hash)


def _attach_repository_counter(persistence: object, counters: SafetyCounters) -> None:
    from sqlalchemy import event

    def before_cursor_execute(*_args: object) -> None:
        counters.record("repository_calls")

    event.listen(persistence.engine, "before_cursor_execute", before_cursor_execute)


async def _build_canonical_production_agent(
    settings: object,
    role: AccountRole,
    transport: TransportLedger,
    counters: SafetyCounters,
) -> tuple[object, DurableAuthorityEvidence]:
    from backend.app.persistence import create_runtime_persistence
    from backend.app.runtime import build_production_agent

    persistence = create_runtime_persistence(
        settings.database_url.get_secret_value(),
        REPOSITORY_ROOT / "migrations",
        entry_limits=settings.entry_budget_limits(),
        server_autonomy_enabled=settings.app_autonomous_enabled,
    )
    resources: object | None = None
    transferred = False
    try:
        _attach_repository_counter(persistence, counters)
        resources = _build_instrumented_production_resources(settings, transport, counters)
        authority = _validate_preexisting_authority(
            persistence,
            role=role,
            account_fingerprint=resources.account_fingerprint,
        )
        guarded = replace(
            persistence,
            repository=_ExistingAccountRepository(persistence.repository, authority),
        )
        transferred = True
        agent = await build_production_agent(
            settings,
            REPOSITORY_ROOT / "migrations",
            persistence_factory=lambda *_args, **_kwargs: guarded,
            resources_factory=lambda _settings: resources,
        )
        return agent, authority
    finally:
        if not transferred:
            if resources is not None:
                await _close_production_resources(resources)
            persistence.close()


@dataclass(frozen=True)
class _CanonicalServiceEvidence:
    terminal_code: str
    failure_code: str | None
    counts: Mapping[str, int]
    account_fingerprint: str


async def _probe_canonical_submission(agent: object) -> _CanonicalServiceEvidence:
    service = agent.service
    if type(service) is not AgentRunService:
        raise RehearsalError("CANONICAL_AGENT_SERVICE_REQUIRED")
    counts = {
        key: 0
        for key in (
            "acquisition",
            "mcp",
            "gemini",
            "proposal",
            "intent",
            "execution",
            "provider_write",
            "account_authority",
            "repository",
        )
    }
    original_authority = service._account_authority
    original_acquisition = service._acquisition
    original_decisions = service._decisions
    original_runtime = service._runtime
    counted_authority = _CountedAuthority(original_authority, counts)
    service._account_authority = counted_authority
    service._acquisition = _CountedAcquisition(original_acquisition, counts)
    service._decisions = _CountedDecisions(original_decisions, counts)
    service._runtime = _CountedRuntime(original_runtime, counts)
    try:
        result = await service.run(Actor.SCHEDULER)
    finally:
        service._account_authority = original_authority
        service._acquisition = original_acquisition
        service._decisions = original_decisions
        service._runtime = original_runtime
    side_effect_counts = {
        key: counts[key]
        for key in (
            "acquisition",
            "mcp",
            "gemini",
            "proposal",
            "intent",
            "execution",
            "provider_write",
        )
    }
    calibration = result.decision.calibration
    observed = counted_authority.observed
    fingerprint = getattr(observed, "account_fingerprint", None)
    if (
        result.terminal_code != "CALIBRATION_BINDING_NO_TRADE"
        or result.decision.code != "CALIBRATION_BINDING_NO_TRADE"
        or result.approved_intent_id is not None
        or result.execution_certificate_id is not None
        or calibration is None
        or calibration.account_role is not AccountRole.SUBMISSION
        or calibration.account_fingerprint != fingerprint
        or any(side_effect_counts.values())
        or counts["account_authority"] != 1
        or counts["repository"] != 4
        or not isinstance(fingerprint, str)
    ):
        raise RehearsalError("SUBMISSION_NO_TRADE_NOT_PROVEN")
    return _CanonicalServiceEvidence(
        result.terminal_code,
        None,
        MappingProxyType(side_effect_counts | {"repository": counts["repository"]}),
        fingerprint,
    )


async def _probe_canonical_development(agent: object) -> _CanonicalServiceEvidence:
    service = agent.service
    if type(service) is not AgentRunService:
        raise RehearsalError("CANONICAL_AGENT_SERVICE_REQUIRED")
    counts = {
        key: 0
        for key in (
            "acquisition",
            "mcp",
            "gemini",
            "proposal",
            "intent",
            "execution",
            "provider_write",
            "account_authority",
            "repository",
        )
    }
    original_authority = service._account_authority
    original_acquisition = service._acquisition
    original_decisions = service._decisions
    original_runtime = service._runtime
    counted_authority = _CountedAuthority(original_authority, counts)
    service._account_authority = counted_authority
    service._acquisition = _CountedAcquisition(original_acquisition, counts)
    service._decisions = _CountedDecisions(original_decisions, counts)
    service._runtime = _CountedRuntime(original_runtime, counts)
    try:
        result = await service.run(Actor.SCHEDULER)
    finally:
        service._account_authority = original_authority
        service._acquisition = original_acquisition
        service._decisions = original_decisions
        service._runtime = original_runtime
    side_effect_counts = {
        key: counts[key]
        for key in (
            "acquisition",
            "mcp",
            "gemini",
            "proposal",
            "intent",
            "execution",
            "provider_write",
        )
    }
    fingerprint = getattr(counted_authority.observed, "account_fingerprint", None)
    if (
        result.terminal_code != "PROVIDER_FAILURE_NO_ACTION"
        or result.decision.code != "PROVIDER_FAILURE_NO_ACTION"
        or result.decision.provider_failure_code != "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE"
        or result.approved_intent_id is not None
        or result.execution_certificate_id is not None
        or side_effect_counts
        != {
            "acquisition": 1,
            "mcp": 0,
            "gemini": 0,
            "proposal": 0,
            "intent": 0,
            "execution": 0,
            "provider_write": 0,
        }
        or counts["account_authority"] != 1
        or counts["repository"] != 4
        or not isinstance(fingerprint, str)
    ):
        raise RehearsalError("DEVELOPMENT_SERVICE_OUTCOME_NOT_PROVEN")
    return _CanonicalServiceEvidence(
        result.terminal_code,
        result.decision.provider_failure_code,
        MappingProxyType(side_effect_counts | {"repository": counts["repository"]}),
        fingerprint,
    )


def _trading_boundary_from_agent(
    agent: object,
    transport: TransportLedger,
    role: AccountRole,
) -> TradingBoundary:
    resources = agent.resources
    activity_adapter = resources.providers.activities.resource.value
    activity_reader = getattr(activity_adapter, "_reader", activity_adapter)
    activity_client = getattr(activity_reader, "_client", None)
    activity_headers = getattr(activity_reader, "_headers", None)
    return TradingBoundary(
        client_factory=lambda _credentials: resources.providers.trading.resource.value,
        activity_http=ActivityHttpBoundary(activity_client, transport, headers=activity_headers),
        endpoint=PAPER_ENDPOINT,
        ledger=transport,
        role=role,
    )


async def run_development_operator() -> RehearsalResult:
    """Run the fixed development provider rehearsal through production composition."""
    from backend.app.config import RuntimeRole, Settings

    settings = Settings()
    if settings.app_account_role is not RuntimeRole.DEVELOPMENT:
        raise RehearsalError("DEVELOPMENT_ROLE_REQUIRED")
    provider_auth = Credentials(
        settings.alpaca_api_key.get_secret_value(),
        settings.alpaca_secret_key.get_secret_value(),
    )
    transport = TransportLedger()
    counters = SafetyCounters()
    agent: object | None = None
    cli: VerifiedCli | None = None
    store: ArtifactStore | None = None
    try:
        agent, authority = await _build_canonical_production_agent(
            settings, AccountRole.DEVELOPMENT, transport, counters
        )
        trading = _trading_boundary_from_agent(agent, transport, AccountRole.DEVELOPMENT)
        cli = VerifiedCli.from_archive(
            OFFICIAL_CLI_ARCHIVE, PRODUCTION_CLI_PIN, BoundedSubprocess()
        )
        mcp = MCPBoundary.from_retained_client(
            agent.resources.mcp_research.value,
            now=agent.persistence.database_clock.now,
        )
        captured_at = agent.persistence.database_clock.now()
        before = trading.capture(provider_auth)
        cli_evidence = cli.probe(provider_auth)
        mcp_evidence = await mcp.probe()
        service_evidence = await _probe_canonical_development(agent)
        after = trading.capture(provider_auth)
        artifact = _qualify_provider_rehearsal(
            Mode.DEVELOPMENT,
            captured_at,
            before,
            after,
            transport=transport,
            cli=cli_evidence,
            mcp=mcp_evidence,
            service=service_evidence,
            provider_counters=counters,
            authority=authority,
        )
        await agent.aclose()
        store = ArtifactStore.open_fixed()
        return _seal_provider_rehearsal_result(store, artifact)
    finally:
        if cli is not None:
            cli.close()
        if agent is not None:
            await agent.aclose()
        if store is not None:
            store.close()


async def run_submission_no_trade_operator() -> RehearsalResult:
    """Prove the sealed submission no-trade path through production composition."""
    from backend.app.config import RuntimeRole, Settings

    settings = Settings()
    if settings.app_account_role is not RuntimeRole.SUBMISSION:
        raise RehearsalError("SUBMISSION_ROLE_REQUIRED")
    provider_auth = Credentials(
        settings.alpaca_api_key.get_secret_value(),
        settings.alpaca_secret_key.get_secret_value(),
    )
    transport = TransportLedger()
    counters = SafetyCounters()
    agent: object | None = None
    store: ArtifactStore | None = None
    try:
        agent, authority = await _build_canonical_production_agent(
            settings, AccountRole.SUBMISSION, transport, counters
        )
        trading = _trading_boundary_from_agent(agent, transport, AccountRole.SUBMISSION)
        captured_at = agent.persistence.database_clock.now()
        before = trading.capture(provider_auth)
        submission = await _probe_canonical_submission(agent)
        after = trading.capture(provider_auth)
        artifact = _qualify_provider_rehearsal(
            Mode.SUBMISSION_NO_TRADE,
            captured_at,
            before,
            after,
            transport=transport,
            cli=None,
            mcp=None,
            service=submission,
            provider_counters=counters,
            authority=authority,
        )
        await agent.aclose()
        store = ArtifactStore.open_fixed()
        return _seal_provider_rehearsal_result(store, artifact)
    finally:
        if agent is not None:
            await agent.aclose()
        if store is not None:
            store.close()


async def run_fixture_development(
    credentials: Credentials,
    trading: TradingBoundary,
    cli: VerifiedCli,
    mcp: MCPBoundary,
    store: ArtifactStore,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RehearsalResult:
    captured_at = now()
    if trading.role is not AccountRole.DEVELOPMENT:
        raise RehearsalError("DEVELOPMENT_ROLE_REQUIRED")
    before = trading.capture(credentials)
    cli_evidence = cli.probe(credentials)
    mcp_evidence = await mcp.probe()
    after = trading.capture(credentials)
    if _book_digest(before) != _book_digest(after):
        raise RehearsalError("ACCOUNT_BOOK_CHANGED")
    directory = store.seal_fixture(
        Mode.DEVELOPMENT,
        captured_at,
        _fixture_private(before, after, trading.transport, cli_evidence, mcp_evidence),
        {
            "book_unchanged": True,
            "provider_request_count": len(trading.transport.operations),
            "mcp_call_count": len(mcp_evidence.call_trace),
            "cli_dry_run": cli_evidence["dry_run"],
        },
    )
    return RehearsalResult(directory, store.last_public_summary)


async def run_fixture_submission_no_trade(
    credentials: Credentials,
    trading: TradingBoundary,
    submission: FixtureSubmissionBoundary,
    store: ArtifactStore,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RehearsalResult:
    captured_at = now()
    if trading.role is not AccountRole.SUBMISSION:
        raise RehearsalError("SUBMISSION_ROLE_REQUIRED")
    before = trading.capture(credentials)
    submission_evidence = await submission.probe()
    if submission_evidence.account_fingerprint != before.account_fingerprint:
        raise RehearsalError("ACCOUNT_AUTHORITY_FINGERPRINT_MISMATCH")
    after = trading.capture(credentials)
    if _book_digest(before) != _book_digest(after):
        raise RehearsalError("ACCOUNT_BOOK_CHANGED")
    directory = store.seal_fixture(
        Mode.SUBMISSION_NO_TRADE,
        captured_at,
        _fixture_private(before, after, trading.transport, None, None),
        {
            "book_unchanged": True,
            "provider_request_count": len(trading.transport.operations),
            "terminal_code": submission_evidence.terminal_code,
            "side_effect_counts": dict(submission_evidence.counts),
        },
    )
    return RehearsalResult(directory, store.last_public_summary)


def _fixture_private(
    before: BookCapture,
    after: BookCapture,
    transport: TransportLedger,
    cli: Mapping[str, object] | None,
    mcp: MCPEvidence | None,
) -> dict[str, object]:
    return {
        "before": _capture_payload(before),
        "after": _capture_payload(after),
        "transport_trace": [asdict(operation) for operation in transport.operations],
        "cli_release": cli,
        "mcp_result_summary_hash": None if mcp is None else mcp.result_summary_hash,
    }


def _validate_qualified_capture(
    capture: BookCapture,
    expected_role: AccountRole,
    account_fingerprint: str,
    expected_sdk_trace: tuple[str, ...],
) -> None:
    if (
        capture.role != expected_role.value
        or capture.account_fingerprint != account_fingerprint
        or not capture.account
        or capture.account.get("status") != "ACTIVE"
        or capture.account.get("trading_blocked") is not False
        or capture.account.get("account_blocked") is not False
        or capture.account.get("trade_suspended_by_user") is not False
        or capture.sdk_trace != expected_sdk_trace
        or not capture.http_trace
        or any(method != "GET" for method in capture.http_trace)
        or capture.pagination_terminal is not True
    ):
        raise RehearsalError("PROVIDER_CAPTURE_INCOMPLETE")
    _capture_payload(capture)


def _qualify_provider_rehearsal(
    mode: Mode,
    captured_at: datetime,
    before: BookCapture,
    after: BookCapture,
    *,
    transport: TransportLedger,
    cli: Mapping[str, object] | None,
    mcp: MCPEvidence | None,
    service: _CanonicalServiceEvidence,
    provider_counters: SafetyCounters,
    authority: DurableAuthorityEvidence,
) -> _ProviderRehearsalArtifact:
    expected_role = {
        Mode.DEVELOPMENT: AccountRole.DEVELOPMENT,
        Mode.SUBMISSION_NO_TRADE: AccountRole.SUBMISSION,
    }[mode]
    expected_sdk_trace = ("get_account", "get_all_positions", "get_orders")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise RehearsalError("REHEARSAL_TIME_INVALID")
    for capture in (before, after):
        _validate_qualified_capture(
            capture,
            expected_role,
            authority.account_fingerprint,
            expected_sdk_trace,
        )
    if _book_digest(before) != _book_digest(after):
        raise RehearsalError("ACCOUNT_BOOK_CHANGED")
    if (
        not transport.operations
        or transport.rejected_writes != 0
        or any(operation.method not in READ_HTTP_METHODS for operation in transport.operations)
    ):
        raise RehearsalError("PROVIDER_TRANSPORT_TRACE_INVALID")
    if type(provider_counters) is not SafetyCounters:
        raise RehearsalError("PROVIDER_COUNTER_TRACE_INVALID")
    counters = dict(provider_counters.snapshot())
    if (
        set(counters)
        != {
            "mcp_sessions",
            "mcp_calls",
            "model_calls",
            "repository_calls",
            "runtime_execution_calls",
        }
        or counters["repository_calls"] <= 0
    ):
        raise RehearsalError("PROVIDER_COUNTER_TRACE_INVALID")
    if mode is Mode.DEVELOPMENT:
        if (
            authority.role is not AccountRole.DEVELOPMENT
            or authority.baseline_evidence_hash is not None
            or cli is None
            or mcp is None
            or type(service) is not _CanonicalServiceEvidence
            or service.terminal_code != "PROVIDER_FAILURE_NO_ACTION"
            or service.failure_code != "CONTEXT_ACTIVE_POSITION_NOT_UNIQUE"
            or service.account_fingerprint != authority.account_fingerprint
            or service.counts
            != {
                "acquisition": 1,
                "mcp": 0,
                "gemini": 0,
                "proposal": 0,
                "intent": 0,
                "execution": 0,
                "provider_write": 0,
                "repository": 4,
            }
            or counters["mcp_sessions"] != 1
            or counters["mcp_calls"] != 1
            or counters["model_calls"] != 0
            or counters["runtime_execution_calls"] != 0
        ):
            raise RehearsalError("DEVELOPMENT_PRODUCTION_QUALIFICATION_FAILED")
    elif (
        authority.role is not AccountRole.SUBMISSION
        or authority.baseline_evidence_hash is None
        or cli is not None
        or mcp is not None
        or type(service) is not _CanonicalServiceEvidence
        or service.terminal_code != "CALIBRATION_BINDING_NO_TRADE"
        or service.failure_code is not None
        or service.account_fingerprint != authority.account_fingerprint
        or any(
            service.counts[key] != 0
            for key in (
                "acquisition",
                "mcp",
                "gemini",
                "proposal",
                "intent",
                "execution",
                "provider_write",
            )
        )
        or counters["mcp_sessions"] != 0
        or counters["mcp_calls"] != 0
        or counters["model_calls"] != 0
        or counters["runtime_execution_calls"] != 0
    ):
        raise RehearsalError("SUBMISSION_PRODUCTION_QUALIFICATION_FAILED")
    source_sha256 = _source_sha256()
    invocation = {
        Mode.DEVELOPMENT: "python -m ops.launch.provider_rehearsal development",
        Mode.SUBMISSION_NO_TRADE: ("python -m ops.launch.provider_rehearsal submission-no-trade"),
    }[mode]
    public = {
        "artifact_class": "PROVIDER_REHEARSAL",
        "provider_rehearsal": True,
        "competition_evidence": False,
        "paper_endpoint_verified": True,
        "book_unchanged": True,
        "production_factory": "backend.app.runtime.build_production_agent",
        "invocation": invocation,
        "source_sha256": source_sha256,
        "provider_write_calls": 0,
        "provider_request_count": len(transport.operations),
        "provider_counters": counters,
        "clean_100k_baseline_verified": mode is Mode.SUBMISSION_NO_TRADE,
        "mcp": None
        if mcp is None
        else {
            "tool_surface_count": mcp.tool_surface_count,
            "call": "get_clock",
            "call_count": len(mcp.call_trace),
            "duration_ms": mcp.duration_ms,
        },
        "cli": (
            None
            if cli is None
            else {
                "version": cli["version"],
                "archive_verified": True,
                "binary_verified": True,
                "paper_host_verified": cli["paper_host_verified"],
                "dry_run": cli["dry_run"],
            }
        ),
        "service": {
            "terminal_code": service.terminal_code,
            "failure_code": service.failure_code,
            "counts": dict(service.counts),
        },
    }
    private = {
        "account_role": authority.role.value,
        "account_fingerprint": authority.account_fingerprint,
        "baseline_evidence_hash": authority.baseline_evidence_hash,
        "before": _capture_payload(before),
        "after": _capture_payload(after),
        "mcp_result_summary_hash": None if mcp is None else mcp.result_summary_hash,
        "cli_release": cli,
        "transport_trace": [asdict(operation) for operation in transport.operations],
        "invocation": invocation,
        "source_sha256": source_sha256,
    }
    return _ProviderRehearsalArtifact(
        mode,
        captured_at,
        private,
        public,
    )


def _source_sha256() -> str:
    descriptor = os.open(
        Path(__file__),
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise RehearsalError("REHEARSAL_SOURCE_INVALID")
        source = _read_fd(descriptor, 2 * 1024 * 1024)
    finally:
        os.close(descriptor)
    return hashlib.sha256(source).hexdigest()


def _seal_provider_rehearsal_result(
    store: ArtifactStore,
    artifact: _ProviderRehearsalArtifact,
) -> RehearsalResult:
    directory = store._seal_provider_rehearsal(artifact)
    public_bytes = directory.read_public()
    summary = _json_object(public_bytes.decode(), "ARTIFACT_VERIFICATION_FAILED")
    return RehearsalResult(directory, MappingProxyType(summary))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    runners: Mapping[str, Callable[[], Awaitable[RehearsalResult]]] = {
        Mode.DEVELOPMENT.value: run_development_operator,
        Mode.SUBMISSION_NO_TRADE.value: run_submission_no_trade_operator,
    }
    if len(arguments) != 1 or arguments[0] not in runners:
        print('{"code":"USAGE","status":"error"}', file=sys.stderr)
        return 2
    handle: ArtifactHandle | None = None
    try:
        result = asyncio.run(runners[arguments[0]]())
        handle = result.artifact_directory
        public_hash = hashlib.sha256(handle.read_public()).hexdigest()
        output = (
            _canonical_json(
                {
                    "artifact": handle.name,
                    "mode": arguments[0],
                    "public_summary_sha256": public_hash,
                    "status": "ok",
                }
            )
            .decode()
            .strip()
        )
        if len(output.encode()) > 1_024:
            raise RehearsalError("OPERATOR_OUTPUT_TOO_LARGE")
        print(output)
        return 0
    except RehearsalError as error:
        code = str(error)
        if not code or len(code) > 96 or not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "REHEARSAL_FAILED"
        print(_canonical_json({"code": code, "status": "error"}).decode().strip(), file=sys.stderr)
        return 1
    except Exception:
        print('{"code":"REHEARSAL_FAILED","status":"error"}', file=sys.stderr)
        return 1
    finally:
        if handle is not None:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
