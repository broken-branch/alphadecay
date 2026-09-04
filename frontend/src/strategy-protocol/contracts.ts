import { z } from "zod";

const nonblankSchema = (maximum: number) => z.string().min(1).max(maximum)
  .refine((value) => Boolean(value.trim()));
const evidenceItemSchema = nonblankSchema(1_000);
const nullableMoneySchema = z.string().regex(/^\+?\d{1,12}(?:\.\d{1,6})?$/)
  .refine((value) => Number(value) > 0)
  .nullable();
const nullablePercentSchema = z.string().regex(/^\+?\d{1,9}(?:\.\d{1,9})?$/)
  .refine((value) => Number(value) > 0 && Number(value) <= 100)
  .nullable();

const curationBriefSourceSchema = z.object({
  kind: z.enum(["PASTED_TEXT", "TEXT_FILE", "MARKDOWN_FILE"]),
  content: z.string().min(20).max(20_000).refine((value) => Boolean(value.trim())),
  filename: z.string().min(1).max(255).nullable().default(null),
}).strict().superRefine((source, context) => {
  if (source.kind === "PASTED_TEXT" && source.filename !== null) {
    context.addIssue({ code: "custom", path: ["filename"], message: "Unexpected filename" });
  }
  if (source.kind === "TEXT_FILE" && !source.filename?.toLowerCase().endsWith(".txt")) {
    context.addIssue({ code: "custom", path: ["filename"], message: "Text filename required" });
  }
  if (
    source.kind === "MARKDOWN_FILE"
    && !source.filename?.toLowerCase().endsWith(".md")
    && !source.filename?.toLowerCase().endsWith(".markdown")
  ) {
    context.addIssue({ code: "custom", path: ["filename"], message: "Markdown filename required" });
  }
});

const curationRiskBudgetSchema = z.object({
  max_loss_dollars: nullableMoneySchema.default(null),
  max_account_percent: nullablePercentSchema.default(null),
}).strict().superRefine((budget, context) => {
  if (budget.max_loss_dollars === null && budget.max_account_percent === null) {
    context.addIssue({ code: "custom", message: "Risk limit required" });
  }
});

export const StrategyCurationBriefSchema = z.object({
  source: curationBriefSourceSchema,
  market_scope: nonblankSchema(120).nullable().default(null),
  direction: z.enum(["BULLISH", "BEARISH", "NEUTRAL", "UNSURE"]).nullable().default(null),
  horizon: nonblankSchema(240).nullable().default(null),
  evidence: z.array(evidenceItemSchema).max(12).default([]),
  invalidation: z.array(evidenceItemSchema).max(12).default([]),
  risk_budget: curationRiskBudgetSchema.nullable().default(null),
  notes: nonblankSchema(4_000).nullable().default(null),
}).strict();

export const CuratedDirectionSchema = z.enum(["BULLISH", "BEARISH", "NEUTRAL", "UNSURE"]);
export const CuratedStructureSchema = z.enum([
  "BULL_CALL_DEBIT_SPREAD",
  "BEAR_PUT_DEBIT_SPREAD",
  "IRON_CONDOR",
  "REVIEW_REQUIRED",
]);
export const CurationReadinessSchema = z.enum(["READY", "NEEDS_INPUT", "CONFLICT_REVIEW"]);
export const CurationConfidenceSchema = z.enum(["LOW", "MEDIUM", "HIGH"]);
export const CurationBlockingQuestionSchema = z.enum([
  "MARKET_SCOPE_REQUIRED",
  "DIRECTION_REVIEW_REQUIRED",
  "HORIZON_REQUIRED",
  "EVIDENCE_REQUIRED",
  "RISK_BUDGET_REQUIRED",
  "ENTRY_RULE_REQUIRED",
  "NO_TRADE_RULE_REQUIRED",
  "PROFIT_EXIT_REQUIRED",
  "LOSS_EXIT_REQUIRED",
  "TIME_EXIT_REQUIRED",
  "INVALIDATION_REQUIRED",
  "STRUCTURE_REVIEW_REQUIRED",
]);

const protocolRuleSchema = nonblankSchema(2_000).nullable();

export const StrategyProtocolFieldsSchema = z.object({
  entry_rule: protocolRuleSchema.default(null),
  no_trade_rule: protocolRuleSchema.default(null),
  profit_exit_rule: protocolRuleSchema.default(null),
  loss_exit_rule: protocolRuleSchema.default(null),
  time_exit_rule: protocolRuleSchema.default(null),
  invalidation_rules: z.array(nonblankSchema(2_000)).max(12).default([]),
}).strict();

export const StrategyCurationRequestSchema = z.object({
  brief: StrategyCurationBriefSchema,
  protocol_fields: StrategyProtocolFieldsSchema.default({
    entry_rule: null,
    no_trade_rule: null,
    profit_exit_rule: null,
    loss_exit_rule: null,
    time_exit_rule: null,
    invalidation_rules: [],
  }),
}).strict();

export const StrategyCurationResponseSchema = z.object({
  schema_version: z.literal("v1"),
  status: z.literal("CURATED_REVIEW_REQUIRED"),
  curation_status: z.literal("MODEL_CURATED"),
  automation_state: z.literal("OFF"),
  execution_eligible: z.literal(false),
  paper_trading_only: z.literal(true),
  options_required: z.literal(true),
  defined_risk_required: z.literal(true),
  intake: StrategyCurationBriefSchema,
  protocol_fields: StrategyProtocolFieldsSchema,
  classifications: z.object({
    direction: CuratedDirectionSchema,
    structure: CuratedStructureSchema,
    clarity: CurationReadinessSchema,
    evidence: CurationReadinessSchema,
    risk: CurationReadinessSchema,
    exit: CurationReadinessSchema,
    confidence: CurationConfidenceSchema,
  }).strict(),
  blocking_questions: z.array(CurationBlockingQuestionSchema).max(12)
    .superRefine((questions, context) => {
      if (new Set(questions).size !== questions.length) {
        context.addIssue({ code: "custom", message: "Duplicate blocking question" });
      }
    }),
  supporting_evidence: z.array(z.object({
    evidence_id: z.string().regex(/^evidence-(?:[1-9]|1[0-2])$/),
    excerpt: evidenceItemSchema,
  }).strict()).max(12).superRefine((items, context) => {
    if (new Set(items.map((item) => item.evidence_id)).size !== items.length) {
      context.addIssue({ code: "custom", message: "Duplicate evidence id" });
    }
  }),
}).strict();

export type CuratedDirection = z.infer<typeof CuratedDirectionSchema>;
export type CuratedStructure = z.infer<typeof CuratedStructureSchema>;
export type CurationReadiness = z.infer<typeof CurationReadinessSchema>;
export type CurationConfidence = z.infer<typeof CurationConfidenceSchema>;
export type CurationBlockingQuestion = z.infer<typeof CurationBlockingQuestionSchema>;
export type StrategyProtocolFields = z.infer<typeof StrategyProtocolFieldsSchema>;
export type StrategyCurationBrief = z.infer<typeof StrategyCurationBriefSchema>;
export type StrategyCurationRequest = z.infer<typeof StrategyCurationRequestSchema>;
export type StrategyCurationResponse = z.infer<typeof StrategyCurationResponseSchema>;
