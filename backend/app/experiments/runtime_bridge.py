from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from backend.app.alpaca.opportunity import OpportunityMarketSnapshot
from backend.app.contracts.v1.models import ContractModel
from backend.app.services.opportunity_selection import CandidateSelectionAuthority
from backend.app.strategy_briefs.decision import PositionPhase
from backend.app.strategy_briefs.observations import AcquiredProtocolEvidence
from backend.app.strategy_briefs.tick import (
    CompiledExperimentTick,
    run_compiled_experiment_tick,
)

from .entry_pipeline import (
    ExperimentEntryPipelineResult,
    evaluate_experiment_entry_pipeline,
)
from .models import CompiledExperimentVersion, ExperimentAuthorizationStatus


class ExperimentRuntimeBridgeResult(ContractModel):
    status: Literal["RUNTIME_BRIDGE_EVALUATED"] = "RUNTIME_BRIDGE_EVALUATED"
    authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    automation_state: Literal["OFF"] = "OFF"
    experiment_id: UUID
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_authorization_revision: int = Field(ge=0)
    observed_authorization_revision: int = Field(ge=0)
    observation_source_hashes: tuple[str, ...]
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    tick: CompiledExperimentTick
    entry_pipeline: ExperimentEntryPipelineResult
    validated_compiled: CompiledExperimentVersion = Field(exclude=True, repr=False)
    validated_evidence: AcquiredProtocolEvidence = Field(exclude=True, repr=False)
    validated_authorization: ExperimentAuthorizationStatus = Field(
        exclude=True,
        repr=False,
    )
    validated_snapshot: OpportunityMarketSnapshot = Field(exclude=True, repr=False)
    validated_candidate_authority: CandidateSelectionAuthority = Field(
        exclude=True,
        repr=False,
    )
    schedule_authorized: Literal[False] = False
    provider_access_authorized: Literal[False] = False
    broker_access_authorized: Literal[False] = False
    order_authorized: Literal[False] = False
    execution_eligible: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def restore_pipeline_anchors(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("entry_pipeline"), dict):
            return value
        anchor_names = {
            "validated_compiled": "validated_compiled",
            "validated_tick": "tick",
            "validated_authorization": "validated_authorization",
            "validated_snapshot": "validated_snapshot",
            "validated_candidate_authority": "validated_candidate_authority",
        }
        if not all(name in value for name in anchor_names.values()):
            return value
        restored = dict(value)
        pipeline = dict(value["entry_pipeline"])
        for pipeline_name, bridge_name in anchor_names.items():
            pipeline[pipeline_name] = value[bridge_name]
        restored["entry_pipeline"] = pipeline
        return restored

    @model_validator(mode="after")
    def exact_inputs_reproduce_result(self) -> ExperimentRuntimeBridgeResult:
        expected_tick = run_compiled_experiment_tick(
            self.validated_compiled.compiled_protocol,
            self.validated_evidence,
            PositionPhase.FLAT,
        )
        if expected_tick != self.tick:
            raise ValueError("runtime bridge tick does not reproduce from exact inputs")
        expected_pipeline = evaluate_experiment_entry_pipeline(
            self.validated_compiled,
            expected_tick,
            self.validated_authorization,
            expected_authorization_revision=self.expected_authorization_revision,
            snapshot=self.validated_snapshot,
            candidate_selection_authority=self.validated_candidate_authority,
        )
        if expected_pipeline != self.entry_pipeline:
            raise ValueError("runtime bridge pipeline does not reproduce from exact inputs")

        runtime = expected_pipeline.runtime_authority
        if (
            self.experiment_id != runtime.experiment_id
            or self.source_definition_hash != runtime.source_definition_hash
            or self.protocol_source_hash != runtime.protocol_source_hash
            or self.protocol_hash != expected_tick.protocol_hash
            or self.protocol_hash != runtime.protocol_hash
            or self.expected_authorization_revision != runtime.expected_authorization_revision
            or self.observed_authorization_revision != runtime.observed_authorization_revision
            or self.observation_source_hashes != expected_tick.observation_source_hashes
            or self.snapshot_hash != expected_pipeline.snapshot_hash
            or self.account_fingerprint != expected_pipeline.account_fingerprint
            or expected_tick.position_phase is not PositionPhase.FLAT
            or expected_tick.decision.classification is not runtime.protocol_decision
        ):
            raise ValueError("runtime bridge lineage mismatch")
        return self


def evaluate_experiment_runtime_bridge(
    compiled: CompiledExperimentVersion,
    evidence: AcquiredProtocolEvidence,
    authorization: ExperimentAuthorizationStatus,
    *,
    expected_authorization_revision: int,
    snapshot: OpportunityMarketSnapshot,
    candidate_selection_authority: CandidateSelectionAuthority,
) -> ExperimentRuntimeBridgeResult:
    tick = run_compiled_experiment_tick(
        compiled.compiled_protocol,
        evidence,
        PositionPhase.FLAT,
    )
    entry_pipeline = evaluate_experiment_entry_pipeline(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=expected_authorization_revision,
        snapshot=snapshot,
        candidate_selection_authority=candidate_selection_authority,
    )
    runtime = entry_pipeline.runtime_authority
    return ExperimentRuntimeBridgeResult(
        experiment_id=runtime.experiment_id,
        source_definition_hash=runtime.source_definition_hash,
        protocol_source_hash=runtime.protocol_source_hash,
        protocol_hash=runtime.protocol_hash,
        expected_authorization_revision=runtime.expected_authorization_revision,
        observed_authorization_revision=runtime.observed_authorization_revision,
        observation_source_hashes=tick.observation_source_hashes,
        snapshot_hash=entry_pipeline.snapshot_hash,
        account_fingerprint=entry_pipeline.account_fingerprint,
        tick=tick,
        entry_pipeline=entry_pipeline,
        validated_compiled=compiled,
        validated_evidence=evidence,
        validated_authorization=authorization,
        validated_snapshot=snapshot,
        validated_candidate_authority=candidate_selection_authority,
    )
