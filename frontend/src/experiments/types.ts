import type { CompetitionPerformanceProofResponse } from "../contracts/v1";
import type { CompetitionRecordResponse, PositionRecord } from "../competition-record/api";
import type { ExperimentPerformanceProjection } from "./experiment-performance-contracts";
import type { ExperimentPerformanceClient } from "./experiment-performance-api";

export type ExperimentStatus = "DRAFT" | "WATCHING" | "REJECTED" | "OPEN" | "CLOSED" | "BLOCKED";
export type ExperimentEvidenceState = "SUPPORTS" | "CONTRADICTS" | "NEUTRAL" | "UNKNOWN";

export type ExperimentDefinition = {
  id: string;
  name: string;
  underlying: string;
  thesis: string;
  whyChosen: readonly string[];
  invalidation: readonly string[];
  status: ExperimentStatus;
  source: "PAPER" | "REPLAY";
  structure: string | null;
  maximumRiskUsd: string | null;
  policyVersion?: string | null;
};

export type ExperimentEvidence = {
  id: string;
  title: string;
  detail: string;
  state: ExperimentEvidenceState;
  observedAt: string | null;
  sourceLabel: string | null;
};

export type ExperimentValuePoint = {
  at: string;
  value: number;
};

export type ExperimentBenchmark = {
  label: string;
  returnPct: string | null;
  path?: readonly ExperimentValuePoint[];
};

export type ExperimentPayoffPoint = {
  underlyingPrice: number;
  pnlUsd: number;
};

export type ExperimentPayoffProfile = {
  points: readonly ExperimentPayoffPoint[];
  breakevenUsd: string | null;
  maximumProfitUsd: string | null;
};

export type ExperimentWorkspaceProps = {
  definition: ExperimentDefinition;
  position?: PositionRecord | null;
  proof?: CompetitionPerformanceProofResponse | null;
  evidence?: readonly ExperimentEvidence[];
  valuePath?: readonly ExperimentValuePoint[];
  benchmark?: ExperimentBenchmark | null;
  performanceProjection?: ExperimentPerformanceProjection | null;
  performanceConnection?: {
    authenticated: boolean;
    csrfToken: string | null;
    client?: ExperimentPerformanceClient;
    onSessionRejected?: () => void;
  };
  payoff?: ExperimentPayoffProfile | null;
  createdAt?: string | null;
  rejectionReason?: string | null;
};

export type ExperimentRecordLineage = {
  publicRecordId: string;
  certificateId: string | null;
};

export type ExperimentArchiveState =
  | "PENDING"
  | "UNAVAILABLE"
  | "NOT_PUBLISHED"
  | "POSITION"
  | "NO_TRADE"
  | "LINEAGE_MISMATCH";

export type ExperimentProofState =
  | "PENDING"
  | "UNAVAILABLE"
  | "NOT_PUBLISHED"
  | "MATCHED"
  | "INCOMPLETE"
  | "LINEAGE_MISMATCH"
  | "NOT_APPLICABLE";

export type CompetitionExperimentAdapterInput = {
  definition: ExperimentDefinition;
  archive: CompetitionRecordResponse | null | undefined;
  proof: CompetitionPerformanceProofResponse | null | undefined;
  lineage: ExperimentRecordLineage | null;
  evidence?: readonly ExperimentEvidence[];
};

export type CompetitionExperimentAdapterResult = {
  archiveState: ExperimentArchiveState;
  proofState: ExperimentProofState;
  workspace: ExperimentWorkspaceProps;
};
