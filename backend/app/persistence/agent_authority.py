from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from backend.app.execution import ExecutionBlocked
from backend.app.experiment_lineage import ExperimentExecutionLineage


def canonical_agent_hash(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ExecutionBlocked("AGENT_PAYLOAD_NOT_CANONICAL_JSON") from error
    return hashlib.sha256(payload.encode()).hexdigest()


def agent_input_material(
    *,
    account_role: str,
    account_fingerprint: str,
    decision_kind: str,
    decision_boundary: datetime,
    observed_at: datetime,
    normalized_input: object,
    thesis_version_id: UUID | None,
) -> dict[str, object]:
    return {
        "domain": "alphadecay.agent-input.v1",
        "account_role": account_role,
        "account_fingerprint": account_fingerprint,
        "decision_kind": decision_kind,
        "decision_boundary": _utc(decision_boundary).isoformat(),
        "observed_at": _utc(observed_at).isoformat(),
        "normalized_input": normalized_input,
        "thesis_version_id": str(thesis_version_id) if thesis_version_id else None,
    }


def agent_result_material(
    *,
    input_hash: str,
    outcome: str,
    reason_code: str,
    policy_hash: str,
    thesis_version_id: UUID | None,
    result_payload: object,
    authorization_id: UUID | None,
    intent_id: UUID | None,
    intent_digest: str | None,
    autonomy_authorized: bool,
    experiment_lineage: ExperimentExecutionLineage | None = None,
) -> dict[str, object]:
    material = {
        "domain": "alphadecay.agent-decision.v1",
        "input_hash": input_hash,
        "outcome": outcome,
        "reason_code": reason_code,
        "policy_hash": policy_hash,
        "thesis_version_id": str(thesis_version_id) if thesis_version_id else None,
        "result_payload": result_payload,
        "authorization_id": str(authorization_id) if authorization_id else None,
        "intent_id": str(intent_id) if intent_id else None,
        "intent_digest": intent_digest,
        "autonomy_authorized": autonomy_authorized,
    }
    if experiment_lineage is not None:
        material["experiment_lineage"] = experiment_lineage.material()
    return material


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
