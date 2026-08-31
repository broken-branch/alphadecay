from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from alpaca.trading.models import Calendar

from backend.app.alpaca.market_data import (
    COMPETITION_END_AT,
    FRIDAY_WEEKEND_CLOSE_AT,
    FrozenCompetitionBoundaryAuthority,
    MarketDataError,
)
from backend.app.policy import HardGateInput, evaluate_assessment
from backend.tests.core_policy_replay.test_policy import assessment_input
from backend.tests.runtime_composition.test_development_acquisition import context


def session(day: date = date(2026, 8, 31)) -> Calendar:
    return Calendar(date=day.isoformat(), open="09:30", close="16:00")


def position_context(origin_at: datetime):
    retained = context()
    return replace(
        retained,
        lifecycle_origin_at=origin_at,
        thesis_frozen_at=origin_at - timedelta(minutes=1),
        launch_authority=replace(
            retained.launch_authority,
            entry_boundary_at=origin_at - timedelta(minutes=2),
        ),
    )


@pytest.mark.parametrize(
    "origin_at",
    (
        FRIDAY_WEEKEND_CLOSE_AT - timedelta(minutes=1),
        FRIDAY_WEEKEND_CLOSE_AT,
    ),
)
def test_pre_gate_position_keeps_the_friday_weekend_boundary(origin_at: datetime) -> None:
    authority = FrozenCompetitionBoundaryAuthority().authority_for(
        context=position_context(origin_at),
        session=session(),
    )

    assert authority.short_call_close_at is None
    assert authority.weekend_close_at == FRIDAY_WEEKEND_CLOSE_AT
    assert authority.contest_end_at == COMPETITION_END_AT


def test_post_gate_position_defers_the_weekend_boundary_to_contest_end() -> None:
    authority = FrozenCompetitionBoundaryAuthority().authority_for(
        context=position_context(FRIDAY_WEEKEND_CLOSE_AT + timedelta(microseconds=1)),
        session=session(),
    )

    assert authority.short_call_close_at is None
    assert authority.weekend_close_at == COMPETITION_END_AT
    assert authority.contest_end_at == COMPETITION_END_AT


def test_contest_end_reason_wins_when_deferred_boundaries_are_equal() -> None:
    result = evaluate_assessment(
        assessment_input(
            hard_gates=HardGateInput(
                weekend_gate_failed=True,
                contest_end_window=True,
            )
        )
    )

    assert result.response.rationale_code == "CONTEST_END_CLOSE"


@pytest.mark.parametrize(
    "changed_context",
    (
        replace(context(), lifecycle_origin_at=datetime(2026, 8, 28, 19, 30)),
        replace(
            context(),
            lifecycle_origin_at=datetime(2026, 8, 28, 19, 30, tzinfo=UTC) + timedelta(hours=1),
            thesis_frozen_at=datetime(2026, 8, 28, 19, 30, tzinfo=UTC) + timedelta(hours=2),
        ),
    ),
)
def test_invalid_context_fails_closed(changed_context) -> None:
    with pytest.raises(MarketDataError, match="LIFECYCLE_BOUNDARY_CONTEXT_INVALID"):
        FrozenCompetitionBoundaryAuthority().authority_for(
            context=changed_context,
            session=session(),
        )


@pytest.mark.parametrize(
    "changed_session",
    (
        Calendar(date="2026-08-27", open="09:30", close="16:00"),
        Calendar(date="2026-09-05", open="09:30", close="16:00"),
        Calendar(date="2026-08-31", open="16:00", close="09:30"),
    ),
)
def test_invalid_session_fails_closed(changed_session: Calendar) -> None:
    with pytest.raises(MarketDataError, match="LIFECYCLE_BOUNDARY_SESSION_INVALID"):
        FrozenCompetitionBoundaryAuthority().authority_for(
            context=context(),
            session=changed_session,
        )
