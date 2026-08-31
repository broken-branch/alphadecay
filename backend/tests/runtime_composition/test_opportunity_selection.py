import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.alpaca.opportunity import (
    OpportunityAccountBook,
    OpportunityBar,
    OpportunityMarketSession,
    OpportunityMarketSnapshot,
    OpportunityOption,
)
from backend.app.contracts.v1 import (
    AccountResponse,
    AccountRole,
    DataQuality,
    PositionListResponse,
)
from backend.app.policy import (
    AccountOpportunityState,
    CatalystQuality,
    OpportunityDirection,
    OpportunityInput,
    OpportunityOutcome,
    OpportunityPolicy,
    VerticalStrategy,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import TradingHaltState
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    CandidateSelectionResult,
    GreekUnitConvention,
    SelectionReason,
    select_vertical_candidate,
)

BOUNDARY = datetime(2037, 4, 15, 16, 0, tzinfo=UTC)
TRUSTED_AT = BOUNDARY + timedelta(seconds=5)
EXPIRY = date(2037, 5, 8)
DIGEST = "a" * 64
GREEK_EVIDENCE = "b" * 64


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _policy(**changes: object) -> OpportunityPolicy:
    values: dict[str, object] = {
        "version": "selection-v1",
        "opportunity_key": "ACME_EVENT",
        "underlying": "ACME",
        "selected_decision_boundary": BOUNDARY,
        "last_entry_boundary": BOUNDARY + timedelta(days=1),
        "maximum_decision_delay": timedelta(seconds=30),
        "maximum_underlying_age": timedelta(seconds=30),
        "maximum_catalyst_age": timedelta(minutes=5),
        "maximum_option_quote_age": timedelta(seconds=30),
        "maximum_leg_quote_skew": timedelta(seconds=5),
        "minimum_vwap_distance": Decimal("0.003"),
        "maximum_vwap_distance": Decimal("0.03"),
        "minimum_relative_return": Decimal("0.0075"),
        "minimum_beta": Decimal("0"),
        "maximum_beta": Decimal("4"),
        "required_trend_hits": 3,
        "maximum_first_reaction": Decimal("0.2"),
        "minimum_catalyst_score": 20,
        "minimum_candidate_score": 70,
        "minimum_dte": 19,
        "maximum_dte": 41,
        "maximum_relative_spread": Decimal("0.10"),
        "minimum_debit_width_fraction": Decimal("0.17"),
        "maximum_debit_width_fraction": Decimal("0.63"),
        "minimum_credit_width_fraction": Decimal("0.18"),
        "maximum_position_loss": Decimal("1250"),
        "maximum_equity_risk_fraction": Decimal("0.0125"),
        "maximum_lifetime_entries": 4,
        "maximum_lifetime_risk": Decimal("2500"),
        "equity_floor": Decimal("50000"),
        "maximum_quantity": 5,
    }
    values.update(changes)
    return OpportunityPolicy(**values)


def _option(
    right: str,
    strike: str,
    bid: str,
    ask: str,
    *,
    quote_at: datetime = BOUNDARY,
    expiry: date = EXPIRY,
) -> OpportunityOption:
    strike_decimal = Decimal(strike)
    occ_strike = f"{int(strike_decimal * 1000):08d}"
    symbol = f"ACME{expiry:%y%m%d}{right}{occ_strike}"
    option = OpportunityOption(
        symbol=symbol,
        underlying="ACME",
        expiry=expiry,
        right=right,
        strike=strike_decimal,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
        quote_at=quote_at,
        retrieved_at=TRUSTED_AT,
        implied_volatility=Decimal("0.3"),
        delta=Decimal("0.5") if right == "C" else Decimal("-0.5"),
        gamma=Decimal("0.03"),
        theta_per_day=Decimal("-0.08"),
        vega_per_iv_point=Decimal("0.17"),
        source_hash="",
    )
    return _seal_option(option)


def _seal_option(option: OpportunityOption) -> OpportunityOption:
    return replace(
        option,
        source_hash=_hash(
            {
                "symbol": option.symbol,
                "expiry": option.expiry.isoformat(),
                "right": option.right,
                "strike": str(option.strike),
                "bid": str(option.bid),
                "ask": str(option.ask),
                "bid_size": option.bid_size,
                "ask_size": option.ask_size,
                "quote_at": option.quote_at.isoformat(),
                "retrieved_at": option.retrieved_at.isoformat(),
                "implied_volatility": str(option.implied_volatility),
                "delta": str(option.delta),
                "gamma": str(option.gamma),
                "theta": str(option.theta_per_day),
                "vega": str(option.vega_per_iv_point),
            }
        ),
    )


def _snapshot(
    options: tuple[OpportunityOption, ...],
    *,
    open_orders: tuple[dict[str, object], ...] = (),
) -> OpportunityMarketSnapshot:
    account = AccountResponse(
        role=AccountRole.DEVELOPMENT,
        paper=True,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        baseline_status=DataQuality.COMPLETE,
        autonomous_enabled=False,
    )
    book = OpportunityAccountBook(
        account=account,
        positions=PositionListResponse(positions=()),
        open_orders=open_orders,
        account_fingerprint=DIGEST,
        source_hash=_hash(
            {
                "account": account.model_dump(mode="json"),
                "positions": PositionListResponse(positions=()).model_dump(mode="json"),
                "orders": open_orders,
                "account_fingerprint": DIGEST,
            }
        ),
    )
    session = OpportunityMarketSession(
        session_date=BOUNDARY.date(),
        open_at=BOUNDARY - timedelta(hours=2),
        close_at=BOUNDARY + timedelta(hours=4),
        clock_at=TRUSTED_AT,
        market_open=True,
        next_open_at=BOUNDARY + timedelta(days=1),
        next_close_at=BOUNDARY + timedelta(days=1, hours=6),
        source_hash="",
    )
    session = replace(
        session,
        source_hash=_hash(
            {
                "date": session.session_date.isoformat(),
                "open": session.open_at.isoformat(),
                "close": session.close_at.isoformat(),
                "clock": session.clock_at.isoformat(),
                "market_open": session.market_open,
                "next_open": session.next_open_at.isoformat(),
                "next_close": session.next_close_at.isoformat(),
            }
        ),
    )
    underlying = OpportunityBar(
        symbol="ACME",
        started_at=BOUNDARY - timedelta(minutes=5),
        completed_at=BOUNDARY,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1000"),
        vwap=Decimal("100.5"),
        source_hash="",
    )

    def seal_bar(bar: OpportunityBar) -> OpportunityBar:
        return replace(
            bar,
            source_hash=_hash(
                {
                    "symbol": bar.symbol,
                    "started_at": bar.started_at.isoformat(),
                    "completed_at": bar.completed_at.isoformat(),
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.volume),
                    "vwap": str(bar.vwap),
                }
            ),
        )

    underlying = seal_bar(underlying)
    benchmark = seal_bar(replace(underlying, symbol="QQQ", source_hash=""))
    source_hash = _hash(
        {
            "request": DIGEST,
            "book": book.source_hash,
            "session": session.source_hash,
            "underlying_bar": underlying.source_hash,
            "benchmark_bar": benchmark.source_hash,
            "options": [item.source_hash for item in options],
        }
    )
    return OpportunityMarketSnapshot(
        trusted_at=TRUSTED_AT,
        account_book=book,
        session=session,
        underlying_bar=underlying,
        benchmark_bar=benchmark,
        options=options,
        request_hash=DIGEST,
        source_hash=source_hash,
    )


def _authority(**changes: object) -> CandidateSelectionAuthority:
    snapshot = changes.pop("snapshot", None)
    request_hash = snapshot.request_hash if snapshot is not None else DIGEST
    source_hash = snapshot.source_hash if snapshot is not None else None
    values: dict[str, object] = {
        "snapshot_request_hash": request_hash,
        "snapshot_source_hash": source_hash,
        "account_fingerprint": DIGEST,
        "observed_equity": Decimal("100000"),
        "observed_buying_power": Decimal("200000"),
        "available_risk": Decimal("1250"),
        "available_buying_power": Decimal("1000"),
        "greek_unit_convention": GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
        "greek_unit_evidence_hash": GREEK_EVIDENCE,
    }
    values.update(changes)
    return CandidateSelectionAuthority(**values)


@pytest.mark.parametrize(
    ("direction", "options", "strategy", "bought_strike", "limit", "quantity"),
    (
        (
            OpportunityDirection.BULLISH,
            (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02")),
            VerticalStrategy.BULL_CALL_DEBIT,
            Decimal("100"),
            Decimal("1.02"),
            5,
        ),
        (
            OpportunityDirection.BULLISH,
            (_option("P", "95", "1.00", "1.02"), _option("P", "100", "2.00", "2.02")),
            VerticalStrategy.BULL_PUT_CREDIT,
            Decimal("95"),
            Decimal("0.98"),
            2,
        ),
        (
            OpportunityDirection.BEARISH,
            (_option("P", "95", "1.00", "1.02"), _option("P", "100", "2.00", "2.02")),
            VerticalStrategy.BEAR_PUT_DEBIT,
            Decimal("100"),
            Decimal("1.02"),
            5,
        ),
        (
            OpportunityDirection.BEARISH,
            (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02")),
            VerticalStrategy.BEAR_CALL_CREDIT,
            Decimal("105"),
            Decimal("0.98"),
            2,
        ),
    ),
)
def test_selects_each_supported_vertical_and_passes_existing_evaluator(
    direction: OpportunityDirection,
    options: tuple[OpportunityOption, ...],
    strategy: VerticalStrategy,
    bought_strike: Decimal,
    limit: Decimal,
    quantity: int,
) -> None:
    policy = _policy()
    snapshot = _snapshot(options)
    result = select_vertical_candidate(
        snapshot, policy, direction, 5, _authority(snapshot=snapshot)
    )

    assert result.reason is SelectionReason.SELECTED
    assert result.candidate is not None
    assert result.candidate.strategy is strategy
    assert result.candidate.legs[0].strike == bought_strike
    assert result.candidate.approved_limit == limit
    assert result.candidate.quantity == quantity

    bearish = direction is OpportunityDirection.BEARISH
    values = OpportunityInput(
        opportunity_key=policy.opportunity_key,
        underlying=policy.underlying,
        observed_decision_boundary=BOUNDARY,
        evaluated_at=TRUSTED_AT,
        completed_bar_at=BOUNDARY,
        decision_boundary_complete=True,
        prior_decision_outcome=None,
        data_quality=DataQuality.COMPLETE,
        market_open=True,
        trading_halted=TradingHaltState.NOT_HALTED,
        underlying_observed_at=BOUNDARY,
        catalyst_observed_at=BOUNDARY,
        catalyst_quality=CatalystQuality.CLEAR,
        catalyst_score=30,
        vwap_distance=Decimal("-0.02") if bearish else Decimal("0.02"),
        relative_return=Decimal("-0.01") if bearish else Decimal("0.01"),
        beta=Decimal("1.5"),
        bull_trend_hits=0 if bearish else 3,
        bear_trend_hits=3 if bearish else 0,
        absolute_first_reaction=Decimal("0.1"),
        candidate=result.candidate,
        account=AccountOpportunityState(
            account_role=AccountRole.DEVELOPMENT,
            book_fingerprint=DIGEST,
            baseline_clean=True,
            clean_equity=Decimal("100000"),
            open_position_count=0,
            open_order_count=0,
            filled_entry_count=0,
            lifetime_approved_risk=Decimal("0"),
            entry_reservation_active=False,
            reserved_approved_risk=Decimal("0"),
            event_already_attempted=False,
        ),
    )
    assert evaluate_opportunity(policy, values).outcome is OpportunityOutcome.ENTRY_APPROVED


def test_quantity_is_bounded_by_explicit_risk_and_buying_power() -> None:
    options = (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02"))
    snapshot = _snapshot(options)
    result = select_vertical_candidate(
        snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        6,
        _authority(
            snapshot=snapshot,
            available_risk=Decimal("250"),
            available_buying_power=Decimal("350"),
        ),
    )
    assert result.candidate is not None
    assert result.candidate.quantity == 2


def test_replay_and_input_order_are_deterministic() -> None:
    options = (
        _option("C", "100", "2.00", "2.02"),
        _option("C", "105", "1.00", "1.02"),
        _option("C", "110", "0.40", "0.42"),
    )
    first_snapshot = _snapshot(options)
    first = select_vertical_candidate(
        first_snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        4,
        _authority(snapshot=first_snapshot),
    )
    second_snapshot = _snapshot(tuple(reversed(options)))
    second = select_vertical_candidate(
        second_snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        4,
        _authority(snapshot=second_snapshot),
    )
    assert first == second


@pytest.mark.parametrize(
    "snapshot",
    (
        _snapshot(
            (
                _option("C", "100", "2.00", "2.02", quote_at=BOUNDARY - timedelta(minutes=1)),
                _option("C", "105", "1.00", "1.02"),
            )
        ),
        _snapshot(
            (
                _option("C", "100", "2.00", "2.02", quote_at=BOUNDARY),
                _option("C", "105", "1.00", "1.02", quote_at=BOUNDARY + timedelta(seconds=6)),
            )
        ),
        _snapshot((_option("C", "100", "1.00", "1.20"), _option("C", "105", "0.40", "0.42"))),
        _snapshot(
            (
                _option("C", "100", "2.00", "2.02", expiry=date(2037, 4, 20)),
                _option("C", "105", "1.00", "1.02", expiry=date(2037, 4, 20)),
            )
        ),
    ),
)
def test_stale_skewed_wide_or_wrong_dte_chain_fails_closed(
    snapshot: OpportunityMarketSnapshot,
) -> None:
    result = select_vertical_candidate(
        snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        4,
        _authority(snapshot=snapshot),
    )
    assert result.candidate is None


def test_nonpositive_natural_and_insufficient_authority_return_no_candidate() -> None:
    no_natural = (_option("C", "100", "1.00", "1.02"), _option("C", "105", "2.00", "2.02"))
    no_natural_snapshot = _snapshot(no_natural)
    assert (
        select_vertical_candidate(
            no_natural_snapshot,
            _policy(),
            OpportunityDirection.BULLISH,
            4,
            _authority(snapshot=no_natural_snapshot),
        ).reason
        is SelectionReason.NO_ELIGIBLE_STRUCTURE
    )

    eligible = (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02"))
    eligible_snapshot = _snapshot(eligible)
    result = select_vertical_candidate(
        eligible_snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        4,
        _authority(
            snapshot=eligible_snapshot,
            available_risk=Decimal("100"),
            available_buying_power=Decimal("100"),
        ),
    )
    assert result == CandidateSelectionResult(None, SelectionReason.RISK_AUTHORITY_INSUFFICIENT)


def test_authority_must_match_observed_equity_and_not_exceed_buying_power() -> None:
    options = (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02"))
    snapshot = _snapshot(options)
    for authority in (
        _authority(snapshot=snapshot, observed_equity=Decimal("99999")),
        _authority(snapshot=snapshot, observed_buying_power=Decimal("199999")),
        _authority(snapshot=snapshot, available_buying_power=Decimal("200001")),
    ):
        result = select_vertical_candidate(
            snapshot, _policy(), OpportunityDirection.BULLISH, 4, authority
        )
        assert result == CandidateSelectionResult(None, SelectionReason.INPUT_INVALID)


def test_candidate_below_policy_score_returns_no_candidate() -> None:
    options = (_option("C", "100", "2.00", "2.04"), _option("C", "105", "1.00", "1.04"))
    snapshot = _snapshot(options)
    result = select_vertical_candidate(
        snapshot,
        _policy(minimum_candidate_score=90),
        OpportunityDirection.BULLISH,
        4,
        _authority(snapshot=snapshot),
    )
    assert result == CandidateSelectionResult(None, SelectionReason.NO_ELIGIBLE_STRUCTURE)


def test_hash_shaped_or_tampered_snapshot_does_not_establish_source_integrity() -> None:
    options = (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02"))
    snapshot = _snapshot(options)
    forged = replace(snapshot, source_hash=DIGEST)
    tampered = replace(
        snapshot,
        options=(replace(snapshot.options[0], ask=Decimal("2.01")), snapshot.options[1]),
    )

    for candidate_snapshot in (forged, tampered):
        result = select_vertical_candidate(
            candidate_snapshot,
            _policy(),
            OpportunityDirection.BULLISH,
            4,
            _authority(snapshot=candidate_snapshot),
        )
        assert result == CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)

    malformed = replace(
        snapshot,
        account_book=replace(snapshot.account_book, open_orders=({"opened": object()},)),
    )
    assert select_vertical_candidate(
        malformed,
        _policy(),
        OpportunityDirection.BULLISH,
        4,
        _authority(snapshot=malformed),
    ) == CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)


def test_snapshot_and_greek_authority_are_exactly_bound() -> None:
    options = (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02"))
    snapshot = _snapshot(options)
    authorities = (
        _authority(snapshot=snapshot, snapshot_source_hash="c" * 64),
        _authority(snapshot=snapshot, snapshot_request_hash="c" * 64),
        _authority(snapshot=snapshot, account_fingerprint="c" * 64),
        _authority(snapshot=snapshot, greek_unit_evidence_hash="not-a-digest"),
        _authority(snapshot=snapshot, greek_unit_convention="ALPACA_GOPRICEOPTIONS_RAW_V1"),
    )
    for authority in authorities:
        result = select_vertical_candidate(
            snapshot, _policy(), OpportunityDirection.BULLISH, 4, authority
        )
        assert result == CandidateSelectionResult(None, SelectionReason.INPUT_INVALID)


def test_nonflat_book_and_mismatched_observed_balances_fail_closed() -> None:
    options = (_option("C", "100", "2.00", "2.02"), _option("C", "105", "1.00", "1.02"))
    snapshot = _snapshot(options)
    open_order_snapshot = _snapshot(options, open_orders=({"id": "open-order"},))

    assert select_vertical_candidate(
        open_order_snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        4,
        _authority(snapshot=open_order_snapshot),
    ) == CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)
    for authority in (
        _authority(snapshot=snapshot, observed_equity=Decimal("99999")),
        _authority(snapshot=snapshot, observed_buying_power=Decimal("199999")),
    ):
        assert select_vertical_candidate(
            snapshot, _policy(), OpportunityDirection.BULLISH, 4, authority
        ) == CandidateSelectionResult(None, SelectionReason.INPUT_INVALID)


def test_occ_identity_must_match_normalized_contract_fields() -> None:
    first = _option("C", "100", "2.00", "2.02")
    second = _option("C", "105", "1.00", "1.02")
    mismatches = (
        replace(first, symbol="ACME370508P00100000", source_hash=""),
        replace(first, symbol="ACME370508C00101000", source_hash=""),
        replace(first, symbol="OTHER370508C00100000", source_hash=""),
    )
    for mismatch in mismatches:
        snapshot = _snapshot((_seal_option(mismatch), second))
        assert select_vertical_candidate(
            snapshot,
            _policy(),
            OpportunityDirection.BULLISH,
            4,
            _authority(snapshot=snapshot),
        ) == CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)


@pytest.mark.parametrize(
    ("bought", "sold", "expected"),
    (
        ((_option("C", "100", "1.98", "2.00")), (_option("C", "105", "1.15", "1.17")), True),
        ((_option("C", "100", "1.98", "2.00")), (_option("C", "105", "1.16", "1.18")), False),
        ((_option("C", "100", "4.14", "4.16")), (_option("C", "105", "1.00", "1.02")), False),
    ),
)
def test_debit_natural_limit_respects_exact_fraction_boundaries(
    bought: OpportunityOption,
    sold: OpportunityOption,
    expected: bool,
) -> None:
    snapshot = _snapshot((bought, sold))
    result = select_vertical_candidate(
        snapshot,
        _policy(minimum_candidate_score=0),
        OpportunityDirection.BULLISH,
        1,
        _authority(snapshot=snapshot),
    )
    assert (result.candidate is not None) is expected


def test_credit_buying_power_is_bounded_by_max_loss_not_credit_received() -> None:
    options = (_option("P", "95", "1.00", "1.02"), _option("P", "100", "2.00", "2.02"))
    snapshot = _snapshot(options)
    below = select_vertical_candidate(
        snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        2,
        _authority(snapshot=snapshot, available_buying_power=Decimal("401.99")),
    )
    exact = select_vertical_candidate(
        snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        2,
        _authority(snapshot=snapshot, available_buying_power=Decimal("402")),
    )
    assert below.reason is SelectionReason.RISK_AUTHORITY_INSUFFICIENT
    assert exact.candidate is not None
    assert exact.candidate.quantity == 1
    assert exact.candidate.approved_limit == Decimal("0.98")


@pytest.mark.parametrize(
    ("sold_bid", "bought_ask", "expected"),
    (
        ("2.00", "1.10", True),
        ("1.99", "1.10", False),
        ("6.00", "1.00", False),
    ),
)
def test_credit_natural_limit_respects_minimum_and_width_boundaries(
    sold_bid: str,
    bought_ask: str,
    expected: bool,
) -> None:
    bought_bid = str(Decimal(bought_ask) - Decimal("0.02"))
    sold_ask = str(Decimal(sold_bid) + Decimal("0.02"))
    options = (
        _option("P", "95", bought_bid, bought_ask),
        _option("P", "100", sold_bid, sold_ask),
    )
    snapshot = _snapshot(options)
    result = select_vertical_candidate(
        snapshot,
        _policy(minimum_candidate_score=0),
        OpportunityDirection.BULLISH,
        1,
        _authority(snapshot=snapshot),
    )
    assert (result.candidate is not None) is expected


def test_duplicate_semantic_contract_or_direction_string_fails_closed() -> None:
    first = _option("C", "100", "2.00", "2.02")
    duplicate = _seal_option(replace(first, symbol="ACME370508C00100000"))
    duplicate_snapshot = _snapshot((first, duplicate))
    assert select_vertical_candidate(
        duplicate_snapshot,
        _policy(),
        OpportunityDirection.BULLISH,
        2,
        _authority(snapshot=duplicate_snapshot),
    ) == CandidateSelectionResult(None, SelectionReason.SNAPSHOT_INVALID)

    valid_snapshot = _snapshot((first, _option("C", "105", "1.00", "1.02")))
    assert select_vertical_candidate(
        valid_snapshot,
        _policy(),
        "BULLISH",
        2,
        _authority(snapshot=valid_snapshot),
    ) == CandidateSelectionResult(None, SelectionReason.INPUT_INVALID)
