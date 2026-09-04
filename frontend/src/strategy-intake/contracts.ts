import { z } from "zod";
import type { StrategyDraftRequest } from "./types";

const directionSchema = z.enum(["BULLISH", "BEARISH", "NEUTRAL", "UNSURE"]);
const sourceKindSchema = z.enum(["PASTED_TEXT", "TEXT_FILE", "MARKDOWN_FILE"]);
const evidenceItemSchema = z.string().trim().min(1).max(1_000);

const sourceSchema = z.object({
  kind: sourceKindSchema,
  content: z.string().trim().min(20).max(20_000),
  filename: z.string().min(1).max(255).optional(),
}).strict().superRefine((source, context) => {
  if (source.kind === "PASTED_TEXT" && source.filename != null) {
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

const riskBudgetInputSchema = z.object({
  max_loss_dollars: z.string().regex(/^\d{1,12}(?:\.\d{1,6})?$/),
}).strict();

export const StrategyDraftRequestSchema: z.ZodType<StrategyDraftRequest> = z.object({
  source: sourceSchema,
  market_scope: z.string().trim().min(1).max(120),
  direction: directionSchema,
  horizon: z.string().trim().min(1).max(240),
  evidence: z.array(evidenceItemSchema).max(12),
  invalidation: z.array(evidenceItemSchema).max(12),
  risk_budget: riskBudgetInputSchema,
  notes: z.string().trim().min(1).max(4_000).optional(),
}).strict();

export const ProtocolAssumptionSchema = z.enum([
  "USER_BRIEF_UNVERIFIED",
  "OPTIONS_ONLY",
  "PAPER_ONLY",
  "DEFINED_RISK_ONLY",
]);
export const ProtocolQuestionSchema = z.enum([
  "MARKET_SCOPE_REQUIRED",
  "DIRECTION_REQUIRED",
  "HORIZON_REQUIRED",
  "EVIDENCE_REQUIRED",
  "INVALIDATION_REQUIRED",
  "RISK_BUDGET_REQUIRED",
  "DIRECTION_REVIEW_REQUIRED",
]);
export const PromotionRequirementSchema = z.enum([
  "MODEL_CURATION_REQUIRED",
  "EVIDENCE_REVIEW_REQUIRED",
  "RISK_REVIEW_REQUIRED",
  "OWNER_REVIEW_REQUIRED",
]);
export const CandidateStructureFamilySchema = z.enum([
  "BULL_CALL_DEBIT_SPREAD",
  "BEAR_PUT_DEBIT_SPREAD",
  "IRON_CONDOR",
]);
export const EvidenceCheckSchema = z.enum([
  "VERIFY_THESIS_CLAIMS",
  "CHECK_MARKET_DATA_RECENCY",
  "CHECK_OPTION_LIQUIDITY",
  "CHECK_INVALIDATION_STATE",
]);
export const ExitRequirementSchema = z.enum([
  "PROFIT_EXIT_REQUIRED",
  "LOSS_EXIT_REQUIRED",
  "TIME_EXIT_REQUIRED",
]);

const riskBudgetOutputSchema = z.object({
  max_loss_dollars: z.string().regex(/^\d{1,12}(?:\.\d{1,6})?$/).nullable().optional(),
  max_account_percent: z.string().regex(/^\d{1,9}(?:\.\d{1,9})?$/).nullable().optional(),
}).strict();

const intakeResponseSchema = z.object({
  source: sourceSchema,
  market_scope: z.string().min(1).max(120).nullable().optional(),
  direction: directionSchema.nullable().optional(),
  horizon: z.string().min(1).max(240).nullable().optional(),
  evidence: z.array(evidenceItemSchema).max(12),
  invalidation: z.array(evidenceItemSchema).max(12),
  risk_budget: riskBudgetOutputSchema.nullable().optional(),
  notes: z.string().min(1).max(4_000).nullable().optional(),
}).strict();

export const StrategyDraftResponseSchema = z.object({
  schema_version: z.literal("v1"),
  status: z.literal("DRAFT_REVIEW_REQUIRED"),
  curation_status: z.literal("NOT_CURATED"),
  automation_state: z.literal("OFF"),
  execution_eligible: z.literal(false),
  intake: intakeResponseSchema,
  assumptions: z.array(ProtocolAssumptionSchema),
  questions: z.array(ProtocolQuestionSchema),
  required_before_promotion: z.array(PromotionRequirementSchema),
  structure_constraints: z.object({
    options_required: z.literal(true),
    defined_risk_required: z.literal(true),
    naked_short_options_allowed: z.literal(false),
    direction: directionSchema.nullable(),
    candidate_families: z.array(CandidateStructureFamilySchema),
  }).strict(),
  evidence_plan: z.object({
    submitted_evidence: z.array(z.string()),
    required_checks: z.array(EvidenceCheckSchema),
  }).strict(),
  risk_rules: z.object({
    budget: riskBudgetOutputSchema.nullable(),
    loss_must_be_bounded: z.literal(true),
    size_must_fit_budget: z.literal(true),
  }).strict(),
  exit_rules: z.object({
    invalidation: z.array(z.string()),
    required_before_promotion: z.array(ExitRequirementSchema),
  }).strict(),
}).strict();

export type StrategyDraftResponse = z.infer<typeof StrategyDraftResponseSchema>;
export type ProtocolAssumption = z.infer<typeof ProtocolAssumptionSchema>;
export type ProtocolQuestion = z.infer<typeof ProtocolQuestionSchema>;
export type PromotionRequirement = z.infer<typeof PromotionRequirementSchema>;
export type CandidateStructureFamily = z.infer<typeof CandidateStructureFamilySchema>;
export type EvidenceCheck = z.infer<typeof EvidenceCheckSchema>;
export type ExitRequirement = z.infer<typeof ExitRequirementSchema>;
