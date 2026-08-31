from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.market_data import NormalizedLifecycleMarketEvidence
from backend.app.contracts.v1 import AccountRole, ThesisResponse
from backend.app.execution import SweepObservation
from backend.app.persistence.sqlalchemy_models import (
    AgentInputSnapshotRow,
    AlpacaMarketSessionRow,
    GreekAuthorityVersionRow,
    LifecycleAccountObservationRow,
    LifecycleLaunchAuthorityRow,
    LifecycleObservationBindingRow,
    LifecycleObservationManifestRow,
    LifecycleSourceObservationRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
    ThesisVersionRow,
)
from backend.app.policy import VolatilityView
from backend.app.services.acquisition import (
    AlpacaMarketSession,
    GreekAuthorityEvidence,
    LifecycleProviderObservation,
    ObservedPaperAccountAuthority,
    RetainedLifecycleContext,
    RetainedLifecycleTransition,
    RetainedOptionPosition,
)

from .contracts import LifecycleLaunchAuthority
from .research import LifecycleResearchSource


class LifecyclePersistenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SQLAlchemyLifecycleRepository:
    def __init__(self, sessions: sessionmaker) -> None:
        self._sessions = sessions

    def load(self, authority: ObservedPaperAccountAuthority) -> RetainedLifecycleContext:
        _require_persisted_role(authority.role)
        with self._sessions.begin() as session:
            positions = session.scalars(
                select(ManagedLifecyclePositionRow).where(
                    ManagedLifecyclePositionRow.account_role == authority.role.value,
                    ManagedLifecyclePositionRow.closed_at.is_(None),
                )
            ).all()
            if len(positions) != 1:
                raise LifecyclePersistenceError("ACTIVE_POSITION_NOT_UNIQUE")
            position = positions[0]
            if position.account_fingerprint != authority.account_fingerprint:
                raise LifecyclePersistenceError("ACCOUNT_AUTHORITY_MISMATCH")
            snapshot = session.get(ManagedPositionSnapshotRow, position.current_snapshot_id)
            thesis = session.get(ThesisVersionRow, position.thesis_version_id)
            launch = session.get(LifecycleLaunchAuthorityRow, position.managed_position_id)
            greek = session.scalar(
                select(GreekAuthorityVersionRow)
                .where(GreekAuthorityVersionRow.effective_at <= position.activated_at)
                .order_by(GreekAuthorityVersionRow.version.desc())
                .limit(1)
            )
            transitions = session.scalars(
                select(ManagedPositionTransitionRow)
                .where(
                    ManagedPositionTransitionRow.managed_position_id == position.managed_position_id
                )
                .order_by(ManagedPositionTransitionRow.transition_sequence)
            ).all()
            if None in (snapshot, thesis, launch, greek) or not transitions:
                raise LifecyclePersistenceError("CONTEXT_LINEAGE_INCOMPLETE")
            assert snapshot is not None and thesis is not None and launch is not None
            assert greek is not None
            inventory = tuple(
                RetainedOptionPosition(
                    symbol=str(item["symbol"]),
                    signed_quantity=Decimal(str(item["signed_quantity"])),
                    multiplier=int(item["multiplier"]),
                )
                for item in snapshot.normalized_inventory
            )
            if len(inventory) != 2:
                raise LifecyclePersistenceError("CONTEXT_INVENTORY_INVALID")
            limits = thesis.exposure_limits
            try:
                response = ThesisResponse.model_validate(thesis.thesis_payload)
                return RetainedLifecycleContext(
                    thesis_version_id=thesis.thesis_version_id,
                    account_role=authority.role,
                    account_fingerprint=position.account_fingerprint,
                    policy_hash=thesis.policy_hash,
                    thesis=response,
                    thesis_frozen_at=_utc(thesis.frozen_at),
                    lifecycle_origin_at=_utc(transitions[0].occurred_at),
                    lifecycle_transitions=tuple(
                        RetainedLifecycleTransition(
                            action=item.action,
                            occurred_at=_utc(item.occurred_at),
                            market_session_id=item.market_session_id,
                            cashflow=Decimal(item.cashflow_contribution),
                            activity_hashes=tuple(
                                sorted(
                                    str(value["activity_id_hash"])
                                    for value in item.fill_activity_manifest
                                )
                            ),
                        )
                        for item in transitions
                    ),
                    target_at=_utc(thesis.target_at),
                    position_fingerprint=position.active_position_fingerprint,
                    expected_positions=(inventory[0], inventory[1]),
                    delta_low=Decimal(str(limits["delta_low"])),
                    delta_high=Decimal(str(limits["delta_high"])),
                    vega_low=Decimal(str(limits["vega_low"])),
                    vega_high=Decimal(str(limits["vega_high"])),
                    maximum_daily_theta=Decimal(str(limits["maximum_daily_theta"])),
                    minimum_dte=int(limits["minimum_dte"]),
                    maximum_dte=int(limits["maximum_dte"]),
                    maximum_relative_spread=Decimal(str(limits["maximum_relative_spread"])),
                    liquidity_authority_hash=str(limits["liquidity_authority_hash"]),
                    volatility_view=VolatilityView(thesis.volatility_view),
                    entry_atm_iv=Decimal(thesis.entry_atm_iv),
                    approved_max_loss=Decimal(thesis.approved_max_loss),
                    portfolio_risk_cap=Decimal(thesis.portfolio_risk_cap),
                    greek_authority=GreekAuthorityEvidence(
                        greek.authority_id,
                        greek.version,
                        _utc(greek.effective_at),
                        greek.timestamp_contract_hash,
                        greek.units_contract_hash,
                    ),
                    managed_position_id=position.managed_position_id,
                    current_snapshot_id=snapshot.snapshot_id,
                    launch_authority=LifecycleLaunchAuthority(
                        Decimal(launch.beta60),
                        launch.benchmark_symbol,
                        _utc(launch.entry_boundary_at),
                        launch.entry_policy_hash,
                        launch.underlying_source_hash,
                        launch.benchmark_source_hash,
                        launch.completed_bar_source_hash,
                    ),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise LifecyclePersistenceError("CONTEXT_PAYLOAD_INVALID") from error

    def persist(
        self,
        *,
        context: RetainedLifecycleContext,
        observation: LifecycleProviderObservation,
        clusters: tuple[object, ...],
        classifications: tuple[object, ...],
        manifest_id: UUID,
        manifest_hash: str,
        trusted_at: datetime,
    ) -> None:
        _require_persisted_role(context.account_role)
        with self._sessions.begin() as session:
            position = session.get(ManagedLifecyclePositionRow, context.managed_position_id)
            if (
                position is None
                or position.closed_at is not None
                or position.account_role != context.account_role.value
                or position.account_fingerprint != context.account_fingerprint
                or position.thesis_version_id != context.thesis_version_id
                or position.current_snapshot_id != context.current_snapshot_id
                or position.active_position_fingerprint != context.position_fingerprint
            ):
                raise LifecyclePersistenceError("CONTEXT_AUTHORITY_MISMATCH")
            existing = session.get(LifecycleObservationManifestRow, manifest_id)
            if existing is not None:
                if existing.manifest_hash != manifest_hash:
                    raise LifecyclePersistenceError("MANIFEST_ID_CONFLICT")
                return
            source_ids = {
                source_id
                for cluster in clusters
                for source_id in getattr(cluster, "source_ids", ())
            }
            research_sources = session.scalars(
                select(LifecycleSourceObservationRow).where(
                    LifecycleSourceObservationRow.external_source_id.in_(source_ids)
                )
            ).all()
            if {item.external_source_id for item in research_sources} != source_ids or any(
                item.source_kind not in {"MCP_NEWS", "MCP_CORPORATE_ACTION"}
                for item in research_sources
            ):
                raise LifecyclePersistenceError("RESEARCH_SOURCE_AUTHORITY_MISSING")
            account_id, account_payload, sweep_hash = self._persist_account_observation(
                session,
                context,
                observation.sweep,
                trusted_at,
            )
            self._persist_market_session(session, observation.boundaries.market_session, trusted_at)
            provider_sources = tuple(_sources(observation))
            for (
                kind,
                source_hash,
                request_hash,
                observed_at,
                retrieved_at,
                payload,
            ) in provider_sources:
                prior = session.scalar(
                    select(LifecycleSourceObservationRow).where(
                        LifecycleSourceObservationRow.source_hash == source_hash
                    )
                )
                if prior is not None:
                    expected = (
                        uuid5(NAMESPACE_URL, f"alphadecay:lifecycle-source:{source_hash}"),
                        None,
                        kind,
                        None,
                        None,
                        request_hash,
                        _digest(payload),
                        payload,
                        _utc(observed_at),
                        _utc(retrieved_at),
                        source_hash,
                    )
                    durable = (
                        prior.source_id,
                        prior.external_source_id,
                        prior.source_kind,
                        prior.account_role,
                        prior.account_fingerprint,
                        prior.request_hash,
                        prior.result_hash,
                        prior.normalized_payload,
                        _utc(prior.observed_at),
                        _utc(prior.retrieved_at),
                        prior.source_hash,
                    )
                    if durable != expected:
                        raise LifecyclePersistenceError("SOURCE_HASH_CONFLICT")
                    continue
                session.add(
                    LifecycleSourceObservationRow(
                        source_id=uuid5(
                            NAMESPACE_URL, f"alphadecay:lifecycle-source:{source_hash}"
                        ),
                        external_source_id=None,
                        source_kind=kind,
                        account_role=None,
                        account_fingerprint=None,
                        request_hash=request_hash,
                        result_hash=_digest(payload),
                        normalized_payload=payload,
                        observed_at=_utc(observed_at),
                        retrieved_at=_utc(retrieved_at),
                        source_hash=source_hash,
                        created_at=_utc(trusted_at),
                    )
                )
            session.flush()
            all_source_hashes = {item.source_hash for item in research_sources} | {
                item[1] for item in provider_sources
            }
            durable_sources = session.scalars(
                select(LifecycleSourceObservationRow)
                .where(LifecycleSourceObservationRow.source_hash.in_(all_source_hashes))
                .order_by(LifecycleSourceObservationRow.source_hash)
            ).all()
            if {item.source_hash for item in durable_sources} != all_source_hashes:
                raise LifecyclePersistenceError("SOURCE_AUTHORITY_INCOMPLETE")
            session.add(
                LifecycleObservationManifestRow(
                    manifest_id=manifest_id,
                    manifest_hash=manifest_hash,
                    agent_input_snapshot_id=None,
                    account_observation_id=account_id,
                    managed_position_id=context.managed_position_id,
                    managed_snapshot_id=context.current_snapshot_id,
                    reconciliation_id=None,
                    greek_authority_id=context.greek_authority.authority_id,
                    sweep_hash=sweep_hash,
                    account_manifest=account_payload,
                    activity_manifest=_json(observation.sweep.activities),
                    option_manifest=_json(observation.options),
                    atm_iv_manifest=_json(observation.atm_iv),
                    underlying_manifest=_json(observation.underlying),
                    boundary_manifest=_json(observation.boundaries),
                    research_manifest=_json(
                        [
                            {
                                "clusters": clusters,
                                "classifications": classifications,
                                "sources": tuple(
                                    {
                                        "logical_source_id": item.external_source_id,
                                        "source_hash": item.source_hash,
                                        "request_hash": item.request_hash,
                                        "result_hash": item.result_hash,
                                    }
                                    for item in sorted(
                                        research_sources,
                                        key=lambda value: value.external_source_id or "",
                                    )
                                ),
                            }
                        ]
                    ),
                    source_authority_manifest=[_source_authority(item) for item in durable_sources],
                    observed_at=_utc(observation.boundaries.observed_at),
                    created_at=_utc(trusted_at),
                )
            )

    def persist_account_observation(
        self,
        *,
        context: RetainedLifecycleContext,
        sweep: SweepObservation,
        trusted_at: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            self._require_active_context(session, context)
            self._persist_account_observation(session, context, sweep, trusted_at)

    def persist_market_session(
        self,
        *,
        context: RetainedLifecycleContext,
        evidence: NormalizedLifecycleMarketEvidence,
        trusted_at: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            self._require_active_context(session, context)
            self._persist_market_session(
                session,
                evidence.boundaries.market_session,
                trusted_at,
            )

    def persist_research_sources(
        self,
        context: RetainedLifecycleContext,
        records: tuple[LifecycleResearchSource, ...],
        trusted_at: datetime,
    ) -> None:
        _require_persisted_role(context.account_role)
        with self._sessions.begin() as session:
            position = session.get(ManagedLifecyclePositionRow, context.managed_position_id)
            if (
                position is None
                or position.closed_at is not None
                or position.account_role != context.account_role.value
                or position.current_snapshot_id != context.current_snapshot_id
                or position.account_fingerprint != context.account_fingerprint
                or position.thesis_version_id != context.thesis_version_id
            ):
                raise LifecyclePersistenceError("CONTEXT_AUTHORITY_MISMATCH")
            for record in records:
                if (
                    record.source_kind not in {"MCP_NEWS", "MCP_CORPORATE_ACTION"}
                    or not record.logical_source_id
                    or len(record.logical_source_id) > 128
                    or record.result_hash != _digest(record.normalized_payload)
                    or _utc(record.observed_at) > _utc(record.retrieved_at)
                    or _utc(record.retrieved_at) > _utc(trusted_at)
                    or any(
                        len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                        for value in (
                            record.request_hash,
                            record.result_hash,
                            record.source_hash,
                        )
                    )
                ):
                    raise LifecyclePersistenceError("RESEARCH_SOURCE_INVALID")
                source_id = uuid5(
                    NAMESPACE_URL, f"alphadecay:lifecycle-source:{record.source_hash}"
                )
                prior = session.scalar(
                    select(LifecycleSourceObservationRow).where(
                        (LifecycleSourceObservationRow.source_hash == record.source_hash)
                        | (
                            LifecycleSourceObservationRow.external_source_id
                            == record.logical_source_id
                        )
                    )
                )
                values = (
                    source_id,
                    record.logical_source_id,
                    record.source_kind,
                    None,
                    None,
                    record.request_hash,
                    record.result_hash,
                    record.normalized_payload,
                    _utc(record.observed_at),
                    _utc(record.retrieved_at),
                    record.source_hash,
                )
                if prior is not None:
                    durable = (
                        prior.source_id,
                        prior.external_source_id,
                        prior.source_kind,
                        prior.account_role,
                        prior.account_fingerprint,
                        prior.request_hash,
                        prior.result_hash,
                        prior.normalized_payload,
                        _utc(prior.observed_at),
                        _utc(prior.retrieved_at),
                        prior.source_hash,
                    )
                    if durable != values:
                        raise LifecyclePersistenceError("SOURCE_HASH_CONFLICT")
                    continue
                session.add(
                    LifecycleSourceObservationRow(
                        source_id=source_id,
                        external_source_id=record.logical_source_id,
                        source_kind=record.source_kind,
                        account_role=None,
                        account_fingerprint=None,
                        request_hash=record.request_hash,
                        result_hash=record.result_hash,
                        normalized_payload=record.normalized_payload,
                        observed_at=_utc(record.observed_at),
                        retrieved_at=_utc(record.retrieved_at),
                        source_hash=record.source_hash,
                        created_at=_utc(trusted_at),
                    )
                )

    def bind_input(self, manifest_id: UUID, input_snapshot_id: UUID, trusted_at: datetime) -> None:
        with self._sessions.begin() as session:
            manifest = session.get(LifecycleObservationManifestRow, manifest_id)
            input_snapshot = session.get(AgentInputSnapshotRow, input_snapshot_id)
            position = (
                session.get(ManagedLifecyclePositionRow, manifest.managed_position_id)
                if manifest is not None
                else None
            )
            already_bound = session.scalar(
                select(LifecycleObservationBindingRow).where(
                    (LifecycleObservationBindingRow.manifest_id == manifest_id)
                    | (LifecycleObservationBindingRow.agent_input_snapshot_id == input_snapshot_id)
                )
            )
            if (
                manifest is None
                or input_snapshot is None
                or position is None
                or already_bound is not None
                or manifest.agent_input_snapshot_id is not None
                or manifest.reconciliation_id is not None
                or input_snapshot.decision_kind != "ASSESSMENT"
                or input_snapshot.account_role != position.account_role
                or input_snapshot.account_fingerprint != position.account_fingerprint
                or input_snapshot.thesis_version_id != position.thesis_version_id
                or input_snapshot.normalized_payload.get("acquisition_manifest_id")
                != str(manifest_id)
                or input_snapshot.normalized_payload.get("acquisition_manifest_hash")
                != manifest.manifest_hash
            ):
                raise LifecyclePersistenceError("MANIFEST_BINDING_INVALID")
            session.add(
                LifecycleObservationBindingRow(
                    binding_id=uuid5(
                        NAMESPACE_URL,
                        f"alphadecay:lifecycle-binding:{manifest_id}:{input_snapshot_id}",
                    ),
                    manifest_id=manifest_id,
                    agent_input_snapshot_id=input_snapshot_id,
                    created_at=_utc(trusted_at),
                )
            )

    @staticmethod
    def _require_active_context(session, context: RetainedLifecycleContext) -> None:
        _require_persisted_role(context.account_role)
        position = session.get(ManagedLifecyclePositionRow, context.managed_position_id)
        if (
            position is None
            or position.closed_at is not None
            or position.account_role != context.account_role.value
            or position.account_fingerprint != context.account_fingerprint
            or position.thesis_version_id != context.thesis_version_id
            or position.current_snapshot_id != context.current_snapshot_id
            or position.active_position_fingerprint != context.position_fingerprint
        ):
            raise LifecyclePersistenceError("CONTEXT_AUTHORITY_MISMATCH")

    @staticmethod
    def _persist_account_observation(
        session,
        context: RetainedLifecycleContext,
        sweep: SweepObservation,
        trusted_at: datetime,
    ) -> tuple[UUID, dict[str, object], str]:
        account_payload = _json(sweep)
        if not isinstance(account_payload, dict):
            raise LifecyclePersistenceError("ACCOUNT_OBSERVATION_INVALID")
        sweep_hash = _digest(account_payload)
        account_id = uuid5(NAMESPACE_URL, f"alphadecay:lifecycle-account:{sweep_hash}")
        values = (
            context.managed_position_id,
            context.current_snapshot_id,
            context.account_role.value,
            context.account_fingerprint,
            account_payload,
            sweep_hash,
            _utc(sweep.retrieval_started_at),
            _utc(sweep.retrieval_completed_at),
            _utc(trusted_at),
        )
        prior = session.get(LifecycleAccountObservationRow, account_id)
        if prior is not None:
            durable = (
                prior.managed_position_id,
                prior.managed_snapshot_id,
                prior.account_role,
                prior.account_fingerprint,
                prior.sweep_payload,
                prior.sweep_hash,
                _utc(prior.retrieval_started_at),
                _utc(prior.retrieval_completed_at),
                _utc(prior.accepted_at),
            )
            if durable != values:
                raise LifecyclePersistenceError("ACCOUNT_OBSERVATION_CONFLICT")
            return account_id, account_payload, sweep_hash
        session.add(
            LifecycleAccountObservationRow(
                observation_id=account_id,
                managed_position_id=values[0],
                managed_snapshot_id=values[1],
                account_role=values[2],
                account_fingerprint=values[3],
                sweep_payload=values[4],
                sweep_hash=values[5],
                retrieval_started_at=values[6],
                retrieval_completed_at=values[7],
                accepted_at=values[8],
            )
        )
        return account_id, account_payload, sweep_hash

    @staticmethod
    def _persist_market_session(session, value: AlpacaMarketSession, trusted_at: datetime) -> None:
        prior = session.get(AlpacaMarketSessionRow, value.market_session_id)
        payload = _json(value)
        if prior is not None:
            if prior.session_date != value.session_date or prior.source_hash != value.source_hash:
                raise LifecyclePersistenceError("MARKET_SESSION_CONFLICT")
            return
        session.add(
            AlpacaMarketSessionRow(
                market_session_id=value.market_session_id,
                session_date=value.session_date,
                open_at=_utc(value.open_at),
                close_at=_utc(value.close_at),
                source_hash=value.source_hash,
                request_hash=value.request_hash,
                retrieved_at=_utc(value.retrieved_at),
                source_payload=payload,
                session_hash=_digest(payload),
                created_at=_utc(trusted_at),
            )
        )


def _sources(observation: LifecycleProviderObservation):
    atm = observation.atm_iv
    underlying = observation.underlying
    market_session = observation.boundaries.market_session
    yield (
        "ATM_IV",
        atm.source_hash,
        atm.request_hash,
        atm.observed_at,
        atm.retrieved_at,
        _json(atm),
    )
    yield (
        "UNDERLYING_QUOTE",
        underlying.quote_source_hash,
        underlying.request_hash,
        underlying.quote_observed_at,
        underlying.quote_retrieved_at,
        _json(underlying),
    )
    yield (
        "MARKET_CALENDAR",
        market_session.source_hash,
        market_session.request_hash,
        market_session.open_at,
        market_session.retrieved_at,
        _json(market_session),
    )
    for component, source_hash in (
        ("call", atm.call_source_hash),
        ("put", atm.put_source_hash),
    ):
        yield (
            "OPTION_SNAPSHOT",
            source_hash,
            atm.request_hash,
            atm.observed_at,
            atm.retrieved_at,
            {"component": component, "source_hash": source_hash},
        )
    for option in observation.options:
        yield (
            "OPTION_SNAPSHOT",
            option.source_hash,
            option.source_hash,
            option.quote_observed_at,
            option.retrieved_at,
            _json(option),
        )
    bars: dict[str, tuple[str, datetime]] = {
        underlying.completed_bar_source_hash: (
            underlying.underlying,
            underlying.completed_bar_at,
        ),
        underlying.benchmark_completed_bar_source_hash: (
            underlying.benchmark_symbol,
            underlying.benchmark_completed_bar_at,
        ),
    }
    for point in observation.boundaries.price_confirmation:
        bars[point.underlying_bar_source_hash] = (
            underlying.underlying,
            point.completed_bar_at,
        )
        bars[point.benchmark_bar_source_hash] = (
            underlying.benchmark_symbol,
            point.completed_bar_at,
        )
    for source_hash, (symbol, completed_at) in bars.items():
        yield (
            "COMPLETED_BAR",
            source_hash,
            underlying.request_hash,
            completed_at,
            observation.boundaries.observed_at,
            {
                "symbol": symbol,
                "completed_bar_at": completed_at.isoformat(),
                "source_hash": source_hash,
            },
        )


def _json(value: object):
    if hasattr(value, "model_dump"):
        return _json(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID | Enum):
        return str(value)
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_authority(value: LifecycleSourceObservationRow) -> dict[str, object]:
    return {
        "source_id": str(value.source_id),
        "external_source_id": value.external_source_id,
        "source_kind": value.source_kind,
        "account_role": value.account_role,
        "account_fingerprint": value.account_fingerprint,
        "request_hash": value.request_hash,
        "result_hash": value.result_hash,
        "normalized_payload": value.normalized_payload,
        "observed_at": _utc(value.observed_at).isoformat(),
        "retrieved_at": _utc(value.retrieved_at).isoformat(),
        "source_hash": value.source_hash,
        "created_at": _utc(value.created_at).isoformat(),
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_persisted_role(role: AccountRole) -> None:
    if role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}:
        raise LifecyclePersistenceError("EXECUTABLE_ROLE_REQUIRED")
