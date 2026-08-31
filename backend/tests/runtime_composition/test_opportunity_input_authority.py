from __future__ import annotations

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
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import (
    AccountResponse,
    AccountRole,
    DataQuality,
    PositionListResponse,
)
from backend.app.policy import (
    CatalystQuality,
    OpportunityDirection,
    OpportunityOutcome,
    OpportunityPolicy,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import (
    TradingHaltState,
    derive_opportunity_direction,
    opportunity_policy_hash,
)
from backend.app.services.opportunity_input import (
    AccountBudgetAuthority,
    CatalystAuthority,
    DecimalSignalAuthority,
    OpportunityInputAuthorityError,
    OpportunitySignalAuthority,
    PriorDecisionAuthority,
    TrendSignalAuthority,
    assemble_opportunity_input,
)
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    CandidateSelectionResult,
    GreekUnitConvention,
    SelectionReason,
    select_vertical_candidate,
)

BOUNDARY = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
TRUSTED_AT = BOUNDARY + timedelta(seconds=20)
ACCOUNT_FINGERPRINT = "a" * 64
BOOK_FINGERPRINT = "b" * 64
CALL_100 = "ACME260918C00100000"
CALL_105 = "ACME260918C00105000"


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _request() -> OpportunitySnapshotRequest:
    return OpportunitySnapshotRequest(
        account_role=AccountRole.DEVELOPMENT,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        underlying="ACME",
        benchmark="QQQ",
        decision_boundary=BOUNDARY,
        minimum_expiry=date(2026, 9, 14),
        maximum_expiry=date(2026, 9, 30),
        minimum_strike=Decimal("90"),
        maximum_strike=Decimal("110"),
        maximum_contracts=16,
        maximum_quote_age=timedelta(seconds=30),
        maximum_quote_skew=timedelta(seconds=5),
    )


def _request_hash(request: OpportunitySnapshotRequest) -> str:
    return opportunity_snapshot_request_digest(request)


def _bar(symbol: str, open_: str, close: str) -> OpportunityBar:
    started_at = BOUNDARY - timedelta(minutes=5)
    values = {
        "symbol": symbol,
        "started_at": started_at.isoformat(),
        "completed_at": BOUNDARY.isoformat(),
        "open": open_,
        "high": str(Decimal(close) + 1),
        "low": str(Decimal(open_) - 1),
        "close": close,
        "volume": "1000",
        "vwap": str((Decimal(open_) + Decimal(close)) / 2),
    }
    return OpportunityBar(
        symbol=symbol,
        started_at=started_at,
        completed_at=BOUNDARY,
        open=Decimal(values["open"]),
        high=Decimal(values["high"]),
        low=Decimal(values["low"]),
        close=Decimal(values["close"]),
        volume=Decimal(values["volume"]),
        vwap=Decimal(values["vwap"]),
        source_hash=_hash(values),
    )


def _option(symbol: str, strike: str, bid: str, ask: str) -> OpportunityOption:
    values = {
        "symbol": symbol,
        "expiry": "2026-09-18",
        "right": "C",
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "bid_size": 10,
        "ask_size": 12,
        "quote_at": (TRUSTED_AT - timedelta(seconds=3)).isoformat(),
        "retrieved_at": (TRUSTED_AT - timedelta(seconds=1)).isoformat(),
        "implied_volatility": "0.35",
        "delta": "0.55",
        "gamma": "0.03",
        "theta": "-0.08",
        "vega": "0.17",
    }
    return OpportunityOption(
        symbol=symbol,
        underlying="ACME",
        expiry=date(2026, 9, 18),
        right="C",
        strike=Decimal(strike),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=12,
        quote_at=TRUSTED_AT - timedelta(seconds=3),
        retrieved_at=TRUSTED_AT - timedelta(seconds=1),
        implied_volatility=Decimal("0.35"),
        delta=Decimal("0.55"),
        gamma=Decimal("0.03"),
        theta_per_day=Decimal("-0.08"),
        vega_per_iv_point=Decimal("0.17"),
        source_hash=_hash(values),
    )


def _snapshot(
    *,
    request: OpportunitySnapshotRequest | None = None,
    options: tuple[OpportunityOption, ...] | None = None,
) -> OpportunityMarketSnapshot:
    request = request or _request()
    account = AccountResponse(
        role=AccountRole.DEVELOPMENT,
        paper=True,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        baseline_status=DataQuality.UNKNOWN,
        autonomous_enabled=False,
    )
    positions = PositionListResponse(positions=())
    book_material = {
        "account": account.model_dump(mode="json"),
        "positions": positions.model_dump(mode="json"),
        "orders": (),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
    }
    book = OpportunityAccountBook(
        account=account,
        positions=positions,
        open_orders=(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        source_hash=_hash(book_material),
    )
    session_material = {
        "date": "2026-08-31",
        "open": datetime(2026, 8, 31, 13, 30, tzinfo=UTC).isoformat(),
        "close": datetime(2026, 8, 31, 20, 0, tzinfo=UTC).isoformat(),
        "clock": (TRUSTED_AT - timedelta(seconds=2)).isoformat(),
        "market_open": True,
        "next_open": datetime(2026, 9, 1, 13, 30, tzinfo=UTC).isoformat(),
        "next_close": datetime(2026, 9, 1, 20, 0, tzinfo=UTC).isoformat(),
    }
    session = OpportunityMarketSession(
        session_date=date(2026, 8, 31),
        open_at=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 31, 20, 0, tzinfo=UTC),
        clock_at=TRUSTED_AT - timedelta(seconds=2),
        market_open=True,
        next_open_at=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        next_close_at=datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
        source_hash=_hash(session_material),
    )
    underlying = _bar("ACME", "100", "101")
    benchmark = _bar("QQQ", "500", "501")
    options = options or (
        _option(CALL_100, "100", "2.08", "2.10"),
        _option(CALL_105, "105", "0.93", "0.94"),
    )
    request_hash = _request_hash(request)
    source_hash = _hash(
        {
            "request": request_hash,
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
        request_hash=request_hash,
        source_hash=source_hash,
    )


def _policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="assembler-v1",
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        selected_decision_boundary=BOUNDARY,
        last_entry_boundary=BOUNDARY + timedelta(hours=1),
        maximum_decision_delay=timedelta(seconds=30),
        maximum_underlying_age=timedelta(seconds=30),
        maximum_catalyst_age=timedelta(minutes=15),
        maximum_option_quote_age=timedelta(seconds=30),
        maximum_leg_quote_skew=timedelta(seconds=5),
        minimum_vwap_distance=Decimal("0.003"),
        maximum_vwap_distance=Decimal("0.03"),
        minimum_relative_return=Decimal("0.0075"),
        minimum_beta=Decimal("0"),
        maximum_beta=Decimal("3"),
        required_trend_hits=3,
        maximum_first_reaction=Decimal("0.12"),
        minimum_catalyst_score=10,
        minimum_candidate_score=70,
        minimum_dte=14,
        maximum_dte=35,
        maximum_relative_spread=Decimal("0.05"),
        minimum_debit_width_fraction=Decimal("0.20"),
        maximum_debit_width_fraction=Decimal("0.60"),
        minimum_credit_width_fraction=Decimal("0.20"),
        maximum_position_loss=Decimal("1250"),
        maximum_equity_risk_fraction=Decimal("0.0125"),
        maximum_lifetime_entries=3,
        maximum_lifetime_risk=Decimal("3000"),
        equity_floor=Decimal("97500"),
        maximum_quantity=5,
    )


def _signals(snapshot: OpportunityMarketSnapshot) -> OpportunitySignalAuthority:
    at = TRUSTED_AT - timedelta(seconds=2)
    return OpportunitySignalAuthority(
        snapshot_source_hash=snapshot.source_hash,
        calculation_source_hash="1" * 64,
        beta=DecimalSignalAuthority(Decimal("1.2"), at, "2" * 64),
        vwap_distance=DecimalSignalAuthority(Decimal("0.01"), at, "3" * 64),
        relative_return=DecimalSignalAuthority(Decimal("0.008"), at, "4" * 64),
        trend=TrendSignalAuthority(3, 0, at, "5" * 64),
        absolute_first_reaction=DecimalSignalAuthority(Decimal("0.08"), at, "6" * 64),
        trading_halt_state=TradingHaltState.NOT_HALTED,
        trading_status_observed_at=at,
        trading_status_source_hash="7" * 64,
    )


def _catalyst() -> CatalystAuthority:
    return CatalystAuthority(
        opportunity_key="ACME_EVENT",
        quality=CatalystQuality.CLEAR,
        score=25,
        observed_at=TRUSTED_AT - timedelta(minutes=1),
        source_hash="8" * 64,
    )


def _account(snapshot: OpportunityMarketSnapshot) -> AccountBudgetAuthority:
    return AccountBudgetAuthority(
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        snapshot_book_source_hash=snapshot.account_book.source_hash,
        observed_at=TRUSTED_AT,
        baseline_clean=True,
        baseline_source_hash="9" * 64,
        book_fingerprint=BOOK_FINGERPRINT,
        book_source_hash="c" * 64,
        clean_equity=Decimal("100000"),
        open_position_count=0,
        open_order_count=0,
        filled_entry_count=0,
        lifetime_approved_risk=Decimal("0"),
        entry_reservation_active=False,
        reserved_approved_risk=Decimal("0"),
        event_already_attempted=False,
        history_source_hash="d" * 64,
    )


def _prior() -> PriorDecisionAuthority:
    return PriorDecisionAuthority(
        opportunity_key="ACME_EVENT",
        decision_boundary=BOUNDARY,
        outcome=None,
        observed_at=TRUSTED_AT,
        source_hash="e" * 64,
    )


def _selection_authority(snapshot: OpportunityMarketSnapshot) -> CandidateSelectionAuthority:
    return CandidateSelectionAuthority(
        snapshot_request_hash=snapshot.request_hash,
        snapshot_source_hash=snapshot.source_hash,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        observed_equity=Decimal("100000"),
        observed_buying_power=Decimal("200000"),
        available_risk=Decimal("1250"),
        available_buying_power=Decimal("200000"),
        greek_unit_convention=GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
        greek_unit_evidence_hash="f" * 64,
    )


def _assemble(**changes: object):
    request = changes.pop("request", _request())
    snapshot = changes.pop("snapshot", _snapshot(request=request))
    policy = changes.pop("policy", _policy())
    signals = changes.pop("signals", _signals(snapshot))
    direction = derive_opportunity_direction(
        policy,
        vwap_distance=signals.vwap_distance.value,
        relative_return=signals.relative_return.value,
        bull_trend_hits=signals.trend.bull_hits,
        bear_trend_hits=signals.trend.bear_hits,
    )
    requested_maximum_quantity = changes.pop("requested_maximum_quantity", 4)
    selection_authority = changes.pop(
        "selection_authority",
        _selection_authority(snapshot) if direction is not None else None,
    )
    selection = changes.pop(
        "selection",
        select_vertical_candidate(
            snapshot,
            policy,
            direction,
            requested_maximum_quantity,
            selection_authority,
        )
        if direction is not None and selection_authority is not None
        else None,
    )
    return assemble_opportunity_input(
        request=request,
        snapshot=snapshot,
        policy=policy,
        requested_maximum_quantity=requested_maximum_quantity,
        selection_authority=selection_authority,
        selection=selection,
        signals=signals,
        catalyst=changes.pop("catalyst", _catalyst()),
        account=changes.pop("account", _account(snapshot)),
        prior_decision=changes.pop("prior_decision", _prior()),
    )


def test_assembles_authorized_input_that_existing_policy_approves() -> None:
    assembled = _assemble()

    decision = evaluate_opportunity(_policy(), assembled.values)

    assert decision.outcome is OpportunityOutcome.ENTRY_APPROVED
    assert assembled.policy_hash == opportunity_policy_hash(_policy()) == decision.policy_hash
    assert len(assembled.authority_hash) == 64
    assert assembled == _assemble()


def test_snapshot_digest_helpers_preserve_the_existing_bytes() -> None:
    request = _request()
    snapshot = _snapshot(request=request)

    assert opportunity_snapshot_request_digest(request) == (
        "f8dd2b4e431282626994040130437239b11337f33f67de2435c6c351052aae3f"
    )
    assert opportunity_account_book_digest(snapshot.account_book) == (
        "cfaaca2a4e378b2871f2708b1c13aef8a78f25c881a96256ee9736267d2a12f8"
    )
    assert opportunity_market_session_digest(snapshot.session) == (
        "d0a11b66ffb74a2db17f53151cf182f5d9f78de168d62d49c375505c7d049c82"
    )
    assert opportunity_bar_digest(snapshot.underlying_bar) == (
        "46f6794e876e1c246b1a1b96604f20761e9acab7a098ba600139cbac7cbc1f97"
    )
    assert opportunity_bar_digest(snapshot.benchmark_bar) == (
        "99e2d29759aa5defb180fe1893e0d3591ccd94379485a8af933f28b558eec879"
    )
    assert tuple(opportunity_option_digest(option) for option in snapshot.options) == (
        "c459670983957aba3ed71e6a5dc7d4594080e18348d96bd515558a4ddf41a46d",
        "b5c3049b96636ae1d5a87e177a3340d37a83077bb66751ad400744a94099455c",
    )
    assert opportunity_market_snapshot_digest(snapshot) == (
        "541342953ab0c6b266f898e240dbe6f2ab502912b59af2aa22bc92c7c5022f9c"
    )


def test_missing_candidate_is_a_stable_no_trade_input() -> None:
    request = _request()
    snapshot = _snapshot(
        request=request,
        options=(_option(CALL_100, "100", "2.08", "2.10"),),
    )
    assembled = _assemble(request=request, snapshot=snapshot)

    decision = evaluate_opportunity(_policy(), assembled.values)

    assert decision.outcome is OpportunityOutcome.NO_TRADE
    assert decision.reason_codes[0].value == "OPTION_CANDIDATE_MISSING"


def test_unconfirmed_direction_skips_selection_and_yields_no_trade() -> None:
    snapshot = _snapshot()
    signals = replace(
        _signals(snapshot),
        vwap_distance=replace(_signals(snapshot).vwap_distance, value=Decimal("0.001")),
    )

    assembled = _assemble(snapshot=snapshot, signals=signals)
    decision = evaluate_opportunity(_policy(), assembled.values)

    assert assembled.values.candidate is None
    assert decision.outcome is OpportunityOutcome.NO_TRADE
    assert decision.reason_codes[0].value == "DIRECTION_NOT_CONFIRMED"
    assert decision.direction is None


def test_unconfirmed_direction_never_invokes_selector(monkeypatch) -> None:
    import backend.app.services.opportunity_input as target

    snapshot = _snapshot()
    signals = replace(
        _signals(snapshot),
        relative_return=replace(_signals(snapshot).relative_return, value=Decimal("0")),
    )

    def unexpected_selector_call(*args, **kwargs):
        raise AssertionError("selector invoked without a confirmed direction")

    monkeypatch.setattr(target, "select_vertical_candidate", unexpected_selector_call)

    assembled = _assemble(snapshot=snapshot, signals=signals)

    assert assembled.values.candidate is None


def test_unconfirmed_direction_rejects_selector_authority() -> None:
    snapshot = _snapshot()
    signals = replace(
        _signals(snapshot),
        relative_return=replace(_signals(snapshot).relative_return, value=Decimal("0")),
    )

    with pytest.raises(
        OpportunityInputAuthorityError, match="CANDIDATE_SELECTION_WITHOUT_DIRECTION"
    ):
        _assemble(
            snapshot=snapshot,
            signals=signals,
            selection_authority=_selection_authority(snapshot),
        )


def test_unconfirmed_direction_rejects_carried_candidate() -> None:
    snapshot = _snapshot()
    selected = _assemble(snapshot=snapshot).values.candidate
    assert selected is not None
    signals = replace(
        _signals(snapshot),
        relative_return=replace(_signals(snapshot).relative_return, value=Decimal("0")),
    )

    with pytest.raises(
        OpportunityInputAuthorityError, match="CANDIDATE_SELECTION_WITHOUT_DIRECTION"
    ):
        _assemble(
            snapshot=snapshot,
            signals=signals,
            selection=CandidateSelectionResult(selected, SelectionReason.SELECTED),
        )


def test_monkeypatched_external_direction_cannot_authorize_a_candidate(monkeypatch) -> None:
    monkeypatch.setitem(
        globals(),
        "derive_opportunity_direction",
        lambda *args, **kwargs: OpportunityDirection.BEARISH,
    )

    with pytest.raises(OpportunityInputAuthorityError, match="CANDIDATE_SELECTION_MISMATCH"):
        _assemble()


def test_assembly_derives_direction_once_after_validating_signals(monkeypatch) -> None:
    import backend.app.services.opportunity_input as target

    calls = 0
    canonical = target.derive_opportunity_direction

    def counted_direction(*args, **kwargs):
        nonlocal calls
        calls += 1
        return canonical(*args, **kwargs)

    monkeypatch.setattr(target, "derive_opportunity_direction", counted_direction)

    assembled = _assemble()

    assert assembled.values.candidate is not None
    assert calls == 1


def test_bearish_direction_is_derived_from_signals_before_selection() -> None:
    snapshot = _snapshot()
    signals = replace(
        _signals(snapshot),
        vwap_distance=replace(_signals(snapshot).vwap_distance, value=Decimal("-0.01")),
        relative_return=replace(_signals(snapshot).relative_return, value=Decimal("-0.008")),
        trend=replace(_signals(snapshot).trend, bull_hits=0, bear_hits=3),
    )

    assembled = _assemble(snapshot=snapshot, signals=signals)
    decision = evaluate_opportunity(_policy(), assembled.values)

    assert decision.outcome is OpportunityOutcome.ENTRY_APPROVED
    assert decision.direction is OpportunityDirection.BEARISH


@pytest.mark.parametrize(
    ("halt_state", "reason"),
    (
        (TradingHaltState.HALTED, "TRADING_HALTED"),
        (TradingHaltState.UNKNOWN, "TRADING_HALT_STATUS_UNKNOWN"),
    ),
)
def test_halt_authority_is_typed_and_fails_closed(
    halt_state: TradingHaltState, reason: str
) -> None:
    snapshot = _snapshot()
    signals = replace(_signals(snapshot), trading_halt_state=halt_state)

    decision = evaluate_opportunity(_policy(), _assemble(snapshot=snapshot, signals=signals).values)

    assert decision.outcome is OpportunityOutcome.NO_TRADE
    assert decision.reason_codes[0].value == reason


def test_raw_boolean_halt_authority_is_rejected() -> None:
    snapshot = _snapshot()
    signals = replace(_signals(snapshot), trading_halt_state=False)

    with pytest.raises(OpportunityInputAuthorityError, match="SIGNAL_AUTHORITY_INVALID"):
        _assemble(snapshot=snapshot, signals=signals)

    with pytest.raises(ValueError):
        TradingHaltState(False)

    decision = evaluate_opportunity(
        _policy(), replace(_assemble(snapshot=snapshot).values, trading_halted=False)
    )
    assert decision.outcome is OpportunityOutcome.NO_TRADE
    assert decision.reason_codes[0].value == "NORMALIZED_INPUT_INVALID"


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    (
        (
            "signals",
            lambda snapshot: replace(_signals(snapshot), snapshot_source_hash="f" * 64),
            "SIGNAL_SNAPSHOT_MISMATCH",
        ),
        (
            "account",
            lambda snapshot: replace(_account(snapshot), clean_equity=Decimal("99999")),
            "ACCOUNT_AUTHORITY_MISMATCH",
        ),
        (
            "prior_decision",
            lambda snapshot: replace(_prior(), decision_boundary=BOUNDARY + timedelta(minutes=5)),
            "PRIOR_DECISION_AUTHORITY_MISMATCH",
        ),
    ),
)
def test_authority_substitutions_fail_closed(field, replacement, code) -> None:
    snapshot = _snapshot()

    with pytest.raises(OpportunityInputAuthorityError, match=code):
        _assemble(snapshot=snapshot, **{field: replacement(snapshot)})


def test_snapshot_component_substitution_fails_hash_validation() -> None:
    snapshot = _snapshot()
    changed = replace(
        snapshot, underlying_bar=replace(snapshot.underlying_bar, close=Decimal("102"))
    )

    with pytest.raises(OpportunityInputAuthorityError, match="SNAPSHOT_COMPONENT_HASH_MISMATCH"):
        _assemble(snapshot=changed)


def test_stale_and_future_evidence_fail_closed() -> None:
    snapshot = _snapshot()
    stale = replace(
        _signals(snapshot),
        beta=replace(_signals(snapshot).beta, observed_at=TRUSTED_AT - timedelta(minutes=1)),
    )
    future = replace(_catalyst(), observed_at=TRUSTED_AT + timedelta(microseconds=1))

    with pytest.raises(OpportunityInputAuthorityError, match="SIGNAL_EVIDENCE_STALE_OR_FUTURE"):
        _assemble(snapshot=snapshot, signals=stale)
    with pytest.raises(OpportunityInputAuthorityError, match="CATALYST_EVIDENCE_STALE_OR_FUTURE"):
        _assemble(snapshot=snapshot, catalyst=future)


def test_signal_observation_cannot_predate_the_completed_boundary() -> None:
    snapshot = _snapshot()
    changed = replace(
        _signals(snapshot),
        beta=replace(
            _signals(snapshot).beta,
            observed_at=BOUNDARY - timedelta(microseconds=1),
        ),
    )

    with pytest.raises(OpportunityInputAuthorityError, match="SIGNAL_EVIDENCE_STALE_OR_FUTURE"):
        _assemble(snapshot=snapshot, signals=changed)


def test_candidate_must_be_the_exact_selected_snapshot_contracts() -> None:
    snapshot = _snapshot()
    authority = _selection_authority(snapshot)
    selected = select_vertical_candidate(
        snapshot, _policy(), OpportunityDirection.BULLISH, 4, authority
    )
    assert selected.candidate is not None
    changed = replace(selected.candidate, candidate_score=selected.candidate.candidate_score + 1)

    with pytest.raises(OpportunityInputAuthorityError, match="CANDIDATE_SELECTION_MISMATCH"):
        _assemble(
            snapshot=snapshot,
            selection_authority=authority,
            selection=replace(selected, candidate=changed),
        )


def test_no_candidate_cannot_replace_a_selected_candidate() -> None:
    with pytest.raises(OpportunityInputAuthorityError, match="CANDIDATE_SELECTION_MISMATCH"):
        _assemble(selection=CandidateSelectionResult(None, SelectionReason.NO_ELIGIBLE_STRUCTURE))


def test_snapshot_request_hash_is_recomputed_from_exact_request() -> None:
    request = replace(_request(), maximum_contracts=15)
    snapshot = _snapshot()

    with pytest.raises(OpportunityInputAuthorityError, match="SNAPSHOT_REQUEST_MISMATCH"):
        _assemble(request=request, snapshot=snapshot)


def test_selection_risk_authority_is_bound_to_durable_budget() -> None:
    snapshot = _snapshot()
    changed = replace(_selection_authority(snapshot), available_risk=Decimal("999"))

    with pytest.raises(OpportunityInputAuthorityError, match="CANDIDATE_AUTHORITY_MISMATCH"):
        _assemble(snapshot=snapshot, selection_authority=changed)


def test_prior_decision_history_must_be_current_at_assembly() -> None:
    stale = replace(_prior(), observed_at=TRUSTED_AT - timedelta(seconds=1))

    with pytest.raises(OpportunityInputAuthorityError, match="PRIOR_DECISION_AUTHORITY_MISMATCH"):
        _assemble(prior_decision=stale)


def test_prior_decision_is_carried_as_binding_policy_authority() -> None:
    prior = replace(_prior(), outcome=OpportunityOutcome.NO_TRADE)
    assembled = _assemble(prior_decision=prior)

    decision = evaluate_opportunity(_policy(), assembled.values)

    assert decision.outcome is OpportunityOutcome.NO_TRADE
    assert decision.reason_codes[0].value == "PRIOR_DECISION_BINDING"


def test_each_source_authority_changes_the_canonical_hash() -> None:
    original = _assemble()
    snapshot = _snapshot()
    changed = _assemble(
        snapshot=snapshot,
        signals=replace(
            _signals(snapshot),
            beta=replace(_signals(snapshot).beta, source_hash="f" * 64),
        ),
    )

    assert changed.values == original.values
    assert changed.authority_hash != original.authority_hash
