import type { StrategyCurationResponse } from "./contracts";

export function curatedProtocolFixture(): StrategyCurationResponse {
  return {
    schema_version: "v1",
    status: "CURATED_REVIEW_REQUIRED",
    curation_status: "MODEL_CURATED",
    automation_state: "OFF",
    execution_eligible: false,
    paper_trading_only: true,
    options_required: true,
    defined_risk_required: true,
    intake: {
      source: {
        kind: "PASTED_TEXT",
        content: "SPY may rise if broad participation improves after the opening range.",
        filename: null,
      },
      market_scope: "SPY",
      direction: "BULLISH",
      horizon: "A few weeks",
      evidence: ["More sectors hold above the opening range."],
      invalidation: ["SPY closes below the prior session low."],
      risk_budget: { max_loss_dollars: "240", max_account_percent: null },
      notes: null,
    },
    protocol_fields: {
      entry_rule: "Enter only after SPY closes above the opening range.",
      no_trade_rule: "Stand aside when the opening range is not confirmed by broad participation.",
      profit_exit_rule: "Close after the spread reaches the reviewed profit target.",
      loss_exit_rule: "Close before the spread reaches the reviewed loss limit.",
      time_exit_rule: "Close no later than the final reviewed session.",
      invalidation_rules: ["SPY closes below the prior session low."],
    },
    classifications: {
      direction: "BULLISH",
      structure: "BULL_CALL_DEBIT_SPREAD",
      clarity: "READY",
      evidence: "NEEDS_INPUT",
      risk: "READY",
      exit: "CONFLICT_REVIEW",
      confidence: "MEDIUM",
    },
    blocking_questions: ["EVIDENCE_REQUIRED", "TIME_EXIT_REQUIRED"],
    supporting_evidence: [{
      evidence_id: "evidence-1",
      excerpt: "More sectors hold above the opening range.",
    }],
  };
}
