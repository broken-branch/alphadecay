from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from backend.app.alpaca.opportunity import OpportunitySnapshotError
from backend.app.alpaca.opportunity_runtime import OpportunitySnapshotRuntimeAdapter
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor
from backend.app.persistence.opportunity_evidence import (
    PersistedOpportunityObservation,
    opportunity_observation_digest,
)
from backend.app.policy import OpportunityOutcome, OpportunityReason
from backend.app.services.acquisition import (
    AcquisitionFailure,
    AcquisitionKind,
    ObservedPaperAccountAuthority,
)
from backend.app.services.opportunity_composition import (
    OpportunityHistoryEvidence,
    OpportunityPlanAuthority,
    OpportunitySignalEvidence,
    ProductionOpportunityComposer,
)
from backend.app.services.opportunity_selection import GreekUnitConvention

BOUNDARY = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
TRUSTED = BOUNDARY + timedelta(seconds=10)
HASHES = tuple(character * 64 for character in "abcdef1234567890")


class _SyncPort:
    def __init__(self, name: str, result: object, calls: list[str], method: str) -> None:
        self._name = name
        self._result = result
        self._calls = calls
        setattr(self, method, self._call)

    def _call(self, *args, **kwargs):
        self._calls.append(self._name)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _AsyncCatalystPort:
    def __init__(self, result: object, calls: list[str]) -> None:
        self._result = result
        self._calls = calls

    async def produce(self, **kwargs):
        self._calls.append("catalyst")
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _ObservationPort:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def append(self, spec):
        self._calls.append("observation")
        manifest_hash = opportunity_observation_digest(spec)
        return PersistedOpportunityObservation(
            observation_id=uuid5(
                NAMESPACE_URL,
                f"alphadecay:opportunity-observation:{manifest_hash}",
            ),
            plan_id=spec.plan_id,
            baseline_id=spec.baseline_id,
            account_fingerprint=spec.account_fingerprint,
            manifest_hash=manifest_hash,
            trusted_at=spec.trusted_at,
            evaluated_at=spec.evaluated_at,
        )


def _fixture():
    account_fingerprint = HASHES[0]
    policy = SimpleNamespace(
        underlying="ACME",
        opportunity_key="ACME_EARNINGS",
        selected_decision_boundary=BOUNDARY,
        last_entry_boundary=BOUNDARY + timedelta(minutes=5),
        maximum_lifetime_risk=Decimal("3000"),
        maximum_position_loss=Decimal("1250"),
    )
    request = SimpleNamespace(
        expected_account_fingerprint=account_fingerprint,
        benchmark="QQQ",
    )
    plan_id = uuid4()
    plan = SimpleNamespace(
        plan_id=plan_id,
        opportunity_key=policy.opportunity_key,
        policy_hash=HASHES[1],
        plan_hash=HASHES[0],
        frozen_at=BOUNDARY - timedelta(days=2),
    )
    baseline = SimpleNamespace(
        baseline_id=uuid4(),
        plan_id=plan_id,
        account_fingerprint=account_fingerprint,
        captured_at=BOUNDARY - timedelta(days=1),
    )
    authority = OpportunityPlanAuthority(
        plan_spec=SimpleNamespace(
            opportunity_key=policy.opportunity_key,
            underlying=policy.underlying,
            policy=policy,
            request_contract=request,
            frozen_at=plan.frozen_at,
        ),
        plan=plan,
        baseline_seal=SimpleNamespace(
            plan_id=plan_id,
            account_fingerprint=account_fingerprint,
            captured_at=baseline.captured_at,
        ),
        baseline=baseline,
        signal_request=SimpleNamespace(),
        catalyst_plan=SimpleNamespace(),
        requested_maximum_quantity=2,
    )
    book_account = SimpleNamespace(buying_power=Decimal("200000"))
    snapshot = SimpleNamespace(
        source_hash=HASHES[2],
        request_hash=HASHES[3],
        trusted_at=TRUSTED,
        account_book=SimpleNamespace(account=book_account),
        underlying_bar=SimpleNamespace(source_hash=HASHES[4]),
        benchmark_bar=SimpleNamespace(source_hash=HASHES[5]),
    )
    directional = SimpleNamespace(
        snapshot_source_hash=snapshot.source_hash,
        source_hash=HASHES[6],
        beta=SimpleNamespace(value=Decimal("1.2")),
        vwap_distance=SimpleNamespace(value=Decimal("0.02")),
        relative_return=SimpleNamespace(value=Decimal("0.015")),
        trend=SimpleNamespace(bull_hits=3, bear_hits=0),
        absolute_first_reaction=SimpleNamespace(value=Decimal("0.03")),
    )
    signal_evidence = OpportunitySignalEvidence(
        authority=directional,
        calendar_hash=HASHES[7],
        daily_hash=HASHES[8],
        intraday_hash=HASHES[9],
    )
    halt = SimpleNamespace(
        trading_halt_state=SimpleNamespace(),
        trading_status_observed_at=TRUSTED,
        trading_status_source_hash=HASHES[10],
    )
    catalyst_authority = SimpleNamespace(source_hash=HASHES[5])
    catalyst = SimpleNamespace(
        authority=catalyst_authority,
        plan_hash=HASHES[0],
        policy_hash=HASHES[1],
        criteria_hash=HASHES[2],
        research_source_hash=HASHES[3],
        classification_hash=HASHES[4],
        authority_hash=HASHES[5],
    )
    account = SimpleNamespace(
        account_fingerprint=account_fingerprint,
        clean_equity=Decimal("100000"),
        lifetime_approved_risk=Decimal("0"),
        reserved_approved_risk=Decimal("0"),
        snapshot_book_source_hash=HASHES[12],
        history_source_hash=HASHES[13],
    )
    history = OpportunityHistoryEvidence(account=account, budget_hash=HASHES[14])
    prior = SimpleNamespace(source_hash=HASHES[15])
    greek = SimpleNamespace(
        convention=GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
        evidence_hash=HASHES[5],
        authority_hash=HASHES[6],
    )
    thesis = SimpleNamespace(
        thesis_version_id=uuid4(),
        account_role=AccountRole.DEVELOPMENT,
        policy_hash=plan.policy_hash,
        underlying="ACME",
        frozen_at=TRUSTED,
    )
    return SimpleNamespace(
        authority=authority,
        policy=policy,
        snapshot=snapshot,
        signal_evidence=signal_evidence,
        halt=halt,
        catalyst=catalyst,
        history=history,
        prior=prior,
        greek=greek,
        thesis=thesis,
    )


def _composer(values, calls: list[str]) -> ProductionOpportunityComposer:
    return ProductionOpportunityComposer(
        plans=_SyncPort("plan", values.authority, calls, "load"),
        snapshots=_SyncPort("snapshot", values.snapshot, calls, "collect"),
        signals=_SyncPort("signal", values.signal_evidence, calls, "collect"),
        halts=_SyncPort("halt", values.halt, calls, "read"),
        catalysts=_AsyncCatalystPort(values.catalyst, calls),
        history=_SyncPort("history", values.history, calls, "load"),
        prior_decisions=_SyncPort("prior", values.prior, calls, "load"),
        greek_authority=_SyncPort("greek", values.greek, calls, "load"),
        observations=_ObservationPort(calls),
        theses=_SyncPort("thesis_persist", values.thesis, calls, "persist"),
    )


def _acquire(composer: ProductionOpportunityComposer, values):
    return composer.acquire(
        ObservedPaperAccountAuthority(
            AccountRole.DEVELOPMENT,
            values.authority.plan_spec.request_contract.expected_account_fingerprint,
            True,
            True,
        ),
        TRUSTED,
        uuid4(),
        actor=Actor.SCHEDULER,
    )


def test_composes_authorities_in_order_and_returns_only_the_built_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    calls: list[str] = []
    acquisition = object()
    assembly = SimpleNamespace(values=SimpleNamespace())
    decision = SimpleNamespace(outcome=OpportunityOutcome.ENTRY_APPROVED)
    selection = SimpleNamespace()
    draft = SimpleNamespace()

    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.derive_opportunity_direction",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.select_vertical_candidate",
        lambda *args, **kwargs: calls.append("selection") or selection,
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.assemble_opportunity_input",
        lambda **kwargs: calls.append("input") or assembly,
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.evaluate_opportunity",
        lambda *args, **kwargs: calls.append("decision") or decision,
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.build_frozen_opportunity_thesis",
        lambda inputs: calls.append("thesis_build") or draft,
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.build_development_entry_proposal",
        lambda inputs: calls.append("proposal") or SimpleNamespace(acquisition=acquisition),
    )

    result = asyncio.run(_acquire(_composer(values, calls), values))

    assert result is acquisition
    assert calls == [
        "plan",
        "snapshot",
        "signal",
        "halt",
        "catalyst",
        "history",
        "prior",
        "greek",
        "selection",
        "input",
        "decision",
        "observation",
        "thesis_build",
        "thesis_persist",
        "proposal",
    ]


def test_missing_plan_fails_closed_before_any_downstream_authority() -> None:
    values = _fixture()
    calls: list[str] = []
    composer = _composer(values, calls)
    composer._plans = _SyncPort("plan", RuntimeError("missing"), calls, "load")

    with pytest.raises(AcquisitionFailure) as raised:
        asyncio.run(_acquire(composer, values))

    assert raised.value.kind is AcquisitionKind.OPPORTUNITY
    assert raised.value.code == "OPPORTUNITY_PLAN_UNAVAILABLE"
    assert calls == ["plan"]


def test_adjusted_contract_reason_survives_runtime_snapshot_adapter() -> None:
    values = _fixture()
    calls: list[str] = []
    composer = _composer(values, calls)
    collector = _SyncPort(
        "snapshot",
        OpportunitySnapshotError("NON_STANDARD_CONTRACT_UNSUPPORTED"),
        calls,
        "collect",
    )
    composer._snapshots = OpportunitySnapshotRuntimeAdapter(collector)

    with pytest.raises(AcquisitionFailure) as raised:
        asyncio.run(_acquire(composer, values))

    assert raised.value.kind is AcquisitionKind.OPPORTUNITY
    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"
    assert calls == ["plan", "snapshot"]


def test_rejects_non_scheduler_or_wrong_account_before_provider_work() -> None:
    values = _fixture()
    calls: list[str] = []
    composer = _composer(values, calls)
    authority = ObservedPaperAccountAuthority(
        AccountRole.DEVELOPMENT,
        values.authority.plan_spec.request_contract.expected_account_fingerprint,
        True,
        True,
    )

    with pytest.raises(AcquisitionFailure) as wrong_actor:
        asyncio.run(composer.acquire(authority, TRUSTED, uuid4(), actor=Actor.OWNER))

    mismatched = ObservedPaperAccountAuthority(
        AccountRole.DEVELOPMENT,
        "0" * 64,
        True,
        True,
    )
    with pytest.raises(AcquisitionFailure) as wrong_account:
        asyncio.run(composer.acquire(mismatched, TRUSTED, uuid4(), actor=Actor.SCHEDULER))

    assert wrong_actor.value.code == "OPPORTUNITY_ACQUISITION_AUTHORITY_INVALID"
    assert wrong_account.value.code == "OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH"
    assert calls == ["plan"]


@pytest.mark.parametrize(
    ("trusted_at", "expected_code"),
    (
        (
            BOUNDARY - timedelta(microseconds=1),
            "OPPORTUNITY_DECISION_BOUNDARY_NOT_REACHED",
        ),
        (
            BOUNDARY + timedelta(minutes=5),
            "OPPORTUNITY_ENTRY_WINDOW_CLOSED",
        ),
    ),
)
def test_rejects_invalid_entry_time_before_provider_work(
    trusted_at: datetime,
    expected_code: str,
) -> None:
    values = _fixture()
    calls: list[str] = []
    composer = _composer(values, calls)
    authority = ObservedPaperAccountAuthority(
        AccountRole.DEVELOPMENT,
        values.authority.plan_spec.request_contract.expected_account_fingerprint,
        True,
        True,
    )

    with pytest.raises(AcquisitionFailure) as raised:
        asyncio.run(
            composer.acquire(
                authority,
                trusted_at,
                uuid4(),
                actor=Actor.SCHEDULER,
            )
        )

    assert raised.value.code == expected_code
    assert calls == ["plan"]


def test_nonapproved_decision_persists_observation_but_never_builds_a_thesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    calls: list[str] = []
    build_thesis = Mock()
    no_trade = object()
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.derive_opportunity_direction",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.assemble_opportunity_input",
        lambda **kwargs: SimpleNamespace(values=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.evaluate_opportunity",
        lambda *args, **kwargs: SimpleNamespace(
            outcome=OpportunityOutcome.NO_TRADE,
            reason_codes=(OpportunityReason.CATALYST_DATA_MISSING,),
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.build_frozen_opportunity_thesis",
        build_thesis,
    )
    build_no_trade = Mock(return_value=no_trade)
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.OpportunityNoTradeAcquisition",
        build_no_trade,
    )

    result = asyncio.run(_acquire(_composer(values, calls), values))

    assert result is no_trade
    assert calls[-1] == "observation"
    assert "thesis_persist" not in calls
    build_thesis.assert_not_called()
    build_no_trade.assert_called_once()


def test_invalid_source_identity_stops_before_selection_or_persistence() -> None:
    values = _fixture()
    values.signal_evidence = OpportunitySignalEvidence(
        authority=values.signal_evidence.authority,
        calendar_hash="not-a-hash",
        daily_hash=values.signal_evidence.daily_hash,
        intraday_hash=values.signal_evidence.intraday_hash,
    )
    calls: list[str] = []

    with pytest.raises(AcquisitionFailure) as raised:
        asyncio.run(_acquire(_composer(values, calls), values))

    assert raised.value.code == "OPPORTUNITY_SOURCE_INVALID"
    assert calls == [
        "plan",
        "snapshot",
        "signal",
        "halt",
        "catalyst",
        "history",
        "prior",
        "greek",
    ]


def test_observation_replay_mismatch_stops_before_thesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    calls: list[str] = []
    composer = _composer(values, calls)
    composer._observations = _SyncPort(
        "observation",
        PersistedOpportunityObservation(
            observation_id=uuid4(),
            plan_id=values.authority.plan.plan_id,
            baseline_id=values.authority.baseline.baseline_id,
            account_fingerprint=values.history.account.account_fingerprint,
            manifest_hash=HASHES[0],
            trusted_at=TRUSTED,
            evaluated_at=TRUSTED,
        ),
        calls,
        "append",
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.derive_opportunity_direction",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.assemble_opportunity_input",
        lambda **kwargs: SimpleNamespace(values=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "backend.app.services.opportunity_composition.evaluate_opportunity",
        lambda *args, **kwargs: SimpleNamespace(
            outcome=OpportunityOutcome.NO_TRADE,
            reason_codes=(OpportunityReason.CATALYST_DATA_MISSING,),
        ),
    )

    with pytest.raises(AcquisitionFailure) as raised:
        asyncio.run(_acquire(composer, values))

    assert raised.value.code == "OPPORTUNITY_OBSERVATION_REPLAY_MISMATCH"
    assert calls[-1] == "observation"
    assert "thesis_persist" not in calls
