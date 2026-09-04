from __future__ import annotations

from datetime import UTC, date, datetime

from backend.app.alpaca.opportunity import OpportunityMarketSnapshot
from backend.app.alpaca.opportunity_signals import OpportunitySignalRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import FrozenThesisVersion
from backend.app.services.opportunity_catalyst import CatalystEvidencePlan
from backend.app.services.opportunity_composition import (
    OpportunityHistoryEvidence,
    OpportunityPlanAuthority,
)
from backend.app.services.opportunity_input import (
    AccountBudgetAuthority,
    PriorDecisionAuthority,
)
from backend.app.services.opportunity_thesis import SQLAlchemyOpportunityThesisRepository

from .opportunity_authority import (
    GreekUnitAuthority,
    SQLAlchemyOpportunityAuthorityRepository,
)
from .opportunity_evidence import (
    OpportunityObservationSpec,
    PersistedOpportunityBaseline,
    PersistedOpportunityObservation,
    SQLAlchemyOpportunityEvidenceRepository,
)


class OpportunityRuntimePersistenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SQLAlchemyOpportunityPlanAdapter:
    def __init__(
        self,
        repository: SQLAlchemyOpportunityEvidenceRepository,
        *,
        opportunity_key: str,
        version: int | None = None,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
    ) -> None:
        if account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise ValueError("OPPORTUNITY_ACCOUNT_ROLE_INVALID")
        self._repository = repository
        self._opportunity_key = opportunity_key
        self._version = version
        self._account_role = account_role

    def load(self, *, trusted_at: datetime) -> OpportunityPlanAuthority:
        read_at = _utc(trusted_at, "OPPORTUNITY_PLAN_READ_TIME_INVALID")
        if self._version is None:
            loaded_plan = self._repository.load_plan(
                self._opportunity_key,
                account_role=self._account_role,
            )
        else:
            loaded_plan = self._repository.load_plan(
                self._opportunity_key,
                version=self._version,
                account_role=self._account_role,
            )
        if loaded_plan is None:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_PLAN_MISSING")
        if loaded_plan.persisted.frozen_at > read_at:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_PLAN_NOT_YET_EFFECTIVE")
        loaded_baseline = self._repository.load_baseline(
            loaded_plan.persisted.plan_id,
            account_role=self._account_role,
        )
        if loaded_baseline is None:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_BASELINE_MISSING")
        spec = loaded_plan.spec
        if (
            getattr(spec, "account_role", AccountRole.DEVELOPMENT) is not self._account_role
            or getattr(loaded_plan.persisted, "account_role", AccountRole.DEVELOPMENT)
            is not self._account_role
            or getattr(loaded_baseline.seal, "account_role", AccountRole.DEVELOPMENT)
            is not self._account_role
            or getattr(loaded_baseline.persisted, "account_role", AccountRole.DEVELOPMENT)
            is not self._account_role
        ):
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_ACCOUNT_ROLE_MISMATCH")
        signal_request = OpportunitySignalRequest(
            account_role=self._account_role,
            underlying=spec.underlying,
            benchmark="QQQ",
            daily_start_session=spec.daily_start_session,
            pre_event_cutoff=spec.pre_event_session,
            first_reaction_session=spec.reaction_session,
            signal_session=spec.signal_session,
            signal_boundary=spec.policy.selected_decision_boundary,
        )
        catalyst_plan = CatalystEvidencePlan(
            opportunity_key=spec.opportunity_key,
            underlying=spec.underlying,
            plan_hash=loaded_plan.persisted.plan_hash,
            policy_hash=loaded_plan.persisted.policy_hash,
            thesis_code=spec.thesis_code,
            allowed_event_codes=spec.allowed_event_codes,
            invalidation_codes=spec.invalidation_codes,
            evidence_window_start=spec.evidence_window_start,
            evidence_window_end=spec.evidence_window_end,
            frozen_at=spec.frozen_at,
        )
        return OpportunityPlanAuthority(
            plan_spec=spec,
            plan=loaded_plan.persisted,
            baseline_seal=loaded_baseline.seal,
            baseline=loaded_baseline.persisted,
            signal_request=signal_request,
            catalyst_plan=catalyst_plan,
            requested_maximum_quantity=spec.policy.maximum_quantity,
        )


class SQLAlchemyOpportunityHistoryAdapter:
    def __init__(
        self,
        authority_repository: SQLAlchemyOpportunityAuthorityRepository,
        evidence_repository: SQLAlchemyOpportunityEvidenceRepository,
        *,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
    ) -> None:
        if account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise ValueError("OPPORTUNITY_ACCOUNT_ROLE_INVALID")
        self._authorities = authority_repository
        self._evidence = evidence_repository
        self._account_role = account_role

    def load(
        self,
        *,
        expected_account_fingerprint: str,
        opportunity_key: str,
        trading_day: date,
        snapshot: OpportunityMarketSnapshot,
        baseline: PersistedOpportunityBaseline,
    ) -> OpportunityHistoryEvidence:
        if type(snapshot) is not OpportunityMarketSnapshot:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_SNAPSHOT_AUTHORITY_INVALID")
        if type(baseline) is not PersistedOpportunityBaseline:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_BASELINE_AUTHORITY_INVALID")
        loaded_baseline = self._evidence.load_baseline(
            baseline.plan_id,
            account_role=self._account_role,
        )
        if loaded_baseline is None or loaded_baseline.persisted != baseline:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_BASELINE_AUTHORITY_MISMATCH")
        seal = loaded_baseline.seal
        book = snapshot.account_book
        if (
            seal.account_fingerprint != expected_account_fingerprint
            or getattr(seal, "account_role", AccountRole.DEVELOPMENT) is not self._account_role
            or getattr(baseline, "account_role", AccountRole.DEVELOPMENT) is not self._account_role
            or baseline.account_fingerprint != expected_account_fingerprint
            or book.account_fingerprint != expected_account_fingerprint
            or book.account.role is not self._account_role
        ):
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH")

        history = self._authorities.load_entry_history(
            expected_account_fingerprint=expected_account_fingerprint,
            event_key=opportunity_key,
            trading_day=trading_day,
        )
        # A clean book's equity must equal the durable account equity exactly. With
        # positions open, equity moves with option marks between reads, so the book is
        # bound through the reconciliation state instead and equity only has to be sane.
        if history.account_fingerprint != expected_account_fingerprint or (
            history.clean_equity != book.account.equity
            if not book.positions.positions
            else not (book.account.equity.is_finite() and book.account.equity > 0)
        ):
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH")

        account = AccountBudgetAuthority(
            account_role=self._account_role,
            account_fingerprint=expected_account_fingerprint,
            snapshot_book_source_hash=book.source_hash,
            observed_at=snapshot.trusted_at,
            baseline_clean=not seal.positions_manifest and not seal.orders_manifest,
            baseline_source_hash=baseline.baseline_hash,
            book_fingerprint=book.source_hash,
            book_source_hash=book.source_hash,
            # With positions open the budget is sized from the equity observed in this
            # snapshot, so the selector, assembler, and policy all see one equity figure.
            clean_equity=(
                book.account.equity if book.positions.positions else history.clean_equity
            ),
            open_position_count=len(book.positions.positions),
            open_order_count=len(book.open_orders),
            filled_entry_count=history.entries_used,
            lifetime_approved_risk=history.gross_approved_risk,
            entry_reservation_active=history.reserved_intent_id is not None,
            reserved_approved_risk=history.reserved_risk,
            event_already_attempted=history.event_already_attempted,
            history_source_hash=history.authority_hash,
        )
        return OpportunityHistoryEvidence(account=account, budget_hash=history.authority_hash)


class SQLAlchemyOpportunityPriorDecisionAdapter:
    def __init__(
        self,
        repository: SQLAlchemyOpportunityAuthorityRepository,
        *,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
    ) -> None:
        if account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise ValueError("OPPORTUNITY_ACCOUNT_ROLE_INVALID")
        self._repository = repository
        self._account_role = account_role

    def load(
        self,
        *,
        expected_account_fingerprint: str,
        opportunity_key: str,
        decision_boundary: datetime,
        as_of: datetime,
    ) -> PriorDecisionAuthority:
        authority = self._repository.load_prior_opportunity_decision(
            expected_account_fingerprint=expected_account_fingerprint,
            expected_opportunity_key=opportunity_key,
            decision_boundary=decision_boundary,
            as_of=as_of,
        )
        if authority.account_fingerprint != expected_account_fingerprint:
            raise OpportunityRuntimePersistenceError("OPPORTUNITY_ACCOUNT_AUTHORITY_MISMATCH")
        return PriorDecisionAuthority(
            opportunity_key=authority.opportunity_key,
            decision_boundary=authority.decision_boundary,
            outcome=authority.outcome,
            observed_at=authority.observed_at,
            source_hash=authority.source_hash,
        )


class SQLAlchemyOpportunityGreekAuthorityAdapter:
    def __init__(self, repository: SQLAlchemyOpportunityAuthorityRepository) -> None:
        self._repository = repository

    def load(self, *, effective_at: datetime) -> GreekUnitAuthority:
        return self._repository.load_latest_greek_unit_authority(effective_at=effective_at)


class SQLAlchemyOpportunityObservationAdapter:
    def __init__(self, repository: SQLAlchemyOpportunityEvidenceRepository) -> None:
        self._repository = repository

    def append(self, spec: OpportunityObservationSpec) -> PersistedOpportunityObservation:
        return self._repository.append_observation(spec)


class SQLAlchemyOpportunityThesisAdapter:
    def __init__(self, repository: SQLAlchemyOpportunityThesisRepository) -> None:
        self._repository = repository

    def persist(self, draft: FrozenThesisVersion) -> FrozenThesisVersion:
        return self._repository.persist(draft)


def _utc(value: datetime, code: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise OpportunityRuntimePersistenceError(code)
    return value.astimezone(UTC)
