import { z } from "zod";
import type { ReviewedExecutableProtocolRequest } from "../protocol-builder";

const hash = z.string().regex(/^[0-9a-f]{64}$/);
const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const utcDateTime = z.string().datetime({ offset: true });
const decimal = z.string().regex(/^-?(?:0|[1-9]\d{0,11})(?:\.\d{1,9})?$/);
const positiveDecimal = z.string().regex(/^(?:0|[1-9]\d{0,11})(?:\.\d{1,9})?$/);

const metric = z.enum([
  "UNDERLYING_LAST_PRICE", "UNDERLYING_SESSION_CLOSE", "UNDERLYING_SESSION_VWAP",
  "UNDERLYING_SMA_20", "UNDERLYING_SMA_50", "UNDERLYING_RETURN_PERCENT",
  "OPTION_RELATIVE_SPREAD_PERCENT", "OPTION_QUOTE_AGE_SECONDS", "DAYS_TO_EXPIRY",
  "POSITION_RETURN_ON_MAX_RISK_PERCENT", "TRADING_SESSIONS_HELD",
  "MINUTES_TO_SESSION_CLOSE",
]);
const fact = z.enum([
  "PAPER_ACCOUNT_CONFIRMED", "BASELINE_CLEAN", "ACCOUNT_FLAT", "NO_OPEN_ORDER",
  "BUYING_POWER_SUFFICIENT", "NO_PRIOR_ENTRY_ATTEMPT", "MARKET_OPEN",
  "TRADING_NOT_HALTED", "MARKET_DATA_COMPLETE", "OPTION_QUOTES_FRESH",
  "OPTION_QUOTES_SYNCHRONIZED", "OPTION_LIQUIDITY_ACCEPTABLE",
  "RISK_WITHIN_REVIEWED_BUDGET",
]);
const operator = z.enum([
  "LESS_THAN", "LESS_THAN_OR_EQUAL", "EQUAL", "GREATER_THAN_OR_EQUAL", "GREATER_THAN",
]);
const metricOperand = z.object({ kind: z.literal("METRIC"), metric }).strict();
const constantOperand = z.object({ kind: z.literal("CONSTANT"), value: decimal }).strict();
const predicate = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("NUMERIC"),
    left: metricOperand,
    operator,
    right: z.discriminatedUnion("kind", [metricOperand, constantOperand]),
  }).strict(),
  z.object({ kind: z.literal("FACT"), fact, expected: z.boolean() }).strict(),
]);
const sourceRule = z.object({
  source_text: z.string().min(1).max(2_000),
  mapping_state: z.literal("FULLY_MAPPED"),
  match: z.literal("ALL"),
  predicates: z.array(predicate).min(1).max(12),
}).strict();
const compiledRule = z.object({
  source_rule_hash: hash,
  match: z.enum(["ALL", "ANY"]),
  predicates: z.array(predicate).min(1).max(12),
}).strict();

const schedule = z.object({
  event_session: date,
  pre_event_session: date,
  reaction_session: date,
  signal_session: date,
  daily_start_session: date,
  evidence_window_start: utcDateTime,
  evidence_window_end: utcDateTime,
  entry_window_start: utcDateTime,
  decision_boundary: utcDateTime,
  entry_window_end: utcDateTime,
}).strict();
const selection = z.object({
  minimum_expiry: date,
  maximum_expiry: date,
  minimum_dte: z.number().int().min(1).max(730),
  target_dte: z.number().int().min(1).max(730),
  maximum_dte: z.number().int().min(1).max(730),
  minimum_strike: positiveDecimal,
  maximum_strike: positiveDecimal,
  width_dollars: positiveDecimal,
  quantity: z.number().int().min(1).max(100),
  maximum_debit_per_share: positiveDecimal,
  maximum_loss_dollars: positiveDecimal,
  maximum_contracts_considered: z.number().int().min(1).max(128),
}).strict();
const marketQuality = z.object({
  maximum_underlying_age_seconds: z.number().int().min(1).max(3_600),
  maximum_option_quote_age_seconds: z.number().int().min(1).max(120),
  maximum_leg_quote_skew_seconds: z.number().int().min(1).max(30),
  maximum_relative_spread_percent: positiveDecimal,
  minimum_leg_bid_size: z.number().int().min(1),
  minimum_leg_ask_size: z.number().int().min(1),
}).strict();
const definition = z.object({
  opportunity_key: z.string().regex(/^[A-Z][A-Z0-9_]{0,63}$/),
  definition_version: z.number().int().min(1).max(1_000_000),
  benchmark_symbol: z.string().regex(/^[A-Z][A-Z0-9.-]{0,9}$/),
  allowed_event_codes: z.array(z.string()).min(1).max(12),
  thesis_code: z.string().regex(/^[A-Z][A-Z0-9_]{0,63}$/),
  invalidation_codes: z.array(z.string()).min(1).max(12),
  schedule,
  selection,
  market_quality: marketQuality,
  maximum_account_risk_percent: positiveDecimal.nullable(),
}).strict();
const rules = z.object({
  entry_rule: sourceRule,
  no_trade_rule: sourceRule,
  profit_exit_rule: sourceRule,
  loss_exit_rule: sourceRule,
  time_exit_rule: sourceRule,
  invalidation_rules: z.array(sourceRule).min(1).max(12),
}).strict();
const compiledRules = z.object({
  entry_rule: compiledRule,
  no_trade_rule: compiledRule,
  profit_exit_rule: compiledRule,
  loss_exit_rule: compiledRule,
  time_exit_rule: compiledRule,
  invalidation_rules: z.array(compiledRule).min(1).max(12),
}).strict();

export const CompileExperimentRequestSchema = z.object({
  source_definition_hash: hash,
  definition,
  rules,
}).strict();

const CompiledStrategyProtocolSchema = z.object({
  review_state: z.literal("REVIEWED"),
  compile_status: z.literal("COMPILABLE"),
  arm_state: z.literal("NOT_ARMED"),
  automation_state: z.literal("OFF"),
  execution_eligible: z.literal(false),
  paper_trading_only: z.literal(true),
  options_required: z.literal(true),
  defined_risk_required: z.literal(true),
  recipe: z.literal("TWO_LEG_DEBIT_VERTICAL"),
  leg_count: z.literal(2),
  net_premium: z.literal("DEBIT"),
  symbol: z.string().regex(/^[A-Z]{1,6}$/),
  direction: z.enum(["BULLISH", "BEARISH"]),
  structure: z.enum(["BULL_CALL_DEBIT_SPREAD", "BEAR_PUT_DEBIT_SPREAD"]),
  risk_budget: z.object({
    max_loss_dollars: positiveDecimal.nullable(),
    max_account_percent: positiveDecimal.nullable(),
  }).strict(),
  definition,
  rules: compiledRules,
  mandatory_safety_facts: z.array(fact),
  source_hash: hash,
  compiler_hash: hash,
  definition_hash: hash,
  protocol_hash: hash,
}).strict();

export const CompiledExperimentVersionSchema = z.object({
  schema_version: z.literal("v1"),
  experiment_id: z.string().uuid(),
  source_version: z.literal(1),
  compiled_version: z.literal(1),
  source_definition_hash: hash,
  lifecycle_state: z.literal("COMPILED"),
  arm_state: z.literal("NOT_ARMED"),
  automation_state: z.literal("OFF"),
  execution_eligible: z.literal(false),
  paper_trading_only: z.literal(true),
  protocol_hash: hash,
  compiled_protocol: CompiledStrategyProtocolSchema,
  created_at: utcDateTime,
}).strict().superRefine((version, context) => {
  if (version.protocol_hash !== version.compiled_protocol.protocol_hash) {
    context.addIssue({ code: "custom", message: "Compiled protocol hash mismatch" });
  }
});

export type CompileExperimentRequest = z.infer<typeof CompileExperimentRequestSchema>;
export type CompiledExperimentVersion = z.infer<typeof CompiledExperimentVersionSchema>;

export function compileRequestFromProtocol(
  sourceDefinitionHash: string,
  protocol: ReviewedExecutableProtocolRequest,
): CompileExperimentRequest {
  return CompileExperimentRequestSchema.parse({
    source_definition_hash: sourceDefinitionHash,
    definition: protocol.definition,
    rules: protocol.rules,
  });
}
