import { copy } from "../content/copy";
import type {
  AlternativeState,
  MatchState,
  OperationalState,
  QualityState,
  ReplayAction,
} from "./types";

export const actionLabels: Record<ReplayAction, string> = {
  HOLD: copy.alternatives.hold,
  CLOSE: copy.alternatives.close,
  ROLL: copy.alternatives.roll,
  NO_ACTION: copy.alternatives.noAction,
};

export const alternativeStateLabels: Record<AlternativeState, string> = {
  SELECTED: copy.alternatives.selected,
  REJECTED: copy.alternatives.rejected,
  UNAVAILABLE: copy.alternatives.unavailable,
};

export const matchLabels: Record<MatchState, string> = {
  ALIGNED: copy.thesis.aligned,
  WEAKENED: copy.thesis.weakened,
  BROKEN: copy.thesis.broken,
  UNKNOWN: copy.thesis.unknown,
};

export const qualityLabels: Record<QualityState, string> = {
  FRESH: copy.drift.fresh,
  AGING: copy.drift.aging,
  STALE: copy.drift.stale,
  MISSING: copy.drift.missing,
  CROSSED: copy.drift.crossed,
};

export const stateCopy = {
  COLD: copy.states.cold,
  NO_POSITION: copy.states.noPosition,
  STALE: copy.states.stale,
  UNKNOWN: copy.states.unknown,
  ASSIGNMENT: copy.states.assignment,
  BLOCKED: copy.states.blocked,
} satisfies Record<Exclude<OperationalState, "READY">, { heading: string; body: string }>;
