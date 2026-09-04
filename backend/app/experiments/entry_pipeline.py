from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from backend.app.alpaca.opportunity import (
    OpportunityMarketSnapshot,
    opportunity_market_snapshot_digest,
)
from backend.app.contracts.v1.models import ContractModel
from backend.app.services.opportunity_selection import CandidateSelectionAuthority
from backend.app.strategy_briefs.selection import (
    CompiledCandidateSelection,
    select_compiled_vertical_candidate,
)
from backend.app.strategy_briefs.tick import CompiledExperimentTick

from .models import CompiledExperimentVersion, ExperimentAuthorizationStatus
from .runtime_authority import (
    ExperimentRuntimeAuthorityDecision,
    ExperimentRuntimeDisposition,
    evaluate_experiment_runtime_authority,
)


class ExperimentEntryPipelineResult(ContractModel):
    status: Literal["ENTRY_PIPELINE_EVALUATED"] = "ENTRY_PIPELINE_EVALUATED"
    authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    automation_state: Literal["OFF"] = "OFF"
    runtime_authority: ExperimentRuntimeAuthorityDecision
    candidate_selection: CompiledCandidateSelection | None
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    validated_snapshot: OpportunityMarketSnapshot = Field(exclude=True, repr=False)
    validated_candidate_authority: CandidateSelectionAuthority = Field(
        exclude=True,
        repr=False,
    )
    validated_compiled: CompiledExperimentVersion = Field(exclude=True, repr=False)
    validated_tick: CompiledExperimentTick = Field(exclude=True, repr=False)
    validated_authorization: ExperimentAuthorizationStatus = Field(
        exclude=True,
        repr=False,
    )
    schedule_authorized: Literal[False] = False
    provider_access_authorized: Literal[False] = False
    broker_access_authorized: Literal[False] = False
    order_authorized: Literal[False] = False
    execution_eligible: Literal[False] = False

    @model_validator(mode="after")
    def component_lineage_is_exact(self) -> ExperimentEntryPipelineResult:
        expected_runtime_authority = evaluate_experiment_runtime_authority(
            self.validated_compiled,
            self.validated_tick,
            self.validated_authorization,
            expected_authorization_revision=(
                self.runtime_authority.expected_authorization_revision
            ),
        )
        if expected_runtime_authority != self.runtime_authority:
            raise ValueError("runtime authority does not reproduce from exact inputs")
        snapshot = self.validated_snapshot
        candidate_authority = self.validated_candidate_authority
        if (
            type(snapshot) is not OpportunityMarketSnapshot
            or type(candidate_authority) is not CandidateSelectionAuthority
            or snapshot.source_hash != opportunity_market_snapshot_digest(snapshot)
            or self.snapshot_hash != snapshot.source_hash
            or self.account_fingerprint != snapshot.account_book.account_fingerprint
            or candidate_authority.snapshot_source_hash != snapshot.source_hash
            or candidate_authority.snapshot_request_hash != snapshot.request_hash
            or candidate_authority.account_fingerprint != self.account_fingerprint
        ):
            raise ValueError("pipeline input identity mismatch")
        selection_allowed = (
            self.runtime_authority.disposition
            is ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED
        )
        if selection_allowed != (self.candidate_selection is not None):
            raise ValueError("candidate selection must follow exact runtime authority")
        if self.candidate_selection is not None:
            candidate = self.candidate_selection
            if (
                candidate.status != "CANDIDATE_REVIEW"
                or candidate.authority_state != "NON_AUTHORITATIVE"
                or candidate.arm_state != "NOT_ARMED"
                or candidate.automation_state != "OFF"
                or candidate.execution_eligible is not False
            ):
                raise ValueError("candidate selection cannot grant authority")
            if (
                candidate.protocol_hash != self.runtime_authority.protocol_hash
                or candidate.protocol_source_hash != self.runtime_authority.protocol_source_hash
                or candidate.tick_protocol_hash != self.runtime_authority.protocol_hash
                or candidate.snapshot_hash != self.snapshot_hash
                or candidate.account_fingerprint != self.account_fingerprint
            ):
                raise ValueError("candidate selection lineage mismatch")
            expected_selection = select_compiled_vertical_candidate(
                self.validated_compiled.compiled_protocol,
                self.validated_tick,
                snapshot,
                candidate_authority,
            )
            if expected_selection != candidate:
                raise ValueError("candidate selection does not reproduce from exact inputs")
        return self


def evaluate_experiment_entry_pipeline(
    compiled: CompiledExperimentVersion,
    tick: CompiledExperimentTick,
    authorization: ExperimentAuthorizationStatus,
    *,
    expected_authorization_revision: int,
    snapshot: OpportunityMarketSnapshot,
    candidate_selection_authority: CandidateSelectionAuthority,
) -> ExperimentEntryPipelineResult:
    runtime_authority = evaluate_experiment_runtime_authority(
        compiled,
        tick,
        authorization,
        expected_authorization_revision=expected_authorization_revision,
    )
    candidate_selection = None
    if runtime_authority.disposition is ExperimentRuntimeDisposition.ENTRY_SELECTION_ALLOWED:
        candidate_selection = select_compiled_vertical_candidate(
            compiled.compiled_protocol,
            tick,
            snapshot,
            candidate_selection_authority,
        )
    return ExperimentEntryPipelineResult(
        runtime_authority=runtime_authority,
        candidate_selection=candidate_selection,
        snapshot_hash=snapshot.source_hash,
        account_fingerprint=candidate_selection_authority.account_fingerprint,
        validated_snapshot=snapshot,
        validated_candidate_authority=candidate_selection_authority,
        validated_compiled=compiled,
        validated_tick=tick,
        validated_authorization=authorization,
    )
