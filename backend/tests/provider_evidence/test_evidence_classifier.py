import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import (
    EvidenceRelation,
    GreekExposure,
    SourceCluster,
    ThesisCreateRequest,
    ThesisResponse,
)
from backend.app.evidence.classifier import (
    DEFAULT_GEMINI_BINDING,
    EvidenceClassificationContext,
    EvidenceClassifier,
    EvidenceUnavailable,
    GeminiRequest,
    ModelProviderBinding,
    ModelQuotaError,
    ModelTimeoutError,
    ModelTransientError,
)
from backend.app.evidence.repository import SQLAlchemyEvidenceLedger
from backend.app.persistence.sqlalchemy_models import Base, ModelCallBudgetRow


def cluster(cluster_id: str = "cluster-1", source_id: str = "source-1") -> SourceCluster:
    return SourceCluster(
        cluster_id=cluster_id,
        source_ids=(source_id,),
        headline="Issuer narrowed its outlook.",
        observed_at=datetime(2026, 8, 28, 15, 30, tzinfo=UTC),
        source_tier="PRIMARY",
    )


def thesis() -> ThesisResponse:
    return ThesisResponse(
        thesis_id=UUID("00000000-0000-0000-0000-000000000001"),
        version=1,
        frozen=True,
        thesis_hash="thesis-hash",
        thesis=ThesisCreateRequest(
            underlying="TEST",
            thesis_code="REVENUE_OUTLOOK_IMPROVING",
            invalidation_codes=("inv-guidance",),
            intended_exposure=GreekExposure(
                delta=Decimal("0"),
                gamma=Decimal("0"),
                theta_per_day=Decimal("0"),
                vega_per_iv_point=Decimal("0"),
            ),
            source_policy_hash="source-policy-hash",
        ),
    )


def response(*, source_id: str = "source-1", extra: bool = False) -> str:
    item: dict[str, object] = {
        "cluster_id": "cluster-1",
        "source_ids": [source_id],
        "event_code": "GUIDANCE",
        "relation": "CONTRADICTS",
        "materiality": 3,
        "relevance": 0.9,
        "confidence": 0.8,
        "invalidation_condition_id": "inv-guidance",
    }
    if extra:
        item["summary"] = "free form is forbidden"
    return json.dumps({"classifications": [item]})


class FixtureGemini:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[GeminiRequest] = []

    def generate(self, request: GeminiRequest) -> str:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SelectableFixture(FixtureGemini):
    def __init__(self, outcomes: list[str | Exception], binding: ModelProviderBinding) -> None:
        super().__init__(outcomes)
        self.binding = binding

    def resolve_binding(self) -> ModelProviderBinding:
        return self.binding


class RecordingLedger:
    def __init__(self, target) -> None:
        self.target = target
        self.acquired: list[str] = []

    def acquire(self, evidence_hash: str):
        self.acquired.append(evidence_hash)
        return self.target.acquire(evidence_hash)

    def reserve_model_request(self, model: str) -> int:
        return self.target.reserve_model_request(model)

    def complete(self, lease, classifications) -> None:
        self.target.complete(lease, classifications)

    def release(self, lease) -> None:
        self.target.release(lease)

    def model_request_count(self, model: str) -> int:
        return self.target.model_request_count(model)


@pytest.fixture
def ledger(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'classifier.db'}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            ModelCallBudgetRow(
                model="gemini-3.7-flash",
                request_count=0,
                hard_limit=50,
            )
        )
    yield SQLAlchemyEvidenceLedger(sessions)
    engine.dispose()


def test_structured_classification_maps_bounded_values_and_reuses_hash(ledger) -> None:
    transport = FixtureGemini([response()])
    classifier = EvidenceClassifier(transport, ledger=ledger)

    first = classifier.classify(thesis(), (cluster(),))
    reused = classifier.classify(thesis(), (cluster(),))

    assert first == reused
    assert len(transport.requests) == 1
    assert first[0].relation == EvidenceRelation.CONTRADICTS
    assert first[0].materiality == 3
    assert first[0].relevance == Decimal("0.9")
    assert first[0].confidence == Decimal("0.8")
    assert first[0].source_tier == "PRIMARY"
    assert first[0].invalidates is True
    request = transport.requests[0]
    assert request.model == "gemini-3.7-flash"
    assert request.service_tier == "standard"
    assert request.response_mime_type == "application/json"
    assert request.thinking_level == "low"
    assert not hasattr(request, "temperature")
    assert "source-1" in request.contents
    assert "inv-guidance" in request.contents
    assert "REVENUE_OUTLOOK_IMPROVING" in request.contents
    assert "untrusted quoted data" in request.contents


def test_context_path_classifies_before_a_trade_thesis_exists(ledger) -> None:
    transport = FixtureGemini([response()])
    classifier = EvidenceClassifier(transport, ledger=ledger)
    context = EvidenceClassificationContext(
        context_hash="a" * 64,
        version=1,
        underlying="TEST",
        thesis_code="POST_EVENT_CONTINUATION",
        invalidation_condition_ids=("inv-guidance",),
    )

    classified = classifier.classify_context(context, (cluster(),))

    assert classified[0].event_code == "GUIDANCE"
    contents = json.loads(transport.requests[0].contents)
    assert "frozen_thesis" not in contents
    assert contents["classification_context"] == {
        "context_hash": "a" * 64,
        "invalidation_condition_ids": ["inv-guidance"],
        "thesis_code": "POST_EVENT_CONTINUATION",
        "underlying": "TEST",
        "version": 1,
    }


def test_lifecycle_wrapper_preserves_the_existing_evidence_hash(ledger) -> None:
    transport = FixtureGemini([response()])
    recording = RecordingLedger(ledger)

    EvidenceClassifier(transport, ledger=recording).classify(thesis(), (cluster(),))

    assert recording.acquired == [
        "08dacc69662f6b15e6d9bad59abc19b71c278276da66fedbb0b92ce13688c440"
    ]
    assert transport.requests[0].provider_binding == DEFAULT_GEMINI_BINDING


def test_cache_and_requests_bind_provider_model_and_generation(ledger) -> None:
    first_binding = ModelProviderBinding(
        provider="OWNER_GEMINI",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="owner-model",
        generation=4,
    )
    transport = SelectableFixture([response(), response()], first_binding)
    classifier = EvidenceClassifier(transport, ledger=ledger)

    first = classifier.classify(thesis(), (cluster(),))
    reused = classifier.classify(thesis(), (cluster(),))
    transport.binding = ModelProviderBinding(
        provider="OWNER_GEMINI",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="owner-model",
        generation=5,
    )
    after_rotation = classifier.classify(thesis(), (cluster(),))

    assert first == reused == after_rotation
    assert [item.provider_binding.generation for item in transport.requests] == [4, 5]
    assert [item.model for item in transport.requests] == ["owner-model", "owner-model"]
    assert all("model_provider" not in item.contents for item in transport.requests)
    assert classifier.model_calls == 2


def test_priority_service_tier_requires_explicit_classifier_configuration(ledger) -> None:
    transport = FixtureGemini([response()])

    EvidenceClassifier(transport, ledger=ledger, service_tier="priority").classify(
        thesis(), (cluster(),)
    )

    assert transport.requests[0].service_tier == "priority"


def test_unknown_source_gets_one_repair_then_succeeds(ledger) -> None:
    transport = FixtureGemini([response(source_id="invented"), response()])
    classifier = EvidenceClassifier(transport, ledger=ledger)

    result = classifier.classify(thesis(), (cluster(),))

    assert result[0].source_ids == ("source-1",)
    assert len(transport.requests) == 2
    assert transport.requests[1].validation_errors == ("EVIDENCE_UNKNOWN_SOURCE_ID",)


def test_missing_classification_gets_one_repair_then_fails_closed(ledger) -> None:
    empty = '{"classifications":[]}'
    transport = FixtureGemini([empty, empty])
    classifier = EvidenceClassifier(transport, ledger=ledger)

    with pytest.raises(EvidenceUnavailable, match="EVIDENCE_MISSING_CLUSTER_ID"):
        classifier.classify(thesis(), (cluster(),))

    assert len(transport.requests) == 2
    assert transport.requests[1].validation_errors == ("EVIDENCE_MISSING_CLUSTER_ID",)


def test_second_invalid_response_exhausts_repair_and_fails_closed(ledger) -> None:
    transport = FixtureGemini([response(extra=True), response(extra=True)])
    classifier = EvidenceClassifier(transport, ledger=ledger)

    with pytest.raises(EvidenceUnavailable, match="MODEL_SCHEMA_INVALID"):
        classifier.classify(thesis(), (cluster(),))
    assert len(transport.requests) == 2


def test_source_id_cannot_belong_to_two_clusters(ledger) -> None:
    transport = FixtureGemini([response()])
    classifier = EvidenceClassifier(transport, ledger=ledger)

    with pytest.raises(EvidenceUnavailable, match="EVIDENCE_DUPLICATE_SOURCE_ID"):
        classifier.classify(thesis(), (cluster(), cluster("cluster-2")))
    assert transport.requests == []


def test_model_input_count_and_size_are_bounded_before_provider_call(ledger) -> None:
    transport = FixtureGemini([response()])
    classifier = EvidenceClassifier(transport, ledger=ledger)
    too_many = tuple(cluster(f"cluster-{index}", f"source-{index}") for index in range(13))

    with pytest.raises(EvidenceUnavailable, match="EVIDENCE_CLUSTER_LIMIT_EXCEEDED"):
        classifier.classify(thesis(), too_many)
    with pytest.raises(EvidenceUnavailable, match="MODEL_INPUT_TOO_LARGE"):
        classifier.classify(
            thesis(),
            (cluster().model_copy(update={"headline": "x" * 20_000}),),
        )

    assert transport.requests == []


def test_unfrozen_thesis_never_reaches_model(ledger) -> None:
    transport = FixtureGemini([response()])
    classifier = EvidenceClassifier(transport, ledger=ledger)

    with pytest.raises(EvidenceUnavailable, match="THESIS_NOT_FROZEN"):
        classifier.classify(thesis().model_copy(update={"frozen": False}), (cluster(),))

    assert transport.requests == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ModelTimeoutError(), "MODEL_TIMEOUT"),
        (ModelQuotaError(), "MODEL_QUOTA"),
    ],
)
def test_timeout_and_quota_fail_closed_without_cached_action(
    error: Exception, code: str, ledger
) -> None:
    classifier = EvidenceClassifier(FixtureGemini([error]), ledger=ledger)

    with pytest.raises(EvidenceUnavailable, match=code):
        classifier.classify(thesis(), (cluster(),))


def test_transient_provider_failures_retry_twice_inside_one_total_deadline(ledger) -> None:
    delays: list[float] = []
    transport = FixtureGemini([ModelTransientError(), ModelTransientError(), response()])
    classifier = EvidenceClassifier(transport, ledger=ledger, sleeper=delays.append)

    result = classifier.classify(thesis(), (cluster(),))

    assert result[0].cluster_id == "cluster-1"
    assert classifier.model_calls == 3
    assert delays == [1.0, 3.0]


def test_schema_repair_uses_only_the_remaining_total_deadline(ledger) -> None:
    class Clock:
        value = 100.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    class SlowFirstResponse(FixtureGemini):
        def generate(self, request: GeminiRequest) -> str:
            raw = super().generate(request)
            if len(self.requests) == 1:
                clock.value += 12
            return raw

    transport = SlowFirstResponse([response(extra=True), response()])
    classifier = EvidenceClassifier(transport, ledger=ledger, clock=clock)

    classifier.classify(thesis(), (cluster(),))

    assert transport.requests[0].timeout_ms == 20_000
    assert 17_000 <= transport.requests[1].timeout_ms <= 18_000
