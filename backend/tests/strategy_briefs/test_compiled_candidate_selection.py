from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.alpaca.opportunity import (
    OpportunityAccountBook,
    OpportunityBar,
    OpportunityMarketSession,
    OpportunityMarketSnapshot,
    OpportunityOption,
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import AccountResponse, AccountRole, DataQuality, PositionListResponse
from backend.app.policy.opportunity import VerticalStrategy
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    GreekUnitConvention,
)
from backend.app.strategy_briefs.decision import PositionPhase
from backend.app.strategy_briefs.models import CuratedStructure, StrategyDirection
from backend.app.strategy_briefs.protocol import compile_reviewed_protocol
from backend.app.strategy_briefs.selection import (
    CompiledCandidateReason,
    CompiledCandidateSelectionBlocked,
    select_compiled_vertical_candidate,
)
from backend.app.strategy_briefs.tick import run_compiled_experiment_tick
from backend.tests.strategy_briefs.test_executable_protocol import curation, request
from backend.tests.strategy_briefs.test_protocol_observations import evidence

NOW = datetime(2026, 9, 2, 14, tzinfo=UTC)
EXPIRY = datetime(2026, 10, 9, tzinfo=UTC).date()
FINGERPRINT = "a" * 64
GREEK_HASH = "b" * 64


def protocol(*, bearish: bool = False, quantity: int = 1, max_loss: str = "225"):
    curated = curation()
    if bearish:
        curated = curated.model_copy(
            update={
                "intake": curated.intake.model_copy(
                    update={"direction": StrategyDirection.BEARISH}
                ),
                "classifications": curated.classifications.model_copy(
                    update={
                        "direction": StrategyDirection.BEARISH,
                        "structure": CuratedStructure.BEAR_PUT_DEBIT_SPREAD,
                    }
                ),
            }
        )
    curated = curated.model_copy(
        update={
            "intake": curated.intake.model_copy(
                update={
                    "risk_budget": curated.intake.risk_budget.model_copy(
                        update={"max_loss_dollars": Decimal(max_loss)}
                    )
                }
            )
        }
    )
    value = request(curated)
    selection = value.definition.selection.model_copy(
        update={
            "quantity": quantity,
            "maximum_loss_dollars": Decimal(max_loss),
            "maximum_debit_per_share": Decimal(max_loss) / Decimal(quantity * 100),
        }
    )
    return compile_reviewed_protocol(
        value.model_copy(
            update={"definition": value.definition.model_copy(update={"selection": selection})}
        )
    )


def option(
    right: str,
    strike: str,
    bid: str,
    ask: str,
    *,
    expiry=EXPIRY,
    quote_at=NOW - timedelta(seconds=2),
    bid_size: int = 10,
    ask_size: int = 10,
) -> OpportunityOption:
    strike_value = Decimal(strike)
    symbol = f"SPY{expiry:%y%m%d}{right}{int(strike_value * 1000):08d}"
    value = OpportunityOption(
        symbol=symbol,
        underlying="SPY",
        expiry=expiry,
        right=right,
        strike=strike_value,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=bid_size,
        ask_size=ask_size,
        quote_at=quote_at,
        retrieved_at=NOW - timedelta(seconds=1),
        implied_volatility=Decimal("0.3"),
        delta=Decimal("0.5") if right == "C" else Decimal("-0.5"),
        gamma=Decimal("0.02"),
        theta_per_day=Decimal("-0.05"),
        vega_per_iv_point=Decimal("0.1"),
        source_hash="",
    )
    return replace(value, source_hash=opportunity_option_digest(value))


def snapshot(protocol_value, options: tuple[OpportunityOption, ...]) -> OpportunityMarketSnapshot:
    account = AccountResponse(
        role=AccountRole.DEVELOPMENT,
        paper=True,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        baseline_status=DataQuality.COMPLETE,
        autonomous_enabled=False,
    )
    book = OpportunityAccountBook(account, PositionListResponse(positions=()), (), FINGERPRINT, "")
    book = replace(book, source_hash=opportunity_account_book_digest(book))
    session = OpportunityMarketSession(
        NOW.date(),
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=5),
        NOW,
        True,
        NOW + timedelta(days=1),
        NOW + timedelta(days=1, hours=6),
        "",
    )
    session = replace(session, source_hash=opportunity_market_session_digest(session))
    boundary = protocol_value.definition.schedule.decision_boundary
    underlying = OpportunityBar(
        "SPY",
        boundary - timedelta(minutes=5),
        boundary,
        Decimal("500"),
        Decimal("502"),
        Decimal("499"),
        Decimal("501"),
        Decimal("1000"),
        Decimal("500.5"),
        "",
    )
    underlying = replace(underlying, source_hash=opportunity_bar_digest(underlying))
    benchmark = replace(underlying, symbol="QQQ", source_hash="")
    benchmark = replace(benchmark, source_hash=opportunity_bar_digest(benchmark))
    selection = protocol_value.definition.selection
    quality = protocol_value.definition.market_quality
    request_value = OpportunitySnapshotRequest(
        expected_account_fingerprint=FINGERPRINT,
        underlying="SPY",
        benchmark="QQQ",
        decision_boundary=boundary,
        minimum_expiry=selection.minimum_expiry,
        maximum_expiry=selection.maximum_expiry,
        minimum_strike=selection.minimum_strike,
        maximum_strike=selection.maximum_strike,
        maximum_contracts=selection.maximum_contracts_considered,
        maximum_quote_age=timedelta(seconds=quality.maximum_option_quote_age_seconds),
        maximum_quote_skew=timedelta(seconds=quality.maximum_leg_quote_skew_seconds),
    )
    value = OpportunityMarketSnapshot(
        NOW,
        book,
        session,
        underlying,
        benchmark,
        options,
        opportunity_snapshot_request_digest(request_value),
        "",
    )
    return replace(value, source_hash=opportunity_market_snapshot_digest(value))


def authority(value: OpportunityMarketSnapshot, **updates) -> CandidateSelectionAuthority:
    values = {
        "snapshot_request_hash": value.request_hash,
        "snapshot_source_hash": value.source_hash,
        "account_fingerprint": FINGERPRINT,
        "observed_equity": Decimal("100000"),
        "observed_buying_power": Decimal("200000"),
        "available_risk": Decimal("1000"),
        "available_buying_power": Decimal("1000"),
        "greek_unit_convention": GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
        "greek_unit_evidence_hash": GREEK_HASH,
    }
    values.update(updates)
    return CandidateSelectionAuthority(**values)


def tick(protocol_value):
    return run_compiled_experiment_tick(
        protocol_value, evidence(protocol_value, PositionPhase.FLAT), PositionPhase.FLAT
    )


@pytest.mark.parametrize(
    ("bearish", "options", "strategy", "bought_strike"),
    (
        (
            False,
            (option("C", "500", "2", "2.02"), option("C", "504", "0.5", "0.52")),
            VerticalStrategy.BULL_CALL_DEBIT,
            Decimal("500"),
        ),
        (
            True,
            (option("P", "500", "0.5", "0.52"), option("P", "504", "2", "2.02")),
            VerticalStrategy.BEAR_PUT_DEBIT,
            Decimal("504"),
        ),
    ),
)
def test_selects_reviewed_bull_call_or_bear_put(bearish, options, strategy, bought_strike) -> None:
    compiled = protocol(bearish=bearish)
    market = snapshot(compiled, options)

    result = select_compiled_vertical_candidate(compiled, tick(compiled), market, authority(market))

    assert result.reason is CompiledCandidateReason.SELECTED
    assert result.candidate.strategy is strategy
    assert result.candidate.legs[0].strike == bought_strike
    assert result.candidate.quantity == compiled.definition.selection.quantity
    assert result.authority_state == "NON_AUTHORITATIVE"
    assert result.execution_eligible is False
    assert set(result.leg_source_hashes) == {item.source_hash for item in options}


def test_uses_exact_reviewed_quantity_without_resizing() -> None:
    compiled = protocol(quantity=2)
    market = snapshot(
        compiled,
        (option("C", "500", "1.5", "1.52"), option("C", "504", "0.5", "0.52")),
    )

    result = select_compiled_vertical_candidate(compiled, tick(compiled), market, authority(market))

    assert result.reason is CompiledCandidateReason.SELECTED
    assert result.candidate.quantity == 2


@pytest.mark.parametrize(
    "options",
    (
        (
            option("C", "500", "2", "2.02", quote_at=NOW - timedelta(seconds=21)),
            option("C", "504", "0.5", "0.52"),
        ),
        (
            option("C", "500", "2", "2.02", quote_at=NOW - timedelta(seconds=1)),
            option("C", "504", "0.5", "0.52", quote_at=NOW - timedelta(seconds=5)),
        ),
        (option("C", "500", "1", "2"), option("C", "504", "0.5", "0.52")),
        (option("C", "500", "2", "2.02", bid_size=0), option("C", "504", "0.5", "0.52")),
        (
            option("C", "500", "2", "2.02", expiry=datetime(2026, 9, 10, tzinfo=UTC).date()),
            option("C", "504", "0.5", "0.52", expiry=datetime(2026, 9, 10, tzinfo=UTC).date()),
        ),
        (option("C", "500", "2", "2.02"), option("C", "505", "0.5", "0.52")),
        (option("C", "396", "2", "2.02"), option("C", "400", "0.5", "0.52")),
        (option("C", "500", "3", "3.02"), option("C", "504", "0.5", "0.52")),
    ),
)
def test_ineligible_quote_geometry_or_debit_returns_no_candidate(options) -> None:
    compiled = protocol()
    market = snapshot(compiled, options)
    result = select_compiled_vertical_candidate(compiled, tick(compiled), market, authority(market))
    assert result.candidate is None


def test_insufficient_risk_for_exact_quantity_returns_no_candidate() -> None:
    compiled = protocol()
    market = snapshot(
        compiled,
        (option("C", "500", "1", "1.02"), option("C", "504", "0.5", "0.52")),
    )
    result = select_compiled_vertical_candidate(
        compiled,
        tick(compiled),
        market,
        authority(market, available_risk=Decimal("50"), available_buying_power=Decimal("50")),
    )
    assert result.reason is CompiledCandidateReason.RISK_AUTHORITY_INSUFFICIENT


def test_non_entry_tick_or_identity_mismatch_is_rejected() -> None:
    compiled = protocol()
    market = snapshot(
        compiled,
        (option("C", "500", "2", "2.02"), option("C", "504", "0.5", "0.52")),
    )
    with pytest.raises(CompiledCandidateSelectionBlocked, match="TICK_INVALID"):
        select_compiled_vertical_candidate(
            compiled,
            tick(compiled).model_copy(update={"position_phase": PositionPhase.OPEN}),
            market,
            authority(market),
        )
    with pytest.raises(CompiledCandidateSelectionBlocked, match="ACCOUNT_INVALID"):
        select_compiled_vertical_candidate(
            compiled,
            tick(compiled),
            market,
            authority(market, account_fingerprint="c" * 64),
        )


def test_hash_or_symbol_mismatch_is_rejected() -> None:
    compiled = protocol()
    market = snapshot(
        compiled,
        (option("C", "500", "2", "2.02"), option("C", "504", "0.5", "0.52")),
    )
    corrupted = replace(market, source_hash="d" * 64)
    with pytest.raises(CompiledCandidateSelectionBlocked, match="SNAPSHOT_INVALID"):
        select_compiled_vertical_candidate(
            compiled,
            tick(compiled),
            corrupted,
            authority(corrupted),
        )
    with pytest.raises(CompiledCandidateSelectionBlocked, match="BINDING_MISMATCH"):
        select_compiled_vertical_candidate(
            compiled,
            tick(compiled),
            replace(market, underlying_bar=replace(market.underlying_bar, symbol="QQQ")),
            authority(market),
        )


def test_ranking_is_deterministic_and_no_eligible_candidate_is_explicit() -> None:
    compiled = protocol()
    options = (
        option("C", "500", "2", "2.02"),
        option("C", "504", "0.5", "0.52"),
        option("C", "510", "1.8", "1.82"),
        option("C", "514", "0.5", "0.52"),
    )
    market = snapshot(compiled, options)
    first = select_compiled_vertical_candidate(compiled, tick(compiled), market, authority(market))
    second = select_compiled_vertical_candidate(compiled, tick(compiled), market, authority(market))
    assert first == second
    assert first.selection_basis.order[0] == "TARGET_DTE_DISTANCE"

    empty = snapshot(compiled, (option("P", "500", "1", "1.02"),))
    result = select_compiled_vertical_candidate(compiled, tick(compiled), empty, authority(empty))
    assert result.reason is CompiledCandidateReason.NO_ELIGIBLE_CANDIDATE


def test_exact_duplicate_rank_is_reported_as_unresolved_tie() -> None:
    compiled = protocol()
    lower = option("C", "500", "2", "2.02")
    upper = option("C", "504", "0.5", "0.52")
    market = snapshot(compiled, (lower, upper, lower, upper))

    result = select_compiled_vertical_candidate(compiled, tick(compiled), market, authority(market))

    assert result.reason is CompiledCandidateReason.UNRESOLVED_TIE
    assert result.candidate is None
