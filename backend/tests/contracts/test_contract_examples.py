import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.contracts.v1 import (
    AccountRole,
    EntryApproval,
    EvidenceClassification,
    HealthResponse,
    PerformancePoint,
)
from backend.app.main import app
from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY


def test_health_contract_is_http_visible() -> None:
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert HealthResponse.model_validate(response.json()).status == "ok"


def test_canonical_examples_validate() -> None:
    models = {"health": HealthResponse, "performance-point": PerformancePoint}
    root = Path("contracts/examples/v1")
    for name, model in models.items():
        payload = json.loads((root / f"{name}.json").read_text())
        assert model.model_validate(payload).model_dump(mode="json") == payload


def test_evidence_contract_keeps_policy_units() -> None:
    classification = EvidenceClassification(
        cluster_id="cluster-1",
        source_ids=("source-1",),
        event_code="OUTLOOK_CHANGE",
        relation="CONTRADICTS",
        materiality=3,
        relevance="0.9",
        confidence="0.8",
        source_tier="PRIMARY",
        invalidates=True,
    )
    assert classification.materiality == 3
    assert str(classification.relevance) == "0.9"

    with pytest.raises(ValidationError):
        EvidenceClassification(
            cluster_id="cluster-1",
            source_ids=("source-1",),
            event_code="OUTLOOK_CHANGE",
            relation="CONTRADICTS",
            materiality=100,
            relevance=90,
            confidence=80,
            source_tier="PRIMARY",
        )


def test_entry_approval_uses_the_generic_structural_quantity_boundary() -> None:
    boundary = datetime(2026, 8, 29, 16, tzinfo=UTC)
    values = {
        "approval_id": UUID(int=1),
        "account_role": AccountRole.DEVELOPMENT,
        "decision_boundary": boundary,
        "book_fingerprint": "a" * 64,
        "policy_hash": "b" * 64,
        "selector_policy_hash": "c" * 64,
        "max_loss": "500",
        "quantity": MAX_STRUCTURAL_OPTION_QUANTITY,
        "envelope_hash": "d" * 64,
        "expires_at": boundary + timedelta(minutes=1),
    }

    assert EntryApproval(**values).quantity == MAX_STRUCTURAL_OPTION_QUANTITY
    for invalid in (0, MAX_STRUCTURAL_OPTION_QUANTITY + 1):
        with pytest.raises(ValidationError):
            EntryApproval(**{**values, "quantity": invalid})
