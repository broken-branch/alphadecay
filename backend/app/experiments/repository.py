from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.persistence.sqlalchemy_models import (
    CompiledExperimentVersionRow,
    ExperimentArmEventRow,
    ExperimentArmStateRow,
    ReviewedExperimentDefinitionRow,
)
from backend.app.strategy_briefs.models import StrategyCurationResponse
from backend.app.strategy_briefs.protocol import (
    ProtocolCompilationBlocked,
    ReviewedExecutableProtocolRequest,
    compile_reviewed_protocol,
    verify_compiled_protocol,
)

from .models import (
    CompiledExperimentVersion,
    CompileExperimentRequest,
    ExperimentAuthorizationRequest,
    ExperimentAuthorizationStatus,
    ReviewedExperimentCreateRequest,
    ReviewedExperimentDefinition,
)


class ExperimentRegistryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SQLAlchemyExperimentRegistry:
    def __init__(self, sessions: sessionmaker) -> None:
        self._sessions = sessions

    def create(
        self,
        request: ReviewedExperimentCreateRequest,
        *,
        created_at: datetime,
    ) -> ReviewedExperimentDefinition:
        experiment_id = uuid4()
        created_at = _utc(created_at)
        material = _hash_material(experiment_id, request)
        payload_text = json.dumps(material, sort_keys=True, separators=(",", ":"))
        definition_hash = hashlib.sha256(payload_text.encode()).hexdigest()
        definition = ReviewedExperimentDefinition(
            experiment_id=experiment_id,
            definition_hash=definition_hash,
            original_thesis=request.original_thesis,
            reviewed_protocol=request.reviewed_protocol,
            curation=StrategyCurationResponse.model_validate(
                request.curation.model_dump(mode="python")
            ),
            created_at=created_at,
        )
        row = ReviewedExperimentDefinitionRow(
            experiment_id=experiment_id,
            version=1,
            definition_hash=definition_hash,
            lifecycle_state="REVIEWED",
            payload_text=definition.model_dump_json(),
            created_at=created_at,
        )
        try:
            with self._sessions.begin() as session:
                session.add(row)
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_WRITE_FAILED") from error
        return definition

    def read(self, experiment_id: UUID) -> ReviewedExperimentDefinition | None:
        try:
            with self._sessions() as session:
                row = session.get(ReviewedExperimentDefinitionRow, experiment_id)
                return None if row is None else self._decode(row)
        except ExperimentRegistryError:
            raise
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_READ_FAILED") from error

    def list(self) -> tuple[ReviewedExperimentDefinition, ...]:
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(ReviewedExperimentDefinitionRow).order_by(
                        ReviewedExperimentDefinitionRow.created_at.desc(),
                        ReviewedExperimentDefinitionRow.experiment_id.desc(),
                    )
                )
                return tuple(self._decode(row) for row in rows)
        except ExperimentRegistryError:
            raise
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_READ_FAILED") from error

    def compile(
        self,
        experiment_id: UUID,
        request: CompileExperimentRequest,
        *,
        created_at: datetime,
    ) -> CompiledExperimentVersion:
        created_at = _utc(created_at)
        candidate: CompiledExperimentVersion | None = None
        try:
            with self._sessions.begin() as session:
                source_row = session.get(ReviewedExperimentDefinitionRow, experiment_id)
                if source_row is None:
                    raise ExperimentRegistryError("EXPERIMENT_NOT_FOUND")
                source = self._decode(source_row)
                if request.source_definition_hash != source.definition_hash:
                    raise ExperimentRegistryError("EXPERIMENT_SOURCE_HASH_MISMATCH")
                if (
                    source.curation.intake != source.original_thesis
                    or source.curation.protocol_fields != source.reviewed_protocol
                ):
                    raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
                try:
                    compiled = compile_reviewed_protocol(
                        ReviewedExecutableProtocolRequest(
                            curation=source.curation,
                            definition=request.definition,
                            rules=request.rules,
                        )
                    )
                except (ProtocolCompilationBlocked, ValueError) as error:
                    raise ExperimentRegistryError("EXPERIMENT_COMPILE_INPUT_REJECTED") from error
                candidate = CompiledExperimentVersion(
                    experiment_id=experiment_id,
                    source_definition_hash=source.definition_hash,
                    protocol_hash=compiled.protocol_hash,
                    compiled_protocol=compiled,
                    created_at=created_at,
                )
                existing_row = session.get(CompiledExperimentVersionRow, experiment_id)
                if existing_row is not None:
                    existing = self._decode_compiled(existing_row)
                    if existing.compiled_protocol == compiled:
                        return existing
                    raise ExperimentRegistryError("EXPERIMENT_COMPILE_CONFLICT")
                session.add(
                    CompiledExperimentVersionRow(
                        experiment_id=experiment_id,
                        source_version=1,
                        compiled_version=1,
                        source_definition_hash=source.definition_hash,
                        protocol_hash=compiled.protocol_hash,
                        lifecycle_state="COMPILED",
                        arm_state="NOT_ARMED",
                        automation_state="OFF",
                        execution_eligible=False,
                        payload_text=candidate.model_dump_json(),
                        created_at=created_at,
                    )
                )
                return candidate
        except ExperimentRegistryError:
            raise
        except IntegrityError as error:
            existing = self.read_compiled(experiment_id)
            if (
                candidate is not None
                and existing is not None
                and existing.source_definition_hash == candidate.source_definition_hash
                and existing.compiled_protocol == candidate.compiled_protocol
            ):
                return existing
            if existing is not None:
                raise ExperimentRegistryError("EXPERIMENT_COMPILE_CONFLICT") from error
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_WRITE_FAILED") from error
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_WRITE_FAILED") from error

    def read_compiled(self, experiment_id: UUID) -> CompiledExperimentVersion | None:
        try:
            with self._sessions() as session:
                row = session.get(CompiledExperimentVersionRow, experiment_id)
                return None if row is None else self._decode_compiled(row)
        except ExperimentRegistryError:
            raise
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_READ_FAILED") from error

    def read_authorization(
        self,
        experiment_id: UUID,
    ) -> ExperimentAuthorizationStatus:
        try:
            with self._sessions() as session:
                source, compiled = self._load_verified_compiled(session, experiment_id)
                row = session.get(ExperimentArmStateRow, experiment_id)
                if row is None:
                    if (
                        session.scalar(
                            select(ExperimentArmEventRow.event_id).where(
                                ExperimentArmEventRow.experiment_id == experiment_id
                            )
                        )
                        is not None
                    ):
                        raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
                    return ExperimentAuthorizationStatus(
                        experiment_id=experiment_id,
                        source_definition_hash=source.definition_hash,
                        protocol_hash=compiled.protocol_hash,
                        authorization_revision=0,
                        authorization_state="NOT_ARMED",
                        entry_authorized=False,
                    )
                if (
                    row.source_definition_hash != source.definition_hash
                    or row.protocol_hash != compiled.protocol_hash
                ):
                    raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
                return self._decode_authorization(session, row)
        except ExperimentRegistryError:
            raise
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_READ_FAILED") from error

    def arm(
        self,
        experiment_id: UUID,
        request: ExperimentAuthorizationRequest,
        *,
        changed_at: datetime,
    ) -> ExperimentAuthorizationStatus:
        return self._change_authorization(
            experiment_id,
            request,
            action="ARM",
            changed_at=changed_at,
        )

    def disarm(
        self,
        experiment_id: UUID,
        request: ExperimentAuthorizationRequest,
        *,
        changed_at: datetime,
    ) -> ExperimentAuthorizationStatus:
        return self._change_authorization(
            experiment_id,
            request,
            action="DISARM",
            changed_at=changed_at,
        )

    def _change_authorization(
        self,
        experiment_id: UUID,
        request: ExperimentAuthorizationRequest,
        *,
        action: str,
        changed_at: datetime,
    ) -> ExperimentAuthorizationStatus:
        changed_at = _utc(changed_at)
        try:
            with self._sessions.begin() as session:
                source, compiled = self._load_verified_compiled(session, experiment_id)
                if (
                    request.source_definition_hash != source.definition_hash
                    or request.protocol_hash != compiled.protocol_hash
                ):
                    raise ExperimentRegistryError("EXPERIMENT_AUTHORIZATION_HASH_MISMATCH")
                row = session.scalar(
                    select(ExperimentArmStateRow)
                    .where(ExperimentArmStateRow.experiment_id == experiment_id)
                    .with_for_update()
                )
                if row is not None and (
                    row.source_definition_hash != source.definition_hash
                    or row.protocol_hash != compiled.protocol_hash
                ):
                    raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
                target_state = "ARMED" if action == "ARM" else "DISARMED"
                if row is not None and row.authorization_state == target_state:
                    if request.expected_revision not in {
                        row.authorization_revision,
                        row.authorization_revision - 1,
                    }:
                        raise ExperimentRegistryError("EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT")
                    return self._decode_authorization(session, row)
                current_revision = 0 if row is None else row.authorization_revision
                if request.expected_revision != current_revision:
                    raise ExperimentRegistryError("EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT")
                if action == "DISARM" and row is None:
                    raise ExperimentRegistryError("EXPERIMENT_NOT_ARMED")
                if row is not None and changed_at < _stored_utc(row.updated_at):
                    raise ExperimentRegistryError("EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT")
                if action == "ARM":
                    other = session.scalar(
                        select(ExperimentArmStateRow)
                        .where(
                            ExperimentArmStateRow.authorization_state == "ARMED",
                            ExperimentArmStateRow.experiment_id != experiment_id,
                        )
                        .with_for_update()
                    )
                    if other is not None:
                        raise ExperimentRegistryError("EXPERIMENT_ARM_CONFLICT")
                revision = current_revision + 1
                event_id = uuid4()
                event_hash = _authorization_event_hash(
                    event_id=event_id,
                    experiment_id=experiment_id,
                    source_definition_hash=source.definition_hash,
                    protocol_hash=compiled.protocol_hash,
                    authorization_revision=revision,
                    action=action,
                    authorization_state=target_state,
                    created_at=changed_at,
                )
                entry_authorized = target_state == "ARMED"
                event = ExperimentArmEventRow(
                    event_id=event_id,
                    experiment_id=experiment_id,
                    source_definition_hash=source.definition_hash,
                    protocol_hash=compiled.protocol_hash,
                    authorization_revision=revision,
                    action=action,
                    authorization_state=target_state,
                    entry_authorized=entry_authorized,
                    existing_position_risk_management_preserved=True,
                    runtime_state="NOT_CONNECTED",
                    execution_eligible=False,
                    paper_trading_only=True,
                    event_hash=event_hash,
                    created_at=changed_at,
                )
                session.add(event)
                if row is None:
                    row = ExperimentArmStateRow(
                        experiment_id=experiment_id,
                        source_definition_hash=source.definition_hash,
                        protocol_hash=compiled.protocol_hash,
                        authorization_revision=revision,
                        authorization_state=target_state,
                        entry_authorized=entry_authorized,
                        existing_position_risk_management_preserved=True,
                        runtime_state="NOT_CONNECTED",
                        execution_eligible=False,
                        paper_trading_only=True,
                        last_event_hash=event_hash,
                        updated_at=changed_at,
                    )
                    session.add(row)
                else:
                    row.authorization_revision = revision
                    row.authorization_state = target_state
                    row.entry_authorized = entry_authorized
                    row.last_event_hash = event_hash
                    row.updated_at = changed_at
                session.flush()
                return self._decode_authorization(session, row)
        except ExperimentRegistryError:
            raise
        except IntegrityError as error:
            try:
                committed = self.read_authorization(experiment_id)
            except ExperimentRegistryError:
                committed = None
            target_state = "ARMED" if action == "ARM" else "DISARMED"
            if (
                committed is not None
                and committed.authorization_state == target_state
                and committed.source_definition_hash == request.source_definition_hash
                and committed.protocol_hash == request.protocol_hash
                and request.expected_revision
                in {
                    committed.authorization_revision,
                    committed.authorization_revision - 1,
                }
            ):
                return committed
            code = (
                "EXPERIMENT_ARM_CONFLICT"
                if action == "ARM"
                else ("EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT")
            )
            raise ExperimentRegistryError(code) from error
        except SQLAlchemyError as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_WRITE_FAILED") from error

    def _load_verified_compiled(self, session, experiment_id: UUID):
        source_row = session.get(ReviewedExperimentDefinitionRow, experiment_id)
        if source_row is None:
            raise ExperimentRegistryError("EXPERIMENT_NOT_FOUND")
        compiled_row = session.get(CompiledExperimentVersionRow, experiment_id)
        if compiled_row is None:
            raise ExperimentRegistryError("EXPERIMENT_NOT_COMPILED")
        source = self._decode(source_row)
        compiled = self._decode_compiled(compiled_row)
        protocol = compiled.compiled_protocol
        if (
            compiled.source_definition_hash != source.definition_hash
            or compiled.experiment_id != source.experiment_id
            or not compiled.paper_trading_only
            or not protocol.paper_trading_only
            or not protocol.options_required
            or not protocol.defined_risk_required
            or protocol.recipe != "TWO_LEG_DEBIT_VERTICAL"
            or protocol.leg_count != 2
            or protocol.net_premium != "DEBIT"
        ):
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
        return source, compiled

    @staticmethod
    def _decode_authorization(session, row) -> ExperimentAuthorizationStatus:
        events = tuple(
            session.scalars(
                select(ExperimentArmEventRow)
                .where(ExperimentArmEventRow.experiment_id == row.experiment_id)
                .order_by(ExperimentArmEventRow.authorization_revision)
            )
        )
        if len(events) != row.authorization_revision:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
        previous_created_at: datetime | None = None
        for revision, event in enumerate(events, start=1):
            created_at = _stored_utc(event.created_at)
            expected_state = "ARMED" if revision % 2 else "DISARMED"
            expected_action = "ARM" if expected_state == "ARMED" else "DISARM"
            expected_hash = _authorization_event_hash(
                event_id=event.event_id,
                experiment_id=event.experiment_id,
                source_definition_hash=event.source_definition_hash,
                protocol_hash=event.protocol_hash,
                authorization_revision=event.authorization_revision,
                action=event.action,
                authorization_state=event.authorization_state,
                created_at=created_at,
            )
            if (
                event.authorization_revision != revision
                or event.source_definition_hash != row.source_definition_hash
                or event.protocol_hash != row.protocol_hash
                or event.action != expected_action
                or event.authorization_state != expected_state
                or event.entry_authorized != (expected_state == "ARMED")
                or not event.existing_position_risk_management_preserved
                or event.runtime_state != "NOT_CONNECTED"
                or event.execution_eligible
                or not event.paper_trading_only
                or event.event_hash != expected_hash
                or (previous_created_at is not None and created_at < previous_created_at)
            ):
                raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
            previous_created_at = created_at
        event = events[-1]
        created_at = _stored_utc(event.created_at)
        if (
            row.experiment_id != event.experiment_id
            or row.source_definition_hash != event.source_definition_hash
            or row.protocol_hash != event.protocol_hash
            or row.authorization_revision != event.authorization_revision
            or row.authorization_state != event.authorization_state
            or row.entry_authorized != event.entry_authorized
            or not row.existing_position_risk_management_preserved
            or row.runtime_state != "NOT_CONNECTED"
            or row.execution_eligible
            or not row.paper_trading_only
            or row.last_event_hash != event.event_hash
            or _stored_utc(row.updated_at) != created_at
        ):
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
        return ExperimentAuthorizationStatus(
            experiment_id=row.experiment_id,
            source_definition_hash=row.source_definition_hash,
            protocol_hash=row.protocol_hash,
            authorization_revision=row.authorization_revision,
            authorization_state=row.authorization_state,
            entry_authorized=row.entry_authorized,
            authorization_event_hash=row.last_event_hash,
            updated_at=_stored_utc(row.updated_at),
        )

    @staticmethod
    def _decode(row: ReviewedExperimentDefinitionRow) -> ReviewedExperimentDefinition:
        try:
            definition = ReviewedExperimentDefinition.model_validate_json(row.payload_text)
        except (TypeError, ValueError) as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID") from error
        if (
            definition.experiment_id != row.experiment_id
            or definition.version != row.version
            or definition.definition_hash != row.definition_hash
            or definition.lifecycle_state != row.lifecycle_state
            or definition.created_at != _stored_utc(row.created_at)
            or definition.definition_hash
            != hashlib.sha256(
                json.dumps(
                    _hash_material(
                        definition.experiment_id,
                        ReviewedExperimentCreateRequest(
                            original_thesis=definition.original_thesis,
                            reviewed_protocol=definition.reviewed_protocol,
                            curation=definition.curation.model_dump(mode="python"),
                        ),
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        ):
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
        return definition

    @staticmethod
    def _decode_compiled(row: CompiledExperimentVersionRow) -> CompiledExperimentVersion:
        try:
            version = CompiledExperimentVersion.model_validate_json(row.payload_text)
        except (TypeError, ValueError) as error:
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID") from error
        if (
            version.experiment_id != row.experiment_id
            or version.source_version != row.source_version
            or version.compiled_version != row.compiled_version
            or version.source_definition_hash != row.source_definition_hash
            or version.protocol_hash != row.protocol_hash
            or version.lifecycle_state != row.lifecycle_state
            or version.arm_state != row.arm_state
            or version.automation_state != row.automation_state
            or version.execution_eligible != row.execution_eligible
            or version.created_at != _stored_utc(row.created_at)
            or not verify_compiled_protocol(version.compiled_protocol)
        ):
            raise ExperimentRegistryError("EXPERIMENT_REGISTRY_STATE_INVALID")
        return version


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _hash_material(
    experiment_id: UUID,
    request: ReviewedExperimentCreateRequest,
) -> dict[str, object]:
    return {
        "domain": "alphadecay.reviewed-experiment-definition.v1",
        "experiment_id": str(experiment_id),
        "version": 1,
        "lifecycle_state": "REVIEWED",
        "original_thesis": request.original_thesis.model_dump(mode="json"),
        "reviewed_protocol": request.reviewed_protocol.model_dump(mode="json"),
        "curation": request.curation.model_dump(mode="json"),
    }


def _authorization_event_hash(
    *,
    event_id: UUID,
    experiment_id: UUID,
    source_definition_hash: str,
    protocol_hash: str,
    authorization_revision: int,
    action: str,
    authorization_state: str,
    created_at: datetime,
) -> str:
    material = {
        "domain": "alphadecay.experiment-authorization-event.v1",
        "event_id": str(event_id),
        "experiment_id": str(experiment_id),
        "source_definition_hash": source_definition_hash,
        "protocol_hash": protocol_hash,
        "authorization_revision": authorization_revision,
        "action": action,
        "authorization_state": authorization_state,
        "entry_authorized": authorization_state == "ARMED",
        "existing_position_risk_management_preserved": True,
        "runtime_state": "NOT_CONNECTED",
        "execution_eligible": False,
        "paper_trading_only": True,
        "created_at": _utc(created_at).isoformat(),
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
