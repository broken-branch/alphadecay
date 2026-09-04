import type { StrategyCurationResponse } from "../strategy-protocol";
import type {
  MachineRule,
  ProtocolBuilderDraft,
  ProtocolBuilderErrors,
  ProtocolFact,
  ProtocolMetric,
  ProtocolPredicate,
  ReviewedExecutableProtocolRequest,
  RuleDraft,
} from "./types";

export const protocolMetrics: ProtocolMetric[] = [
  "UNDERLYING_LAST_PRICE", "UNDERLYING_SESSION_CLOSE", "UNDERLYING_SESSION_VWAP",
  "UNDERLYING_SMA_20", "UNDERLYING_SMA_50", "UNDERLYING_RETURN_PERCENT",
  "OPTION_RELATIVE_SPREAD_PERCENT", "OPTION_QUOTE_AGE_SECONDS", "DAYS_TO_EXPIRY",
  "POSITION_RETURN_ON_MAX_RISK_PERCENT", "TRADING_SESSIONS_HELD",
  "MINUTES_TO_SESSION_CLOSE",
];

export const protocolFacts: ProtocolFact[] = [
  "PAPER_ACCOUNT_CONFIRMED", "BASELINE_CLEAN", "ACCOUNT_FLAT", "NO_OPEN_ORDER",
  "BUYING_POWER_SUFFICIENT", "NO_PRIOR_ENTRY_ATTEMPT", "MARKET_OPEN",
  "TRADING_NOT_HALTED", "MARKET_DATA_COMPLETE", "OPTION_QUOTES_FRESH",
  "OPTION_QUOTES_SYNCHRONIZED", "OPTION_LIQUIDITY_ACCEPTABLE",
  "RISK_WITHIN_REVIEWED_BUDGET",
];

export function emptyRuleDraft(): RuleDraft {
  return {
    predicateKind: "", leftMetric: "", operator: "", rightKind: "", rightMetric: "",
    rightValue: "", fact: "", expected: "",
  };
}

export function emptyProtocolBuilderDraft(curation: StrategyCurationResponse): ProtocolBuilderDraft {
  const direction = curation.classifications.direction;
  const structure = curation.classifications.structure;
  return {
    opportunityKey: "", definitionVersion: "", benchmarkSymbol: "", allowedEventCodes: "",
    thesisCode: "", invalidationCodes: "",
    direction: direction === "BULLISH" || direction === "BEARISH" ? direction : "",
    structure: structure === "BULL_CALL_DEBIT_SPREAD" || structure === "BEAR_PUT_DEBIT_SPREAD"
      ? structure : "",
    eventSession: "", preEventSession: "", reactionSession: "", signalSession: "",
    dailyStartSession: "", evidenceWindowStart: "", evidenceWindowEnd: "",
    entryWindowStart: "", decisionBoundary: "", entryWindowEnd: "", minimumExpiry: "",
    maximumExpiry: "", minimumDte: "", targetDte: "", maximumDte: "", minimumStrike: "",
    maximumStrike: "", widthDollars: "", quantity: "", maximumDebitPerShare: "",
    maximumLossDollars: "", maximumContractsConsidered: "",
    maximumUnderlyingAgeSeconds: "", maximumOptionQuoteAgeSeconds: "",
    maximumLegQuoteSkewSeconds: "", maximumRelativeSpreadPercent: "",
    minimumLegBidSize: "", minimumLegAskSize: "", entryRule: emptyRuleDraft(),
    noTradeRule: emptyRuleDraft(), profitExitRule: emptyRuleDraft(),
    lossExitRule: emptyRuleDraft(), timeExitRule: emptyRuleDraft(),
    invalidationRules: curation.protocol_fields.invalidation_rules.map(emptyRuleDraft),
  };
}

type BuildResult =
  | { ok: true; value: ReviewedExecutableProtocolRequest; errors: {} }
  | { ok: false; value: null; errors: ProtocolBuilderErrors };

const codePattern = /^[A-Z][A-Z0-9_]{0,63}$/;
const symbolPattern = /^[A-Z][A-Z0-9.-]{0,9}$/;
const decimalPattern = /^-?(?:0|[1-9]\d{0,11})(?:\.\d{1,9})?$/;
const moneyPattern = /^(?:0|[1-9]\d{0,11})(?:\.\d{1,6})?$/;
const percentPattern = /^(?:0|[1-9]\d{0,8})(?:\.\d{1,9})?$/;

function list(value: string): string[] {
  return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
}

function numberField(
  errors: ProtocolBuilderErrors,
  key: string,
  value: string,
  minimum: number,
  maximum: number,
): number | null {
  const parsed = Number(value);
  if (!/^\d+$/.test(value) || !Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    errors[key] = "range";
    return null;
  }
  return parsed;
}

function positiveDecimal(errors: ProtocolBuilderErrors, key: string, value: string): number | null {
  const parsed = Number(value);
  if (!moneyPattern.test(value) || !Number.isFinite(parsed) || parsed <= 0) {
    errors[key] = "number";
    return null;
  }
  return parsed;
}

function positivePercent(errors: ProtocolBuilderErrors, key: string, value: string): number | null {
  const parsed = Number(value);
  if (!percentPattern.test(value) || !Number.isFinite(parsed) || parsed <= 0 || parsed > 100) {
    errors[key] = "number";
    return null;
  }
  return parsed;
}

function utc(value: string): string | null {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) ? `${value}:00Z` : null;
}

function dateValue(value: string): number {
  return Date.parse(`${value}T00:00:00Z`);
}

function validDate(value: string): boolean {
  const timestamp = dateValue(value);
  return /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(timestamp)
    && new Date(timestamp).toISOString().slice(0, 10) === value;
}

function rule(
  errors: ProtocolBuilderErrors,
  key: string,
  sourceText: string | null,
  draft: RuleDraft | undefined,
): MachineRule | null {
  if (!sourceText || !draft || !draft.predicateKind) {
    errors[key] = "rule";
    return null;
  }
  let predicate: ProtocolPredicate | null = null;
  if (draft.predicateKind === "FACT") {
    if (!draft.fact || !draft.expected) errors[key] = "rule";
    else predicate = { kind: "FACT", fact: draft.fact, expected: draft.expected === "true" };
  } else if (!draft.leftMetric || !draft.operator || !draft.rightKind) {
    errors[key] = "rule";
  } else if (draft.rightKind === "METRIC") {
    if (!draft.rightMetric) errors[key] = "rule";
    else predicate = {
      kind: "NUMERIC",
      left: { kind: "METRIC", metric: draft.leftMetric },
      operator: draft.operator,
      right: { kind: "METRIC", metric: draft.rightMetric },
    };
  } else if (!decimalPattern.test(draft.rightValue)) {
    errors[key] = "rule";
  } else {
    predicate = {
      kind: "NUMERIC",
      left: { kind: "METRIC", metric: draft.leftMetric },
      operator: draft.operator,
      right: { kind: "CONSTANT", value: draft.rightValue },
    };
  }
  return predicate ? {
    source_text: sourceText,
    mapping_state: "FULLY_MAPPED",
    match: "ALL",
    predicates: [predicate],
  } : null;
}

export function buildReviewedExecutableProtocolRequest(
  curation: StrategyCurationResponse,
  draft: ProtocolBuilderDraft,
): BuildResult {
  const errors: ProtocolBuilderErrors = {};
  const readiness = curation.classifications;
  if (
    curation.blocking_questions.length
    || [readiness.clarity, readiness.evidence, readiness.risk, readiness.exit]
      .some((item) => item !== "READY")
  ) errors.curation = "review";
  const evidenceById = new Map(
    curation.intake.evidence.map((excerpt, index) => [`evidence-${index + 1}`, excerpt]),
  );
  if (!curation.supporting_evidence.length
    || new Set(curation.supporting_evidence.map((item) => item.evidence_id)).size
      !== curation.supporting_evidence.length
    || curation.supporting_evidence.some(
      (item) => evidenceById.get(item.evidence_id) !== item.excerpt,
    )
    || !curation.protocol_fields.invalidation_rules.length) errors.curation = "review";
  const expectedStructure = curation.classifications.direction === "BULLISH"
    ? "BULL_CALL_DEBIT_SPREAD"
    : curation.classifications.direction === "BEARISH" ? "BEAR_PUT_DEBIT_SPREAD" : null;
  if (
    !expectedStructure
    || draft.direction !== curation.classifications.direction
    || draft.structure !== expectedStructure
    || curation.intake.direction !== draft.direction
  ) errors.direction = "direction";
  if (!curation.intake.market_scope || !/^[A-Z]{1,6}$/.test(curation.intake.market_scope)) {
    errors.curation = "symbol";
  }
  if (!curation.intake.risk_budget?.max_loss_dollars) errors.curation = "risk";
  if (!codePattern.test(draft.opportunityKey)) errors.opportunityKey = "code";
  if (!codePattern.test(draft.thesisCode)) errors.thesisCode = "code";
  if (!symbolPattern.test(draft.benchmarkSymbol)) {
    errors.benchmarkSymbol = "benchmark";
  }
  const allowedEventCodes = list(draft.allowedEventCodes);
  const invalidationCodes = list(draft.invalidationCodes);
  if (!allowedEventCodes.length || allowedEventCodes.length > 12
    || allowedEventCodes.some((item) => !codePattern.test(item))
    || new Set(allowedEventCodes).size !== allowedEventCodes.length) errors.allowedEventCodes = "codes";
  if (invalidationCodes.length !== curation.protocol_fields.invalidation_rules.length
    || invalidationCodes.some((item) => !codePattern.test(item))
    || new Set(invalidationCodes).size !== invalidationCodes.length) errors.invalidationCodes = "codes";

  const definitionVersion = numberField(errors, "definitionVersion", draft.definitionVersion, 1, 1_000_000);
  const quantity = numberField(errors, "quantity", draft.quantity, 1, 100);
  const minimumDte = numberField(errors, "minimumDte", draft.minimumDte, 1, 730);
  const targetDte = numberField(errors, "targetDte", draft.targetDte, 1, 730);
  const maximumDte = numberField(errors, "maximumDte", draft.maximumDte, 1, 730);
  const maximumContracts = numberField(errors, "maximumContractsConsidered", draft.maximumContractsConsidered, 1, 128);
  const underlyingAge = numberField(errors, "maximumUnderlyingAgeSeconds", draft.maximumUnderlyingAgeSeconds, 1, 3_600);
  const quoteAge = numberField(errors, "maximumOptionQuoteAgeSeconds", draft.maximumOptionQuoteAgeSeconds, 1, 120);
  const quoteSkew = numberField(errors, "maximumLegQuoteSkewSeconds", draft.maximumLegQuoteSkewSeconds, 1, 30);
  const bidSize = numberField(errors, "minimumLegBidSize", draft.minimumLegBidSize, 1, 1_000_000);
  const askSize = numberField(errors, "minimumLegAskSize", draft.minimumLegAskSize, 1, 1_000_000);
  const minStrike = positiveDecimal(errors, "minimumStrike", draft.minimumStrike);
  const maxStrike = positiveDecimal(errors, "maximumStrike", draft.maximumStrike);
  const width = positiveDecimal(errors, "widthDollars", draft.widthDollars);
  const debit = positiveDecimal(errors, "maximumDebitPerShare", draft.maximumDebitPerShare);
  const loss = positiveDecimal(errors, "maximumLossDollars", draft.maximumLossDollars);
  positivePercent(errors, "maximumRelativeSpreadPercent", draft.maximumRelativeSpreadPercent);
  if (minimumDte !== null && targetDte !== null && maximumDte !== null
    && !(minimumDte <= targetDte && targetDte <= maximumDte)) errors.targetDte = "dte";
  if (minStrike !== null && maxStrike !== null && (minStrike >= maxStrike || maxStrike - minStrike > 1_000)) {
    errors.maximumStrike = "strike";
  }
  if (width !== null && debit !== null && debit >= width) errors.maximumDebitPerShare = "debit";
  if (debit !== null && quantity !== null && loss !== null
    && Math.abs(debit * quantity * 100 - loss) > 0.000001) errors.maximumLossDollars = "loss";
  if (loss !== null && loss !== Number(curation.intake.risk_budget?.max_loss_dollars)) {
    errors.maximumLossDollars = "risk";
  }

  const sessionFields = [
    ["dailyStartSession", draft.dailyStartSession], ["preEventSession", draft.preEventSession],
    ["eventSession", draft.eventSession], ["reactionSession", draft.reactionSession],
    ["signalSession", draft.signalSession],
  ] as const;
  const invalidSession = sessionFields.some(([, value]) => !validDate(value));
  for (const [field, value] of sessionFields) if (!validDate(value)) errors[field] = "dates";
  if (invalidSession || sessionFields.some(([, value], index) => (
    index > 0 && dateValue(value) < dateValue(sessionFields[index - 1][1])
  ))) errors.sessions = "dates";
  const invalidMinimumExpiry = !validDate(draft.minimumExpiry);
  const invalidMaximumExpiry = !validDate(draft.maximumExpiry);
  if (invalidMinimumExpiry) errors.minimumExpiry = "dates";
  if (invalidMaximumExpiry) errors.maximumExpiry = "dates";
  if (invalidMinimumExpiry || invalidMaximumExpiry
    || dateValue(draft.minimumExpiry) > dateValue(draft.maximumExpiry)
    || (dateValue(draft.maximumExpiry) - dateValue(draft.minimumExpiry)) / 86_400_000 > 45) {
    errors.expiry = "dates";
  }
  if (minimumDte !== null && draft.signalSession && draft.minimumExpiry
    && (dateValue(draft.minimumExpiry) - dateValue(draft.signalSession)) / 86_400_000 !== minimumDte) {
    errors.minimumDte = "expiry";
  }
  if (maximumDte !== null && draft.signalSession && draft.maximumExpiry
    && (dateValue(draft.maximumExpiry) - dateValue(draft.signalSession)) / 86_400_000 !== maximumDte) {
    errors.maximumDte = "expiry";
  }
  const evidenceStart = utc(draft.evidenceWindowStart);
  const evidenceEnd = utc(draft.evidenceWindowEnd);
  const entryStart = utc(draft.entryWindowStart);
  const decision = utc(draft.decisionBoundary);
  const entryEnd = utc(draft.entryWindowEnd);
  if (!evidenceStart) errors.evidenceWindowStart = "time";
  if (!evidenceEnd) errors.evidenceWindowEnd = "time";
  if (!entryStart) errors.entryWindowStart = "time";
  if (!decision) errors.decisionBoundary = "time";
  if (!entryEnd) errors.entryWindowEnd = "time";
  if (!evidenceStart || !evidenceEnd || !entryStart || !decision || !entryEnd) errors.windows = "time";
  else if (!(evidenceStart < evidenceEnd && evidenceEnd <= decision
    && entryStart <= decision && decision < entryEnd)
    || !entryStart.startsWith(draft.signalSession)
    || !decision.startsWith(draft.signalSession)
    || !entryEnd.startsWith(draft.signalSession)
    || Number(draft.decisionBoundary.slice(-2)) % 5 !== 0) errors.windows = "time";

  const entryRule = rule(errors, "entryRule", curation.protocol_fields.entry_rule, draft.entryRule);
  const noTradeRule = rule(errors, "noTradeRule", curation.protocol_fields.no_trade_rule, draft.noTradeRule);
  const profitExitRule = rule(errors, "profitExitRule", curation.protocol_fields.profit_exit_rule, draft.profitExitRule);
  const lossExitRule = rule(errors, "lossExitRule", curation.protocol_fields.loss_exit_rule, draft.lossExitRule);
  const timeExitRule = rule(errors, "timeExitRule", curation.protocol_fields.time_exit_rule, draft.timeExitRule);
  const invalidationRules = curation.protocol_fields.invalidation_rules.map((source, index) => (
    rule(errors, `invalidationRule.${index}`, source, draft.invalidationRules[index])
  ));
  if (Object.keys(errors).length || !entryRule || !noTradeRule || !profitExitRule
    || !lossExitRule || !timeExitRule || invalidationRules.some((item) => item === null)
    || definitionVersion === null || quantity === null || minimumDte === null
    || targetDte === null || maximumDte === null || maximumContracts === null
    || underlyingAge === null || quoteAge === null || quoteSkew === null || bidSize === null
    || askSize === null || !evidenceStart || !evidenceEnd || !entryStart || !decision
    || !entryEnd) return { ok: false, value: null, errors };

  return {
    ok: true,
    errors: {},
    value: {
      review_state: "REVIEWED",
      curation,
      definition: {
        opportunity_key: draft.opportunityKey,
        definition_version: definitionVersion,
        benchmark_symbol: draft.benchmarkSymbol,
        allowed_event_codes: allowedEventCodes,
        thesis_code: draft.thesisCode,
        invalidation_codes: invalidationCodes,
        schedule: {
          event_session: draft.eventSession, pre_event_session: draft.preEventSession,
          reaction_session: draft.reactionSession, signal_session: draft.signalSession,
          daily_start_session: draft.dailyStartSession, evidence_window_start: evidenceStart,
          evidence_window_end: evidenceEnd, entry_window_start: entryStart,
          decision_boundary: decision, entry_window_end: entryEnd,
        },
        selection: {
          minimum_expiry: draft.minimumExpiry, maximum_expiry: draft.maximumExpiry,
          minimum_dte: minimumDte, target_dte: targetDte, maximum_dte: maximumDte,
          minimum_strike: draft.minimumStrike, maximum_strike: draft.maximumStrike,
          width_dollars: draft.widthDollars, quantity,
          maximum_debit_per_share: draft.maximumDebitPerShare,
          maximum_loss_dollars: draft.maximumLossDollars,
          maximum_contracts_considered: maximumContracts,
        },
        market_quality: {
          maximum_underlying_age_seconds: underlyingAge,
          maximum_option_quote_age_seconds: quoteAge,
          maximum_leg_quote_skew_seconds: quoteSkew,
          maximum_relative_spread_percent: draft.maximumRelativeSpreadPercent,
          minimum_leg_bid_size: bidSize, minimum_leg_ask_size: askSize,
        },
        maximum_account_risk_percent: curation.intake.risk_budget?.max_account_percent ?? null,
      },
      rules: {
        entry_rule: entryRule, no_trade_rule: noTradeRule, profit_exit_rule: profitExitRule,
        loss_exit_rule: lossExitRule, time_exit_rule: timeExitRule,
        invalidation_rules: invalidationRules as MachineRule[],
      },
    },
  };
}
