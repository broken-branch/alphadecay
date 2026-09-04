import type { StrategyCurationResponse } from "../strategy-protocol";

export type ProtocolMetric =
  | "UNDERLYING_LAST_PRICE"
  | "UNDERLYING_SESSION_CLOSE"
  | "UNDERLYING_SESSION_VWAP"
  | "UNDERLYING_SMA_20"
  | "UNDERLYING_SMA_50"
  | "UNDERLYING_RETURN_PERCENT"
  | "OPTION_RELATIVE_SPREAD_PERCENT"
  | "OPTION_QUOTE_AGE_SECONDS"
  | "DAYS_TO_EXPIRY"
  | "POSITION_RETURN_ON_MAX_RISK_PERCENT"
  | "TRADING_SESSIONS_HELD"
  | "MINUTES_TO_SESSION_CLOSE";

export type ProtocolFact =
  | "PAPER_ACCOUNT_CONFIRMED"
  | "BASELINE_CLEAN"
  | "ACCOUNT_FLAT"
  | "NO_OPEN_ORDER"
  | "BUYING_POWER_SUFFICIENT"
  | "NO_PRIOR_ENTRY_ATTEMPT"
  | "MARKET_OPEN"
  | "TRADING_NOT_HALTED"
  | "MARKET_DATA_COMPLETE"
  | "OPTION_QUOTES_FRESH"
  | "OPTION_QUOTES_SYNCHRONIZED"
  | "OPTION_LIQUIDITY_ACCEPTABLE"
  | "RISK_WITHIN_REVIEWED_BUDGET";

export type ComparisonOperator =
  | "LESS_THAN"
  | "LESS_THAN_OR_EQUAL"
  | "EQUAL"
  | "GREATER_THAN_OR_EQUAL"
  | "GREATER_THAN";

export type MetricOperand = { kind: "METRIC"; metric: ProtocolMetric };
export type ConstantOperand = { kind: "CONSTANT"; value: string };
export type ProtocolPredicate =
  | {
      kind: "NUMERIC";
      left: MetricOperand;
      operator: ComparisonOperator;
      right: MetricOperand | ConstantOperand;
    }
  | { kind: "FACT"; fact: ProtocolFact; expected: boolean };

export type MachineRule = {
  source_text: string;
  mapping_state: "FULLY_MAPPED";
  match: "ALL";
  predicates: [ProtocolPredicate];
};

export type ReviewedExecutableProtocolRequest = {
  review_state: "REVIEWED";
  curation: StrategyCurationResponse;
  definition: {
    opportunity_key: string;
    definition_version: number;
    benchmark_symbol: string;
    allowed_event_codes: string[];
    thesis_code: string;
    invalidation_codes: string[];
    schedule: {
      event_session: string;
      pre_event_session: string;
      reaction_session: string;
      signal_session: string;
      daily_start_session: string;
      evidence_window_start: string;
      evidence_window_end: string;
      entry_window_start: string;
      decision_boundary: string;
      entry_window_end: string;
    };
    selection: {
      minimum_expiry: string;
      maximum_expiry: string;
      minimum_dte: number;
      target_dte: number;
      maximum_dte: number;
      minimum_strike: string;
      maximum_strike: string;
      width_dollars: string;
      quantity: number;
      maximum_debit_per_share: string;
      maximum_loss_dollars: string;
      maximum_contracts_considered: number;
    };
    market_quality: {
      maximum_underlying_age_seconds: number;
      maximum_option_quote_age_seconds: number;
      maximum_leg_quote_skew_seconds: number;
      maximum_relative_spread_percent: string;
      minimum_leg_bid_size: number;
      minimum_leg_ask_size: number;
    };
    maximum_account_risk_percent: string | null;
  };
  rules: {
    entry_rule: MachineRule;
    no_trade_rule: MachineRule;
    profit_exit_rule: MachineRule;
    loss_exit_rule: MachineRule;
    time_exit_rule: MachineRule;
    invalidation_rules: MachineRule[];
  };
};

export type RuleDraft = {
  predicateKind: "" | "NUMERIC" | "FACT";
  leftMetric: "" | ProtocolMetric;
  operator: "" | ComparisonOperator;
  rightKind: "" | "METRIC" | "CONSTANT";
  rightMetric: "" | ProtocolMetric;
  rightValue: string;
  fact: "" | ProtocolFact;
  expected: "" | "true" | "false";
};

export type ProtocolBuilderDraft = {
  opportunityKey: string;
  definitionVersion: string;
  benchmarkSymbol: string;
  allowedEventCodes: string;
  thesisCode: string;
  invalidationCodes: string;
  direction: "" | "BULLISH" | "BEARISH";
  structure: "" | "BULL_CALL_DEBIT_SPREAD" | "BEAR_PUT_DEBIT_SPREAD";
  eventSession: string;
  preEventSession: string;
  reactionSession: string;
  signalSession: string;
  dailyStartSession: string;
  evidenceWindowStart: string;
  evidenceWindowEnd: string;
  entryWindowStart: string;
  decisionBoundary: string;
  entryWindowEnd: string;
  minimumExpiry: string;
  maximumExpiry: string;
  minimumDte: string;
  targetDte: string;
  maximumDte: string;
  minimumStrike: string;
  maximumStrike: string;
  widthDollars: string;
  quantity: string;
  maximumDebitPerShare: string;
  maximumLossDollars: string;
  maximumContractsConsidered: string;
  maximumUnderlyingAgeSeconds: string;
  maximumOptionQuoteAgeSeconds: string;
  maximumLegQuoteSkewSeconds: string;
  maximumRelativeSpreadPercent: string;
  minimumLegBidSize: string;
  minimumLegAskSize: string;
  entryRule: RuleDraft;
  noTradeRule: RuleDraft;
  profitExitRule: RuleDraft;
  lossExitRule: RuleDraft;
  timeExitRule: RuleDraft;
  invalidationRules: RuleDraft[];
};

export type ProtocolBuilderErrors = Record<string, string>;
