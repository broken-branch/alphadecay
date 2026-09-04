from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.alpaca.opportunity import OpportunityMarketSnapshot
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_authority import (
    EntryHistoryAuthority,
    GreekUnitAuthority,
    PriorOpportunityDecisionAuthority,
)
from backend.app.persistence.opportunity_evidence import PersistedOpportunityBaseline
from backend.app.persistence.opportunity_runtime import (
    OpportunityRuntimePersistenceError,
    SQLAlchemyOpportunityGreekAuthorityAdapter,
    SQLAlchemyOpportunityHistoryAdapter,
    SQLAlchemyOpportunityObservationAdapter,
    SQLAlchemyOpportunityPlanAdapter,
    SQLAlchemyOpportunityPriorDecisionAdapter,
    SQLAlchemyOpportunityThesisAdapter,
)
from backend.app.policy import OpportunityOutcome
from backend.app.services.opportunity_selection import GreekUnitConvention

NOW = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
ACCOUNT = "a" * 64
BOOK = "b" * 64
BASELINE_HASH = "c" * 64
HISTORY_HASH = "d" * 64


class _EvidenceRepository:
    def __init__(self, plan=None, baseline=None) -> None:
        self.plan = plan
        self.baseline = baseline
        self.appended = None
        self.plan_version = None
        self.plan_role = None
        self.baseline_role = None

    def load_plan(
        self,
        opportunity_key,
        *,
        version=None,
        account_role=AccountRole.DEVELOPMENT,
    ):
        assert opportunity_key == "ACME_EVENT"
        self.plan_version = version
        self.plan_role = account_role
        return self.plan

    def load_baseline(self, plan_id, *, account_role=AccountRole.DEVELOPMENT):
        assert plan_id == self.plan.persisted.plan_id
        self.baseline_role = account_role
        return self.baseline

    def append_observation(self, spec):
        self.appended = spec
        return "observation"


class _AuthorityRepository:
    def __init__(self, *, history=None, prior=None, greek=None) -> None:
        self.history = history
        self.prior = prior
        self.greek = greek

    def load_entry_history(self, **kwargs):
        assert kwargs == {
            "expected_account_fingerprint": ACCOUNT,
            "event_key": "ACME_EVENT",
            "trading_day": date(2026, 8, 31),
        }
        return self.history

    def load_prior_opportunity_decision(self, **kwargs):
        assert kwargs["expected_opportunity_key"] == "ACME_EVENT"
        return self.prior

    def load_latest_greek_unit_authority(self, **kwargs):
        assert kwargs == {"effective_at": NOW}
        return self.greek


def _plan_and_baseline(account_role: AccountRole = AccountRole.DEVELOPMENT):
    plan_id = uuid4()
    persisted = SimpleNamespace(
        plan_id=plan_id,
        frozen_at=NOW - timedelta(minutes=1),
        plan_hash="1" * 64,
        policy_hash="2" * 64,
        account_role=account_role,
    )
    spec = SimpleNamespace(
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        daily_start_session=date(2026, 6, 1),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 8, 28),
        signal_session=date(2026, 8, 31),
        policy=SimpleNamespace(selected_decision_boundary=NOW, maximum_quantity=2),
        thesis_code="POST_EVENT_CONTINUATION",
        allowed_event_codes=("RESULTS", "GUIDANCE"),
        invalidation_codes=("RELATIVE_STRENGTH_LOST",),
        evidence_window_start=NOW - timedelta(minutes=1),
        evidence_window_end=NOW,
        frozen_at=NOW - timedelta(minutes=1),
        account_role=account_role,
    )
    plan = SimpleNamespace(persisted=persisted, spec=spec)
    baseline = PersistedOpportunityBaseline(
        baseline_id=uuid4(),
        plan_id=plan_id,
        account_fingerprint=ACCOUNT,
        baseline_hash=BASELINE_HASH,
        captured_at=NOW - timedelta(seconds=30),
        account_role=account_role,
    )
    loaded_baseline = SimpleNamespace(
        persisted=baseline,
        seal=SimpleNamespace(
            account_fingerprint=ACCOUNT,
            positions_manifest=(),
            orders_manifest=(),
            account_role=account_role,
        ),
    )
    return plan, baseline, loaded_baseline


def _snapshot(account_role: AccountRole = AccountRole.DEVELOPMENT) -> OpportunityMarketSnapshot:
    snapshot = object.__new__(OpportunityMarketSnapshot)
    book = SimpleNamespace(
        account_fingerprint=ACCOUNT,
        source_hash=BOOK,
        account=SimpleNamespace(role=account_role, equity=Decimal("100000")),
        positions=SimpleNamespace(positions=()),
        open_orders=(),
    )
    object.__setattr__(snapshot, "trusted_at", NOW)
    object.__setattr__(snapshot, "account_book", book)
    return snapshot


def test_plan_adapter_builds_exact_signal_and_catalyst_authority() -> None:
    plan, _, loaded_baseline = _plan_and_baseline()
    repository = _EvidenceRepository(plan, loaded_baseline)
    adapter = SQLAlchemyOpportunityPlanAdapter(repository, opportunity_key="ACME_EVENT")

    result = adapter.load(trusted_at=NOW)

    assert result.plan is plan.persisted
    assert result.baseline is loaded_baseline.persisted
    assert result.signal_request.daily_start_session == date(2026, 6, 1)
    assert result.signal_request.pre_event_cutoff == date(2026, 8, 27)
    assert result.catalyst_plan.allowed_event_codes == ("RESULTS", "GUIDANCE")
    assert result.catalyst_plan.evidence_window_start == NOW - timedelta(minutes=1)
    assert result.catalyst_plan.evidence_window_end == NOW
    assert result.requested_maximum_quantity == 2


def test_plan_adapter_pins_exact_configured_version() -> None:
    plan, _, loaded_baseline = _plan_and_baseline()
    repository = _EvidenceRepository(plan, loaded_baseline)
    adapter = SQLAlchemyOpportunityPlanAdapter(
        repository,
        opportunity_key="ACME_EVENT",
        version=3,
    )

    adapter.load(trusted_at=NOW)

    assert repository.plan_version == 3


def test_submission_plan_adapter_reads_exact_submission_authority() -> None:
    plan, _, loaded_baseline = _plan_and_baseline(AccountRole.SUBMISSION)
    repository = _EvidenceRepository(plan, loaded_baseline)
    adapter = SQLAlchemyOpportunityPlanAdapter(
        repository,
        opportunity_key="ACME_EVENT",
        version=3,
        account_role=AccountRole.SUBMISSION,
    )

    result = adapter.load(trusted_at=NOW)

    assert repository.plan_role is AccountRole.SUBMISSION
    assert repository.baseline_role is AccountRole.SUBMISSION
    assert result.signal_request.account_role is AccountRole.SUBMISSION


@pytest.mark.parametrize(
    ("plan", "baseline", "code"),
    (
        (None, None, "OPPORTUNITY_PLAN_MISSING"),
        (
            SimpleNamespace(
                persisted=SimpleNamespace(plan_id=uuid4(), frozen_at=NOW - timedelta(minutes=1))
            ),
            None,
            "OPPORTUNITY_BASELINE_MISSING",
        ),
        (
            SimpleNamespace(
                persisted=SimpleNamespace(plan_id=uuid4(), frozen_at=NOW + timedelta(minutes=1))
            ),
            None,
            "OPPORTUNITY_PLAN_NOT_YET_EFFECTIVE",
        ),
    ),
)
def test_plan_adapter_rejects_missing_or_future_authority(plan, baseline, code) -> None:
    repository = _EvidenceRepository(plan, baseline)
    adapter = SQLAlchemyOpportunityPlanAdapter(repository, opportunity_key="ACME_EVENT")

    with pytest.raises(OpportunityRuntimePersistenceError, match=code):
        adapter.load(trusted_at=NOW)


def test_history_adapter_binds_snapshot_book_and_exact_repository_hashes() -> None:
    plan, baseline, loaded_baseline = _plan_and_baseline()
    evidence = _EvidenceRepository(plan, loaded_baseline)
    history = EntryHistoryAuthority(
        account_fingerprint=ACCOUNT,
        clean_equity=Decimal("100000"),
        entries_used=1,
        gross_approved_risk=Decimal("400"),
        reserved_intent_id=uuid4(),
        reserved_risk=Decimal("250"),
        event_already_attempted=True,
        entry_intent_count=2,
        authority_hash=HISTORY_HASH,
    )
    adapter = SQLAlchemyOpportunityHistoryAdapter(_AuthorityRepository(history=history), evidence)

    result = adapter.load(
        expected_account_fingerprint=ACCOUNT,
        opportunity_key="ACME_EVENT",
        trading_day=date(2026, 8, 31),
        snapshot=_snapshot(),
        baseline=baseline,
    )

    assert result.budget_hash == HISTORY_HASH
    assert result.account.snapshot_book_source_hash == BOOK
    assert result.account.book_fingerprint == BOOK
    assert result.account.book_source_hash == BOOK
    assert result.account.baseline_source_hash == BASELINE_HASH
    assert result.account.history_source_hash == HISTORY_HASH
    assert result.account.filled_entry_count == 1
    assert result.account.entry_reservation_active is True
    assert result.account.baseline_clean is True


def test_submission_history_adapter_reloads_exact_submission_baseline() -> None:
    plan, baseline, loaded_baseline = _plan_and_baseline(AccountRole.SUBMISSION)
    evidence = _EvidenceRepository(plan, loaded_baseline)
    history = EntryHistoryAuthority(
        account_fingerprint=ACCOUNT,
        clean_equity=Decimal("100000"),
        entries_used=0,
        gross_approved_risk=Decimal("0"),
        reserved_intent_id=None,
        reserved_risk=Decimal("0"),
        event_already_attempted=False,
        entry_intent_count=0,
        authority_hash=HISTORY_HASH,
    )
    adapter = SQLAlchemyOpportunityHistoryAdapter(
        _AuthorityRepository(history=history),
        evidence,
        account_role=AccountRole.SUBMISSION,
    )

    result = adapter.load(
        expected_account_fingerprint=ACCOUNT,
        opportunity_key="ACME_EVENT",
        trading_day=date(2026, 8, 31),
        snapshot=_snapshot(AccountRole.SUBMISSION),
        baseline=baseline,
    )

    assert evidence.baseline_role is AccountRole.SUBMISSION
    assert result.account.account_role is AccountRole.SUBMISSION


def test_history_adapter_rejects_unbound_snapshot_account() -> None:
    plan, baseline, loaded_baseline = _plan_and_baseline()
    snapshot = _snapshot()
    snapshot.account_book.account_fingerprint = "f" * 64
    adapter = SQLAlchemyOpportunityHistoryAdapter(
        _AuthorityRepository(), _EvidenceRepository(plan, loaded_baseline)
    )

    with pytest.raises(
        OpportunityRuntimePersistenceError, match="OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH"
    ):
        adapter.load(
            expected_account_fingerprint=ACCOUNT,
            opportunity_key="ACME_EVENT",
            trading_day=date(2026, 8, 31),
            snapshot=snapshot,
            baseline=baseline,
        )


def test_prior_greek_observation_and_thesis_adapters_preserve_authority() -> None:
    prior = PriorOpportunityDecisionAuthority(
        account_fingerprint=ACCOUNT,
        opportunity_key="ACME_EVENT",
        decision_boundary=NOW - timedelta(minutes=1),
        outcome=OpportunityOutcome.NO_TRADE,
        reason_code="DIRECTION_NOT_CONFIRMED",
        observed_at=NOW,
        decision_id=uuid4(),
        source_hash=HISTORY_HASH,
    )
    greek = GreekUnitAuthority(
        authority_id=uuid4(),
        version=1,
        effective_at=NOW - timedelta(days=1),
        convention=GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
        evidence_hash="e" * 64,
        authority_hash="f" * 64,
    )
    authorities = _AuthorityRepository(prior=prior, greek=greek)

    mapped = SQLAlchemyOpportunityPriorDecisionAdapter(authorities).load(
        expected_account_fingerprint=ACCOUNT,
        opportunity_key="ACME_EVENT",
        decision_boundary=prior.decision_boundary,
        as_of=NOW,
    )
    assert mapped.source_hash == prior.source_hash
    assert mapped.outcome is OpportunityOutcome.NO_TRADE
    assert SQLAlchemyOpportunityGreekAuthorityAdapter(authorities).load(effective_at=NOW) is greek

    plan, _, loaded_baseline = _plan_and_baseline()
    evidence = _EvidenceRepository(plan, loaded_baseline)
    observation_spec = object()
    assert (
        SQLAlchemyOpportunityObservationAdapter(evidence).append(observation_spec) == "observation"
    )
    assert evidence.appended is observation_spec

    thesis_repository = SimpleNamespace(persist=lambda draft: draft)
    draft = object()
    assert SQLAlchemyOpportunityThesisAdapter(thesis_repository).persist(draft) is draft
