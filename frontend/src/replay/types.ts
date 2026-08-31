export type ScenarioId = "THESIS_INTACT" | "THETA_TAKEOVER" | "CATALYST_BROKEN" | "STALE_QUOTE";
export type ReplayAction = "HOLD" | "CLOSE" | "ROLL" | "NO_ACTION";
export type MatchState = "ALIGNED" | "WEAKENED" | "BROKEN" | "UNKNOWN";
export type QualityState = "FRESH" | "AGING" | "STALE" | "MISSING" | "CROSSED";
export type AlternativeState = "SELECTED" | "REJECTED" | "UNAVAILABLE";
export type OperationalState = "READY" | "COLD" | "NO_POSITION" | "STALE" | "UNKNOWN" | "ASSIGNMENT" | "BLOCKED";
export type ReviewUrgency = "ROUTINE" | "SOON" | "IMMEDIATE" | "WAITING";

export type GreekExposure = {
  delta: number;
  gamma: number;
  thetaPerDay: number;
  vega: number;
};

export type ComparisonRow = {
  key: "direction" | "volatility" | "horizon" | "risk" | "evidence";
  measuredValue: string;
  state: MatchState;
};

export type DriftContribution = {
  key: "exposure" | "volatility" | "time" | "evidence" | "risk";
  points: number;
  quality: QualityState;
};

export type Alternative = {
  action: ReplayAction;
  state: AlternativeState;
  reasonKey:
    | "holdAligned"
    | "holdTheta"
    | "holdBroken"
    | "closeAligned"
    | "closeTheta"
    | "closeBroken"
    | "rollAligned"
    | "rollTheta"
    | "rollBroken"
    | "holdStale"
    | "closeStale"
    | "rollStale";
};

export type ReplayFixture = {
  schemaVersion: "v1";
  scenario: ScenarioId;
  openingThesis: string;
  invalidationCode: "PRIMARY_CONTRADICTION";
  action: ReplayAction;
  position: {
    symbol: string;
    strikes: string;
    expiry: string;
    quantity: string;
  };
  intended: GreekExposure;
  measured: GreekExposure & {
    dte: number;
    maxLoss: number;
    dataAgeSeconds: number;
  };
  acquisition: {
    event: string;
    trigger: string;
    direction: string;
    structure: string;
    candidateCheck: string;
    state: "SELECTED" | "REJECTED" | "NO_OP";
  };
  autonomy: {
    armed: boolean;
    scheduledTrigger: string;
    emptyBookRoute: string;
    managedPositionRoute: string;
    policy: string;
    reconcile: string;
  };
  provenance: {
    source: string;
    observedAt: string;
    classification: string;
    support: "SUPPORTED" | "CONTRADICTED" | "UNKNOWN";
  };
  evidenceStatus: "CLASSIFIED" | "NOT_RUN";
  evidenceCards: Array<{
    sourceId: string;
    headline: string;
    observedAt: string;
    eventCode: string;
    relation: "SUPPORTS" | "CONTRADICTS" | "NEUTRAL";
    materiality: number;
    relevance: number;
    confidence: number;
    sourceTier: "PRIMARY" | "ORIGINAL_REPORTING" | "SECONDARY";
  }>;
  market: {
    referenceSpot: string;
    mark: string;
    bidAsk: string;
    entryPrice: string;
    liquidationValue: string;
    openPnl: string;
    iv: string;
    ivChange: string;
    riskCap: string;
    orderState: string;
  };
  expectedAfter: GreekExposure | null;
  reviewTiming: {
    assessedAt: string;
    quoteAgeSeconds: number;
    reviewBy: string;
    urgency: ReviewUrgency;
  };
  rollProposal?: {
    expiry: string;
    strikes: string;
    quantity: string;
    estimatedCost: string;
    resultingMaxLoss: string;
  };
  blockedState?: "STALE";
  comparison: ComparisonRow[];
  drift: DriftContribution[];
  alternatives: Alternative[];
  lineage: {
    policyVersion: string;
    fixtureVersion: string;
    assessmentHash: string;
    inputHash: string;
  };
};
