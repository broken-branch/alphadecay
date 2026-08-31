from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ExecutionBlocked, order_envelope_hash
from backend.app.execution.models import ExecutionIntent


@dataclass(frozen=True)
class AuthorizationValues:
    account_role: AccountRole
    policy_hash: str
    fingerprint: str
    envelope_hash: str
    approved_max_loss: Decimal
    quantity: int
    valid: bool
    valid_from: datetime
    expires_at: datetime


def validate_authorization(
    intent: ExecutionIntent, authorization: AuthorizationValues, now: datetime
) -> None:
    envelope = intent.envelope
    if authorization.account_role != intent.account_role:
        raise ExecutionBlocked("AUTHORIZATION_ACCOUNT_ROLE_MISMATCH")
    if authorization.policy_hash != envelope.policy_hash:
        raise ExecutionBlocked("AUTHORIZATION_POLICY_MISMATCH")
    if authorization.fingerprint != envelope.position_or_book_fingerprint:
        raise ExecutionBlocked("AUTHORIZATION_FINGERPRINT_MISMATCH")
    if authorization.envelope_hash != order_envelope_hash(envelope):
        raise ExecutionBlocked("AUTHORIZATION_ENVELOPE_MISMATCH")
    if authorization.approved_max_loss != envelope.approved_max_loss:
        raise ExecutionBlocked("AUTHORIZATION_RISK_MISMATCH")
    if authorization.quantity != envelope.quantity:
        raise ExecutionBlocked("AUTHORIZATION_QUANTITY_MISMATCH")
    if not authorization.valid:
        raise ExecutionBlocked("AUTHORIZATION_INVALID")
    if now < authorization.valid_from:
        raise ExecutionBlocked("AUTHORIZATION_NOT_YET_VALID")
    if now >= authorization.expires_at:
        raise ExecutionBlocked("AUTHORIZATION_EXPIRED")
