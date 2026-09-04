import { curatedProtocolFixture } from "../strategy-protocol/test-fixture";
import type { StrategyCurationResponse } from "../strategy-protocol";
import type { ProtocolBuilderDraft, RuleDraft } from "./types";

export function readyCurationFixture(): StrategyCurationResponse {
  const fixture = curatedProtocolFixture();
  return {
    ...fixture,
    classifications: { ...fixture.classifications, evidence: "READY", exit: "READY" },
    blocking_questions: [],
  };
}

const numeric = (
  leftMetric: RuleDraft["leftMetric"],
  operator: RuleDraft["operator"],
  rightValue: string,
): RuleDraft => ({
  predicateKind: "NUMERIC",
  leftMetric,
  operator,
  rightKind: "CONSTANT",
  rightMetric: "",
  rightValue,
  fact: "",
  expected: "",
});

export function completeProtocolDraft(): ProtocolBuilderDraft {
  return {
    opportunityKey: "SPY_REVIEWED_EXPERIMENT",
    definitionVersion: "1",
    benchmarkSymbol: "QQQ",
    allowedEventCodes: "USER_THESIS",
    thesisCode: "BULLISH_TREND_CONFIRMATION",
    invalidationCodes: "PRIOR_LOW_LOST",
    direction: "BULLISH",
    structure: "BULL_CALL_DEBIT_SPREAD",
    eventSession: "2026-09-01",
    preEventSession: "2026-08-31",
    reactionSession: "2026-09-01",
    signalSession: "2026-09-02",
    dailyStartSession: "2026-06-01",
    evidenceWindowStart: "2026-09-01T13:30",
    evidenceWindowEnd: "2026-09-02T13:45",
    entryWindowStart: "2026-09-02T13:45",
    decisionBoundary: "2026-09-02T13:50",
    entryWindowEnd: "2026-09-02T14:15",
    minimumExpiry: "2026-10-02",
    maximumExpiry: "2026-10-17",
    minimumDte: "30",
    targetDte: "38",
    maximumDte: "45",
    minimumStrike: "400",
    maximumStrike: "800",
    widthDollars: "5",
    quantity: "1",
    maximumDebitPerShare: "2.4",
    maximumLossDollars: "240",
    maximumContractsConsidered: "64",
    maximumUnderlyingAgeSeconds: "300",
    maximumOptionQuoteAgeSeconds: "20",
    maximumLegQuoteSkewSeconds: "3",
    maximumRelativeSpreadPercent: "5",
    minimumLegBidSize: "1",
    minimumLegAskSize: "1",
    entryRule: {
      predicateKind: "NUMERIC",
      leftMetric: "UNDERLYING_SESSION_CLOSE",
      operator: "GREATER_THAN",
      rightKind: "METRIC",
      rightMetric: "UNDERLYING_SMA_50",
      rightValue: "",
      fact: "",
      expected: "",
    },
    noTradeRule: {
      predicateKind: "FACT",
      leftMetric: "",
      operator: "",
      rightKind: "",
      rightMetric: "",
      rightValue: "",
      fact: "MARKET_DATA_COMPLETE",
      expected: "false",
    },
    profitExitRule: numeric("POSITION_RETURN_ON_MAX_RISK_PERCENT", "GREATER_THAN_OR_EQUAL", "50"),
    lossExitRule: numeric("POSITION_RETURN_ON_MAX_RISK_PERCENT", "LESS_THAN_OR_EQUAL", "-25"),
    timeExitRule: numeric("TRADING_SESSIONS_HELD", "GREATER_THAN_OR_EQUAL", "10"),
    invalidationRules: [{
      predicateKind: "NUMERIC",
      leftMetric: "UNDERLYING_SESSION_CLOSE",
      operator: "LESS_THAN",
      rightKind: "METRIC",
      rightMetric: "UNDERLYING_SMA_50",
      rightValue: "",
      fact: "",
      expected: "",
    }],
  };
}
