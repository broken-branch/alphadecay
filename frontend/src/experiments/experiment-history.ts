import type { CompetitionRecord, CompetitionRecordResponse } from "../competition-record/api";
import { copy as publicCopy } from "../content/copy";

const copy = publicCopy.experiment.history;

export type ExperimentHistorySourceState = "PENDING" | "UNAVAILABLE" | "NOT_PUBLISHED" | "PUBLISHED";

export type ExperimentHistoryDecision = {
  action: string;
  reason: string;
  occurredAt: string;
};

export type ExperimentHistoryThesis = {
  direction: string;
  volatilityView: string;
  targetAt: string;
} | null;

export type ExperimentHistoryRecord = {
  selectionKey: string;
  publicationHash: string;
  publicRecordId: string;
  kind: "POSITION" | "NO_TRADE";
  label: string;
  state: string;
  underlying: string | null;
  occurredAt: string;
  publishedAt: string;
  thesis: ExperimentHistoryThesis;
  maximumRisk: null;
  result: null;
  latestDecision: ExperimentHistoryDecision;
};

export type ExperimentHistoryProjection = {
  sourceState: ExperimentHistorySourceState;
  records: readonly ExperimentHistoryRecord[];
};

const actionLabels = {
  ENTRY: copy.entry,
  ROLL: copy.roll,
  CLOSE: copy.close,
  HOLD: copy.hold,
  NO_ACTION: copy.noAction,
} as const;

const assessmentActionLabels = {
  CLOSE: copy.closeSelected,
  ROLL: copy.rollSelected,
} as const;

const reasonLabels = {
  POSITION_OPENED: copy.positionOpened,
  POSITION_ROLLED: copy.positionRolled,
  POSITION_CLOSED: copy.positionClosedReason,
  POSITION_REVIEWED: copy.positionReviewed,
  RISK_REDUCTION: copy.riskReduction,
  THESIS_CHANGED: copy.thesisChanged,
  POSITION_ADJUSTMENT: copy.positionAdjustment,
  DATA_INCOMPLETE: copy.dataIncomplete,
} as const;

const directionLabels = {
  BULLISH: copy.bullish,
  BEARISH: copy.bearish,
  NEUTRAL: copy.neutral,
} as const;

const volatilityLabels = {
  LONG: copy.longVolatility,
  SHORT: copy.shortVolatility,
  NEUTRAL: copy.neutralVolatility,
} as const;

function projectRecord(record: CompetitionRecord, index: number): ExperimentHistoryRecord {
  const selectionKey = `${index}:${record.publication_hash}`;
  if (record.payload.record_kind === "NO_TRADE") {
    return {
      selectionKey,
      publicationHash: record.publication_hash,
      publicRecordId: record.public_record_id,
      kind: "NO_TRADE",
      label: copy.noTrade,
      state: copy.noPositionOpened,
      underlying: null,
      occurredAt: record.occurred_at,
      publishedAt: record.published_at,
      thesis: null,
      maximumRisk: null,
      result: null,
      latestDecision: {
        action: copy.noTrade,
        reason: copy.strategyNotReady,
        occurredAt: record.payload.decided_at,
      },
    };
  }

  const latest = record.payload.events.at(-1)!;
  const action = latest.event_kind === "ASSESSMENT" && (latest.action === "CLOSE" || latest.action === "ROLL")
    ? assessmentActionLabels[latest.action]
    : actionLabels[latest.action];

  return {
    selectionKey,
    publicationHash: record.publication_hash,
    publicRecordId: record.public_record_id,
    kind: "POSITION",
    label: `${copy.position} · ${record.payload.underlying}`,
    state: record.payload.state === "OPEN" ? copy.positionOpen : copy.positionClosed,
    underlying: record.payload.underlying,
    occurredAt: record.occurred_at,
    publishedAt: record.published_at,
    thesis: {
      direction: directionLabels[record.payload.thesis.direction],
      volatilityView: volatilityLabels[record.payload.thesis.volatility_view],
      targetAt: record.payload.thesis.target_at,
    },
    maximumRisk: null,
    result: null,
    latestDecision: {
      action,
      reason: reasonLabels[latest.reason_category],
      occurredAt: latest.occurred_at,
    },
  };
}

export function projectExperimentHistory(
  response: CompetitionRecordResponse | null | undefined,
): ExperimentHistoryProjection {
  if (response === undefined) return { sourceState: "PENDING", records: [] };
  if (response === null) return { sourceState: "UNAVAILABLE", records: [] };
  if (response.publication_status === "NOT_PUBLISHED") {
    return { sourceState: "NOT_PUBLISHED", records: [] };
  }
  return {
    sourceState: "PUBLISHED",
    records: response.records.map(projectRecord),
  };
}
