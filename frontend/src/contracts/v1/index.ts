import { z } from "zod";

const MoneyStringSchema = z.string().regex(/^[+-]?\d{1,12}(?:\.\d{1,6})?$/);
const PercentStringSchema = z.string().regex(/^[+-]?\d{1,9}(?:\.\d{1,9})?$/);

function decimalScaled(value: string, places: number): bigint {
  const negative = value.startsWith("-");
  const unsigned = value.startsWith("-") || value.startsWith("+") ? value.slice(1) : value;
  const [whole, fraction = ""] = unsigned.split(".");
  const scale = 10n ** BigInt(places);
  const scaled = BigInt(whole) * scale + BigInt(fraction.padEnd(places, "0"));
  return negative ? -scaled : scaled;
}

export const HealthResponseSchema = z.object({
  schema_version: z.literal("v1"),
  status: z.literal("ok"),
  build: z.string(),
  runtime_mode: z.enum(["CONNECTED", "REPLAY_ONLY"]),
}).strict();

export const PerformancePointSchema = z.object({
  schema_version: z.literal("v1"),
  scheduled_for: z.string().datetime(),
  attempted_at: z.string().datetime(),
  measured_at: z.string().datetime().nullable(),
  status: z.enum(["COMPLETE", "MISSING", "UNKNOWN"]),
  failure_code: z.enum([
    "CAPTURE_NOT_STARTED",
    "PROVIDER_UNAVAILABLE",
    "ACCOUNT_STATE_INCOMPLETE",
    "BASELINE_UNAVAILABLE",
    "SCHEMA_INVALID",
  ]).nullable(),
  current_equity_usd: MoneyStringSchema.nullable(),
  account_equity_change_usd: MoneyStringSchema.nullable(),
  account_equity_return_pct: PercentStringSchema.nullable(),
  reconciled_lifecycle_cashflow_usd: MoneyStringSchema.nullable(),
  open_position_liquidation_pnl_usd: MoneyStringSchema.nullable(),
  broker_write_count: z.number().int().nonnegative().optional(),
  simulator_limitations_code: z.literal("ALPACA_PAPER_SIMULATION"),
}).strict().superRefine((point, context) => {
  const scheduled = Date.parse(point.scheduled_for);
  const attempted = Date.parse(point.attempted_at);
  const measuredAt = point.measured_at === null ? null : Date.parse(point.measured_at);
  if (attempted < scheduled || (measuredAt !== null && measuredAt < attempted)) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "performance time order is invalid" });
  }
  const normalized = [point.account_equity_change_usd, point.account_equity_return_pct];
  const measured = [
    point.current_equity_usd,
    ...normalized,
    point.reconciled_lifecycle_cashflow_usd,
    point.open_position_liquidation_pnl_usd,
  ];
  if (point.status === "COMPLETE") {
    if (point.measured_at === null || point.current_equity_usd === null) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "complete point requires equity" });
    }
    if (point.failure_code !== null) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "complete point cannot fail" });
    }
    if ((normalized[0] === null) !== (normalized[1] === null)) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "normalized values must agree" });
    }
  }
  if (point.status !== "COMPLETE" && point.failure_code === null) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "incomplete point requires failure" });
  }
  if (
    point.status !== "COMPLETE" &&
    (point.measured_at !== null || measured.some((value) => value !== null))
  ) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "incomplete point hides values" });
  }
});

export const BaselineStatusSchema = z.enum([
  "BASELINE_NOT_CAPTURED",
  "BASELINE_UNKNOWN",
  "BASELINE_CLEAN",
  "BASELINE_CONTAMINATED",
]);

export const CompetitionPerformanceProofResponseSchema = z.object({
  schema_version: z.literal("v1"),
  publication_status: z.enum(["NOT_PUBLISHED", "PUBLISHED"]),
  baseline_status: BaselineStatusSchema.nullable(),
  published_at: z.string().datetime().nullable(),
  point: PerformancePointSchema.nullable(),
  linked_certificate_ids: z.array(z.string().uuid()),
  publication_hash: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  predecessor_hash: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
}).strict().superRefine((proof, context) => {
  if (proof.publication_status === "NOT_PUBLISHED") {
    const hidden = [
      proof.baseline_status,
      proof.published_at,
      proof.point,
      proof.publication_hash,
      proof.predecessor_hash,
    ];
    if (hidden.some((value) => value !== null) || proof.linked_certificate_ids.length > 0) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "unpublished proof hides values" });
    }
    return;
  }
  if (
    proof.baseline_status === null ||
    proof.published_at === null ||
    proof.point === null ||
    proof.publication_hash === null
  ) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "published proof is incomplete" });
    return;
  }
  const latestMeasurement = proof.point.measured_at ?? proof.point.attempted_at;
  if (Date.parse(proof.published_at) < Date.parse(latestMeasurement)) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "publication time order is invalid" });
  }
  const normalized = [
    proof.point.account_equity_change_usd,
    proof.point.account_equity_return_pct,
  ];
  const identifiers = proof.linked_certificate_ids;
  if (new Set(identifiers).size !== identifiers.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "linked certificate IDs must be unique" });
  }
  if (identifiers.some((identifier, index) => index > 0 && identifiers[index - 1] > identifier)) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "linked certificate IDs must be sorted" });
  }
  if (
    proof.baseline_status === "BASELINE_CLEAN" &&
    proof.point.status === "COMPLETE" &&
    normalized.some((value) => value === null)
  ) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "clean proof has normalized values" });
  }
  if (
    proof.baseline_status === "BASELINE_CLEAN" &&
    proof.point.status === "COMPLETE" &&
    normalized.every((value) => value !== null) &&
    proof.point.current_equity_usd !== null
  ) {
    const equity = decimalScaled(proof.point.current_equity_usd, 6);
    const change = decimalScaled(normalized[0], 6);
    const returnPercent = decimalScaled(normalized[1], 9);
    if (change !== equity - 100_000_000_000n || returnPercent !== change) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "clean proof normalized values are inconsistent",
      });
    }
  }
  if (
    proof.baseline_status !== "BASELINE_CLEAN" &&
    normalized.some((value) => value !== null)
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "nonclean proof hides normalized values",
    });
  }
});

export type HealthResponse = z.infer<typeof HealthResponseSchema>;
export type PerformancePoint = z.infer<typeof PerformancePointSchema>;
export type CompetitionPerformanceProofResponse = z.infer<
  typeof CompetitionPerformanceProofResponseSchema
>;
