import { z } from "zod";
import {
  StrategyCurationBriefSchema,
  StrategyCurationResponseSchema,
  StrategyProtocolFieldsSchema,
} from "../strategy-protocol";

export const ReviewedExperimentCreateRequestSchema = z.object({
  schema_version: z.literal("v1").optional(),
  original_thesis: StrategyCurationBriefSchema,
  reviewed_protocol: StrategyProtocolFieldsSchema,
  curation: StrategyCurationResponseSchema,
}).strict().superRefine((definition, context) => {
  if (
    JSON.stringify(definition.curation.intake) !== JSON.stringify(definition.original_thesis)
    || JSON.stringify(definition.curation.protocol_fields) !== JSON.stringify(definition.reviewed_protocol)
  ) {
    context.addIssue({ code: "custom", message: "Reviewed source mismatch" });
  }
});

export const ReviewedExperimentDefinitionSchema = z.object({
  schema_version: z.literal("v1"),
  experiment_id: z.string().uuid(),
  version: z.literal(1),
  definition_hash: z.string().regex(/^[0-9a-f]{64}$/),
  lifecycle_state: z.literal("REVIEWED"),
  automation_state: z.literal("OFF"),
  execution_eligible: z.literal(false),
  paper_trading_only: z.literal(true),
  original_thesis: StrategyCurationBriefSchema,
  reviewed_protocol: StrategyProtocolFieldsSchema,
  curation: StrategyCurationResponseSchema,
  created_at: z.string().datetime({ offset: true }),
}).strict().superRefine((definition, context) => {
  if (
    JSON.stringify(definition.curation.intake) !== JSON.stringify(definition.original_thesis)
    || JSON.stringify(definition.curation.protocol_fields) !== JSON.stringify(definition.reviewed_protocol)
  ) {
    context.addIssue({ code: "custom", message: "Reviewed source mismatch" });
  }
});

export const ReviewedExperimentListResponseSchema = z.object({
  schema_version: z.literal("v1"),
  experiments: z.array(ReviewedExperimentDefinitionSchema),
}).strict();

export type ReviewedExperimentCreateRequest = z.infer<typeof ReviewedExperimentCreateRequestSchema>;
export type ReviewedExperimentDefinition = z.infer<typeof ReviewedExperimentDefinitionSchema>;
export type ReviewedExperimentListResponse = z.infer<typeof ReviewedExperimentListResponseSchema>;
