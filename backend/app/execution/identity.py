from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from .models import ExecutionAction, OrderEnvelope


def intent_digest(envelope: OrderEnvelope) -> str:
    payload = {
        "account_fingerprint": envelope.account_fingerprint,
        "policy_hash": envelope.policy_hash,
        "authorization_certificate_id": str(envelope.authorization_certificate_id),
        "action": envelope.action,
        "legs": [
            {"symbol": leg.symbol, "intent": leg.intent, "ratio": leg.ratio}
            for leg in envelope.legs
        ],
        "quantity": envelope.quantity,
        "minimum_limit": str(envelope.minimum_limit),
        "maximum_limit": str(envelope.maximum_limit),
        "position_or_book_fingerprint": envelope.position_or_book_fingerprint,
    }
    if envelope.action is ExecutionAction.ROLL:
        payload.update(_roll_authority_payload(envelope))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def order_envelope_hash(envelope: OrderEnvelope) -> str:
    payload = {
        "action": envelope.action,
        "legs": [
            {"symbol": leg.symbol, "intent": leg.intent, "ratio": leg.ratio}
            for leg in envelope.legs
        ],
        "quantity": envelope.quantity,
        "minimum_limit": str(envelope.minimum_limit),
        "maximum_limit": str(envelope.maximum_limit),
        "approved_max_loss": str(envelope.approved_max_loss),
        "event_key": envelope.event_key,
        "trading_day": envelope.trading_day.isoformat(),
    }
    if envelope.action is ExecutionAction.ROLL:
        payload.update(_roll_authority_payload(envelope))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _roll_authority_payload(envelope: OrderEnvelope) -> dict[str, str | None]:
    return {
        "market_session_id": (
            str(envelope.market_session_id) if envelope.market_session_id is not None else None
        ),
        "quoted_relative_spread": _decimal_or_none(envelope.quoted_relative_spread),
        "maximum_relative_spread": _decimal_or_none(envelope.maximum_relative_spread),
        "incremental_debit": _decimal_or_none(envelope.incremental_debit),
        "maximum_incremental_debit": _decimal_or_none(envelope.maximum_incremental_debit),
    }


def client_order_id(trading_day: date, action: ExecutionAction, digest: str, attempt: int) -> str:
    if len(digest) != 64 or attempt not in range(4):
        raise ValueError("INVALID_CLIENT_ORDER_ID_INPUT")
    action_code = {
        ExecutionAction.ENTRY: "e",
        ExecutionAction.CLOSE: "c",
        ExecutionAction.ROLL: "r",
    }[action]
    return f"ad-{trading_day:%Y%m%d}-{action_code}-{digest[:24]}-a{attempt}"


def attempt_request_hash(
    digest: str,
    ordinal: int,
    client_id: str,
    limit: Decimal,
    replaces_client_id: str | None,
) -> str:
    if len(digest) != 64 or ordinal not in range(4) or len(client_id) > 64:
        raise ValueError("INVALID_ATTEMPT_HASH_INPUT")
    payload = {
        "intent_digest": digest,
        "ordinal": ordinal,
        "client_order_id": client_id,
        "limit": str(limit),
        "replaces_client_order_id": replaces_client_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def replacement_request_hash(
    digest: str,
    ordinal: int,
    client_id: str,
    limit: Decimal,
    replaces_client_id: str,
    prior_request_hash: str,
    quote_hash: str,
    quote_source_timestamps: tuple[datetime, ...],
    quote_retrieved_at: datetime,
    timing_authority_at: datetime,
) -> str:
    payload = {
        "domain": "alphadecay.quote-replacement-request.v1",
        "intent_digest": digest,
        "ordinal": ordinal,
        "client_order_id": client_id,
        "limit": format(limit.normalize(), "f"),
        "replaces_client_order_id": replaces_client_id,
        "prior_request_hash": prior_request_hash,
        "quote_hash": quote_hash,
        "quote_source_timestamps": [value.isoformat() for value in quote_source_timestamps],
        "quote_retrieved_at": quote_retrieved_at.isoformat(),
        "timing_authority_at": timing_authority_at.isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
