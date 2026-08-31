import { copy } from "../content/copy";
import type { ReplayFixture, ScenarioId } from "./types";

const sharedLineage = {
  policyVersion: "policy-v1",
  fixtureVersion: "fixture-v1",
};

const sharedOpeningThesis = copy.acquisition.openingThesisValue;
const sharedPosition = {
  symbol: copy.positionReview.symbol,
  strikes: copy.positionReview.strikes,
  expiry: copy.positionReview.expiry,
  quantity: copy.positionReview.quantity,
};
const sharedAcquisition = {
  event: copy.acquisition.eventValue,
  trigger: copy.acquisition.triggerValue,
  direction: copy.acquisition.directionValue,
  structure: copy.acquisition.structureValue,
  candidateCheck: copy.acquisition.candidateCheckValue,
  state: "SELECTED" as const,
};
const sharedAutonomy = {
  armed: false,
  scheduledTrigger: copy.autonomy.scheduledTriggerValue,
  emptyBookRoute: copy.autonomy.emptyBookRouteValue,
  managedPositionRoute: copy.autonomy.managedPositionRouteValue,
  policy: copy.autonomy.policyValue,
  reconcile: copy.autonomy.reconcileValue,
};
const sharedMarket = {
  referenceSpot: "$132.50",
  mark: "$2.45",
  bidAsk: "$2.39 / $2.51",
  entryPrice: "$2.35 debit",
  liquidationValue: "$478",
  openPnl: "+$8",
  iv: "50.0%",
  ivChange: "0.0 pts",
  riskCap: "$500",
  orderState: copy.market.noOrderState,
};

export const replayFixtures: Record<ScenarioId, ReplayFixture> = {
  THESIS_INTACT: {
    schemaVersion: "v1",
    scenario: "THESIS_INTACT",
    openingThesis: sharedOpeningThesis,
    position: sharedPosition,
    acquisition: sharedAcquisition,
    autonomy: sharedAutonomy,
    provenance: {
      source: copy.provenanceDetail.sourceValue,
      observedAt: "28 Aug · 15:15 UTC",
      classification: copy.provenanceDetail.supportingClassification,
      support: "SUPPORTED",
    },
    evidenceStatus: "CLASSIFIED",
    evidenceCards: [],
    market: sharedMarket,
    invalidationCode: "PRIMARY_CONTRADICTION",
    action: "HOLD",
    intended: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4 },
    measured: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4, dte: 21, maxLoss: 470, dataAgeSeconds: 0 },
    expectedAfter: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4 },
    reviewTiming: {
      assessedAt: "28 Aug · 15:15 UTC",
      quoteAgeSeconds: 0,
      reviewBy: "28 Aug · 15:45 UTC",
      urgency: "ROUTINE",
    },
    comparison: [
      { key: "direction", measuredValue: "+50", state: "ALIGNED" },
      { key: "volatility", measuredValue: "+4", state: "ALIGNED" },
      { key: "horizon", measuredValue: "21", state: "ALIGNED" },
      { key: "risk", measuredValue: "$470", state: "ALIGNED" },
      { key: "evidence", measuredValue: "0", state: "ALIGNED" }
    ],
    drift: [
      { key: "exposure", points: 0, quality: "FRESH" },
      { key: "volatility", points: 0, quality: "FRESH" },
      { key: "time", points: 0, quality: "FRESH" },
      { key: "evidence", points: 0, quality: "FRESH" },
      { key: "risk", points: 0, quality: "FRESH" }
    ],
    alternatives: [
      { action: "HOLD", state: "SELECTED", reasonKey: "holdAligned" },
      { action: "CLOSE", state: "REJECTED", reasonKey: "closeAligned" },
      { action: "ROLL", state: "REJECTED", reasonKey: "rollAligned" }
    ],
    lineage: {
      ...sharedLineage,
      assessmentHash: "0e3738c4ac8f2268aed8b5bd72628d3d0a84af7efc3dae64c271cd756a1936fc",
      inputHash: "4a4cec4a6f87d5e944505406af19714f278d1511d27a5d8bb62768134c007b7a",
    }
  },
  THETA_TAKEOVER: {
    schemaVersion: "v1",
    scenario: "THETA_TAKEOVER",
    openingThesis: sharedOpeningThesis,
    position: sharedPosition,
    acquisition: sharedAcquisition,
    autonomy: sharedAutonomy,
    provenance: {
      source: copy.provenanceDetail.sourceValue,
      observedAt: "3 Sep · 15:15 UTC",
      classification: copy.provenanceDetail.weakenedClassification,
      support: "SUPPORTED",
    },
    evidenceStatus: "CLASSIFIED",
    evidenceCards: [],
    market: {
      ...sharedMarket,
      mark: "$1.60",
      bidAsk: "$1.54 / $1.66",
      liquidationValue: "$308",
      openPnl: "-$162",
    },
    invalidationCode: "PRIMARY_CONTRADICTION",
    action: "ROLL",
    intended: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4 },
    measured: { delta: 85, gamma: 1, thetaPerDay: -16, vega: 4, dte: 15, maxLoss: 470, dataAgeSeconds: 0 },
    expectedAfter: { delta: 55, gamma: 2, thetaPerDay: -5, vega: 4 },
    reviewTiming: {
      assessedAt: "3 Sep · 15:15 UTC",
      quoteAgeSeconds: 0,
      reviewBy: "3 Sep · 15:20 UTC",
      urgency: "SOON",
    },
    rollProposal: {
      expiry: "25 Sep",
      strikes: "$130 / $135",
      quantity: copy.positionReview.quantity,
      estimatedCost: "$0.10",
      resultingMaxLoss: "$490",
    },
    comparison: [
      { key: "direction", measuredValue: "+85", state: "BROKEN" },
      { key: "volatility", measuredValue: "+4", state: "ALIGNED" },
      { key: "horizon", measuredValue: "15", state: "ALIGNED" },
      { key: "risk", measuredValue: "$470", state: "ALIGNED" },
      { key: "evidence", measuredValue: "50", state: "WEAKENED" }
    ],
    drift: [
      { key: "exposure", points: 60, quality: "FRESH" },
      { key: "volatility", points: 0, quality: "FRESH" },
      { key: "time", points: 40, quality: "FRESH" },
      { key: "evidence", points: 50, quality: "FRESH" },
      { key: "risk", points: 56, quality: "FRESH" }
    ],
    alternatives: [
      { action: "HOLD", state: "REJECTED", reasonKey: "holdTheta" },
      { action: "CLOSE", state: "REJECTED", reasonKey: "closeTheta" },
      { action: "ROLL", state: "SELECTED", reasonKey: "rollTheta" }
    ],
    lineage: {
      ...sharedLineage,
      assessmentHash: "0b278e649fa7efe836489130560c56b93180ec043a0391c64ea5cebf833ea603",
      inputHash: "b547c9aed3be1e34c99d477e669cd509ac6c52f4b2b19362bc093206bc5be1f7",
    }
  },
  CATALYST_BROKEN: {
    schemaVersion: "v1",
    scenario: "CATALYST_BROKEN",
    openingThesis: sharedOpeningThesis,
    position: sharedPosition,
    acquisition: sharedAcquisition,
    autonomy: sharedAutonomy,
    provenance: {
      source: copy.provenanceDetail.sourceValue,
      observedAt: "31 Aug · 15:15 UTC",
      classification: copy.provenanceDetail.contradictedClassification,
      support: "CONTRADICTED",
    },
    evidenceStatus: "CLASSIFIED",
    evidenceCards: [],
    market: {
      ...sharedMarket,
      mark: "$2.15",
      bidAsk: "$2.09 / $2.21",
      liquidationValue: "$418",
      openPnl: "-$52",
    },
    invalidationCode: "PRIMARY_CONTRADICTION",
    action: "CLOSE",
    intended: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4 },
    measured: { delta: 45, gamma: 2, thetaPerDay: -5, vega: 3, dte: 18, maxLoss: 470, dataAgeSeconds: 0 },
    expectedAfter: { delta: 0, gamma: 0, thetaPerDay: 0, vega: 0 },
    reviewTiming: {
      assessedAt: "31 Aug · 15:15 UTC",
      quoteAgeSeconds: 0,
      reviewBy: "31 Aug · 15:16 UTC",
      urgency: "IMMEDIATE",
    },
    comparison: [
      { key: "direction", measuredValue: "+45", state: "ALIGNED" },
      { key: "volatility", measuredValue: "+3", state: "ALIGNED" },
      { key: "horizon", measuredValue: "18", state: "ALIGNED" },
      { key: "risk", measuredValue: "$470", state: "ALIGNED" },
      { key: "evidence", measuredValue: "100", state: "BROKEN" }
    ],
    drift: [
      { key: "exposure", points: 0, quality: "FRESH" },
      { key: "volatility", points: 0, quality: "FRESH" },
      { key: "time", points: 0, quality: "FRESH" },
      { key: "evidence", points: 100, quality: "FRESH" },
      { key: "risk", points: 1, quality: "FRESH" }
    ],
    alternatives: [
      { action: "HOLD", state: "REJECTED", reasonKey: "holdBroken" },
      { action: "CLOSE", state: "SELECTED", reasonKey: "closeBroken" },
      { action: "ROLL", state: "REJECTED", reasonKey: "rollBroken" }
    ],
    lineage: {
      ...sharedLineage,
      assessmentHash: "be90845fd805e9a372ebdc1a27a0cec7613807faf52e8bb76bfd6989beb8c009",
      inputHash: "76b569ab2fd1b5e9248a0487bc2e71cb0e7249cbaf476ad661ef282ec44dea80",
    }
  },
  STALE_QUOTE: {
    schemaVersion: "v1",
    scenario: "STALE_QUOTE",
    openingThesis: sharedOpeningThesis,
    position: sharedPosition,
    acquisition: sharedAcquisition,
    autonomy: sharedAutonomy,
    provenance: {
      source: copy.provenanceDetail.sourceValue,
      observedAt: "28 Aug · 15:14 UTC",
      classification: copy.provenanceDetail.unknownClassification,
      support: "UNKNOWN",
    },
    evidenceStatus: "NOT_RUN",
    evidenceCards: [],
    market: {
      ...sharedMarket,
      mark: copy.market.staleValue,
      bidAsk: copy.market.staleValue,
      liquidationValue: copy.market.notCalculated,
      openPnl: copy.market.notCalculated,
      iv: copy.market.staleValue,
      ivChange: copy.market.notCalculated,
    },
    invalidationCode: "PRIMARY_CONTRADICTION",
    action: "NO_ACTION",
    intended: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4 },
    measured: { delta: 50, gamma: 2, thetaPerDay: -4, vega: 4, dte: 21, maxLoss: 470, dataAgeSeconds: 45 },
    expectedAfter: null,
    reviewTiming: {
      assessedAt: "28 Aug · 15:15 UTC",
      quoteAgeSeconds: 45,
      reviewBy: copy.positionReview.reviewWhenFresh,
      urgency: "WAITING",
    },
    blockedState: "STALE",
    comparison: [
      { key: "direction", measuredValue: "+50", state: "UNKNOWN" },
      { key: "volatility", measuredValue: "+4", state: "UNKNOWN" },
      { key: "horizon", measuredValue: "21", state: "UNKNOWN" },
      { key: "risk", measuredValue: "$470", state: "UNKNOWN" },
      { key: "evidence", measuredValue: "0", state: "UNKNOWN" }
    ],
    drift: [
      { key: "exposure", points: 0, quality: "STALE" },
      { key: "volatility", points: 0, quality: "STALE" },
      { key: "time", points: 0, quality: "STALE" },
      { key: "evidence", points: 0, quality: "STALE" },
      { key: "risk", points: 0, quality: "STALE" }
    ],
    alternatives: [
      { action: "HOLD", state: "UNAVAILABLE", reasonKey: "holdStale" },
      { action: "CLOSE", state: "UNAVAILABLE", reasonKey: "closeStale" },
      { action: "ROLL", state: "UNAVAILABLE", reasonKey: "rollStale" }
    ],
    lineage: {
      ...sharedLineage,
      assessmentHash: "5550837d2a0ef9e789e4b844164d3eb2071d551458cd7e172128f0cd1bfcea95",
      inputHash: "e54bff2ed6c81c06951c545a94d320aca53df3250e00a925d8fe968ed2303edf",
    }
  }
};
