from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.contracts.v1 import (
    BaselineStatus,
    CompetitionPerformanceProofResponse,
    PerformanceFailureCode,
    PerformancePoint,
)
from backend.app.main import app

NOW = datetime(2026, 8, 28, 17, 30, tzinfo=UTC)


def complete_point(*, normalized: bool = True) -> PerformancePoint:
    return PerformancePoint(
        scheduled_for=NOW,
        attempted_at=NOW,
        measured_at=NOW,
        status="COMPLETE",
        failure_code=None,
        current_equity_usd=Decimal("100250"),
        account_equity_change_usd=Decimal("250") if normalized else None,
        account_equity_return_pct=Decimal("0.25") if normalized else None,
        reconciled_lifecycle_cashflow_usd=Decimal("0"),
        open_position_liquidation_pnl_usd=None,
        simulator_limitations_code="ALPACA_PAPER_SIMULATION",
    )


def proof_fields() -> dict[str, object]:
    return {
        "baseline_status": None,
        "published_at": None,
        "point": None,
        "linked_certificate_ids": (),
        "publication_hash": None,
        "predecessor_hash": None,
    }


def test_unpublished_proof_cannot_contain_publication_fields() -> None:
    response = CompetitionPerformanceProofResponse(
        publication_status="NOT_PUBLISHED", **proof_fields()
    )
    assert response.point is None

    with pytest.raises(ValidationError, match="unpublished proof cannot expose values"):
        CompetitionPerformanceProofResponse(
            publication_status="NOT_PUBLISHED",
            **(proof_fields() | {"publication_hash": "a" * 64}),
        )


def test_clean_complete_publication_requires_normalized_values() -> None:
    response = CompetitionPerformanceProofResponse(
        publication_status="PUBLISHED",
        baseline_status=BaselineStatus.CLEAN,
        published_at=NOW,
        point=complete_point(),
        linked_certificate_ids=(),
        publication_hash="a" * 64,
        predecessor_hash=None,
    )
    assert response.point.account_equity_return_pct == Decimal("0.25")

    with pytest.raises(ValidationError, match="clean complete proof requires normalized values"):
        CompetitionPerformanceProofResponse(
            publication_status="PUBLISHED",
            baseline_status=BaselineStatus.CLEAN,
            published_at=NOW,
            point=complete_point(normalized=False),
            linked_certificate_ids=(),
            publication_hash="a" * 64,
            predecessor_hash=None,
        )


@pytest.mark.parametrize(
    "baseline_status",
    [
        BaselineStatus.NOT_CAPTURED,
        BaselineStatus.UNKNOWN,
        BaselineStatus.CONTAMINATED,
    ],
)
def test_nonclean_baseline_suppresses_normalized_values(
    baseline_status: BaselineStatus,
) -> None:
    with pytest.raises(ValidationError, match="nonclean proof cannot expose normalized values"):
        CompetitionPerformanceProofResponse(
            publication_status="PUBLISHED",
            baseline_status=baseline_status,
            published_at=NOW,
            point=complete_point(),
            linked_certificate_ids=(),
            publication_hash="a" * 64,
            predecessor_hash=None,
        )


@pytest.mark.parametrize("status", ["MISSING", "UNKNOWN"])
def test_incomplete_point_requires_bounded_failure_and_suppresses_values(status: str) -> None:
    point = PerformancePoint(
        scheduled_for=NOW,
        attempted_at=NOW,
        measured_at=None,
        status=status,
        failure_code=PerformanceFailureCode.PROVIDER_UNAVAILABLE,
        current_equity_usd=None,
        account_equity_change_usd=None,
        account_equity_return_pct=None,
        reconciled_lifecycle_cashflow_usd=None,
        open_position_liquidation_pnl_usd=None,
        simulator_limitations_code="ALPACA_PAPER_SIMULATION",
    )
    assert point.current_equity_usd is None

    with pytest.raises(ValidationError, match="incomplete point requires a failure code"):
        PerformancePoint(
            scheduled_for=NOW,
            attempted_at=NOW,
            measured_at=None,
            status=status,
            failure_code=None,
            current_equity_usd=None,
            account_equity_change_usd=None,
            account_equity_return_pct=None,
            reconciled_lifecycle_cashflow_usd=None,
            open_position_liquidation_pnl_usd=None,
            simulator_limitations_code="ALPACA_PAPER_SIMULATION",
        )

    with pytest.raises(ValidationError, match="incomplete point cannot expose values"):
        PerformancePoint(
            scheduled_for=NOW,
            attempted_at=NOW,
            measured_at=None,
            status=status,
            failure_code=PerformanceFailureCode.PROVIDER_UNAVAILABLE,
            current_equity_usd=Decimal("100000"),
            account_equity_change_usd=None,
            account_equity_return_pct=None,
            reconciled_lifecycle_cashflow_usd=None,
            open_position_liquidation_pnl_usd=None,
            simulator_limitations_code="ALPACA_PAPER_SIMULATION",
        )


def test_published_proof_requires_point_time_baseline_and_hash() -> None:
    with pytest.raises(ValidationError, match="published proof is incomplete"):
        CompetitionPerformanceProofResponse(publication_status="PUBLISHED", **proof_fields())


def test_public_contract_has_no_private_manifest_or_account_fields() -> None:
    fields = set(CompetitionPerformanceProofResponse.model_fields)
    point_fields = set(PerformancePoint.model_fields)

    assert not fields & {"baseline_id", "snapshot_id", "account_fingerprint"}
    assert not point_fields & {
        "cash_usd",
        "buying_power_usd",
        "account_manifest_hash",
        "position_manifest_hash",
        "order_manifest_hash",
        "activity_manifest_hash",
    }


def test_performance_times_are_timezone_aware_and_ordered() -> None:
    payload = complete_point().model_dump()
    payload["scheduled_for"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="must use UTC"):
        PerformancePoint(**payload)

    payload = complete_point().model_dump()
    payload["scheduled_for"] = NOW.astimezone(timezone(timedelta(hours=-5)))
    with pytest.raises(ValidationError, match="must use UTC"):
        PerformancePoint(**payload)

    with pytest.raises(ValidationError, match="time order"):
        PerformancePoint(
            scheduled_for=NOW,
            attempted_at=NOW.replace(hour=16),
            measured_at=NOW,
            status="COMPLETE",
            failure_code=None,
            current_equity_usd=Decimal("100000"),
            account_equity_change_usd=Decimal("0"),
            account_equity_return_pct=Decimal("0"),
            reconciled_lifecycle_cashflow_usd=None,
            open_position_liquidation_pnl_usd=None,
            simulator_limitations_code="ALPACA_PAPER_SIMULATION",
        )


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-08-28T17:30:00+00:00",
        "2026-08-28T17:30:00z",
        "2026-08-28T17:30:00.1234567Z",
    ),
)
def test_performance_timestamp_text_matches_the_public_utc_contract(timestamp: str) -> None:
    payload = complete_point().model_dump(mode="json")
    payload["scheduled_for"] = timestamp

    with pytest.raises(ValidationError, match="uppercase Z"):
        PerformancePoint.model_validate(payload)


def test_clean_publication_rejects_inconsistent_fixed_baseline_math() -> None:
    inconsistent = complete_point().model_copy(
        update={"account_equity_return_pct": Decimal("0.30")}
    )

    with pytest.raises(ValidationError, match="normalized values are inconsistent"):
        CompetitionPerformanceProofResponse(
            publication_status="PUBLISHED",
            baseline_status=BaselineStatus.CLEAN,
            published_at=NOW,
            point=inconsistent,
            linked_certificate_ids=(),
            publication_hash="a" * 64,
            predecessor_hash=None,
        )


def test_publication_time_follows_measurement_and_certificate_ids_are_canonical() -> None:
    later_point = complete_point().model_copy(update={"measured_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValidationError, match="publication time order"):
        CompetitionPerformanceProofResponse(
            publication_status="PUBLISHED",
            baseline_status=BaselineStatus.CLEAN,
            published_at=NOW,
            point=later_point,
            linked_certificate_ids=(),
            publication_hash="a" * 64,
            predecessor_hash=None,
        )

    first = UUID(int=1)
    second = UUID(int=2)
    for identifiers in ((first, first), (second, first)):
        with pytest.raises(ValidationError, match="linked certificate IDs"):
            CompetitionPerformanceProofResponse(
                publication_status="PUBLISHED",
                baseline_status=BaselineStatus.CLEAN,
                published_at=NOW,
                point=complete_point(),
                linked_certificate_ids=identifiers,
                publication_hash="a" * 64,
                predecessor_hash=None,
            )


def test_proof_decimal_serialization_and_microdollar_math_are_canonical() -> None:
    payload = complete_point().model_dump()
    payload.update(
        current_equity_usd=Decimal("1.00000000001e5"),
        account_equity_change_usd=Decimal("0.000001"),
        account_equity_return_pct=Decimal("0.000000001"),
    )
    point = PerformancePoint(**payload)
    proof = CompetitionPerformanceProofResponse(
        publication_status="PUBLISHED",
        baseline_status=BaselineStatus.CLEAN,
        published_at=NOW,
        point=point,
        linked_certificate_ids=(),
        publication_hash="a" * 64,
        predecessor_hash=None,
    )

    dumped = proof.model_dump(mode="json")
    assert dumped["point"]["current_equity_usd"] == "100000.000001"
    assert dumped["point"]["account_equity_change_usd"] == "0.000001"
    assert dumped["point"]["account_equity_return_pct"] == "0.000000001"


def test_openapi_exposes_proof_canonicalization_constraints() -> None:
    schemas = app.openapi()["components"]["schemas"]
    point = schemas["PerformancePoint"]["properties"]
    proof = schemas["CompetitionPerformanceProofResponse"]["properties"]

    assert point["current_equity_usd"]["anyOf"][0]["pattern"].endswith("{1,6})?$")
    assert point["account_equity_return_pct"]["anyOf"][0]["pattern"].endswith("{1,9})?$")
    assert point["scheduled_for"]["pattern"].endswith("Z$")
    assert proof["published_at"]["anyOf"][0]["pattern"].endswith("Z$")
    assert proof["linked_certificate_ids"]["uniqueItems"] is True
