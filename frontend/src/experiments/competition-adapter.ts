import type { CompetitionPerformanceProofResponse } from "../contracts/v1";
import type { PositionRecord } from "../competition-record/api";
import { copy as publicCopy } from "../content/copy";
import type {
  CompetitionExperimentAdapterInput,
  CompetitionExperimentAdapterResult,
  ExperimentProofState,
} from "./types";

const copy = publicCopy.experiment;

function proofState(
  proof: CompetitionPerformanceProofResponse | null | undefined,
  position: PositionRecord,
  certificateId: string | null,
): { state: ExperimentProofState; proof: CompetitionPerformanceProofResponse | null | undefined } {
  if (proof === undefined) return { state: "PENDING", proof: undefined };
  if (proof === null) return { state: "UNAVAILABLE", proof: null };
  if (proof.publication_status === "NOT_PUBLISHED") {
    return { state: "NOT_PUBLISHED", proof };
  }
  if (
    certificateId === null
    || proof.linked_certificate_ids.length !== 1
    || proof.linked_certificate_ids[0] !== certificateId
  ) {
    return { state: "LINEAGE_MISMATCH", proof: null };
  }
  if (
    proof.baseline_status !== "BASELINE_CLEAN"
    || proof.point?.status !== "COMPLETE"
    || proof.point.measured_at === null
  ) {
    return { state: "INCOMPLETE", proof: null };
  }
  if (Date.parse(proof.point.measured_at) < Date.parse(position.payload.as_of)) {
    return { state: "LINEAGE_MISMATCH", proof: null };
  }
  return { state: "MATCHED", proof };
}

function unavailableResult(
  input: CompetitionExperimentAdapterInput,
  archiveState: CompetitionExperimentAdapterResult["archiveState"],
  proofStateOverride?: ExperimentProofState,
): CompetitionExperimentAdapterResult {
  const unavailableProofState: ExperimentProofState = input.proof === undefined
    ? "PENDING"
    : input.proof === null
      ? "UNAVAILABLE"
      : input.proof.publication_status === "NOT_PUBLISHED"
        ? "NOT_PUBLISHED"
        : "LINEAGE_MISMATCH";
  return {
    archiveState,
    proofState: proofStateOverride ?? unavailableProofState,
    workspace: {
      definition: input.definition,
      position: null,
      proof: input.proof === null ? null : undefined,
      evidence: input.evidence ?? [],
      benchmark: null,
      performanceProjection: null,
      payoff: null,
      valuePath: [],
      createdAt: null,
      rejectionReason: null,
    },
  };
}

export function adaptCompetitionExperiment(
  input: CompetitionExperimentAdapterInput,
): CompetitionExperimentAdapterResult {
  if (input.definition.source === "REPLAY") {
    return unavailableResult(input, "LINEAGE_MISMATCH", "NOT_APPLICABLE");
  }
  if (input.archive === undefined) return unavailableResult(input, "PENDING");
  if (input.archive === null) return unavailableResult(input, "UNAVAILABLE");
  if (input.archive.publication_status === "NOT_PUBLISHED") {
    return unavailableResult(input, "NOT_PUBLISHED");
  }
  if (input.lineage === null) return unavailableResult(input, "LINEAGE_MISMATCH");
  const lineage = input.lineage;

  const record = input.archive.records.find(
    (candidate) => candidate.public_record_id === lineage.publicRecordId,
  );
  if (!record) return unavailableResult(input, "LINEAGE_MISMATCH");

  if (record.payload.record_kind === "NO_TRADE") {
    if (lineage.certificateId !== null) {
      return unavailableResult(input, "LINEAGE_MISMATCH");
    }
    return {
      archiveState: "NO_TRADE",
      proofState: "NOT_APPLICABLE",
      workspace: {
        definition: { ...input.definition, status: "REJECTED" },
        position: null,
        proof: undefined,
        evidence: input.evidence ?? [],
        benchmark: null,
        performanceProjection: null,
        payoff: null,
        valuePath: [],
        createdAt: record.payload.decided_at,
        rejectionReason: copy.adapter.strategyNotReady,
      },
    };
  }

  const position: PositionRecord = { ...record, payload: record.payload };
  const matchedProof = proofState(input.proof, position, lineage.certificateId);
  return {
    archiveState: "POSITION",
    proofState: matchedProof.state,
    workspace: {
      definition: {
        ...input.definition,
        status: position.payload.state,
        underlying: position.payload.underlying,
      },
      position,
      proof: matchedProof.proof,
      evidence: input.evidence ?? [],
      benchmark: null,
      performanceProjection: null,
      payoff: null,
      valuePath: [],
      createdAt: null,
      rejectionReason: null,
    },
  };
}
