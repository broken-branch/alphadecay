import { z } from "zod";

const hash = z.string().regex(/^[0-9a-f]{64}$/);

export const ExperimentAuthorizationRequestSchema = z.object({
  schema_version: z.literal("v1").default("v1"),
  source_definition_hash: hash,
  protocol_hash: hash,
  expected_revision: z.number().int().min(0),
}).strict();

export const ExperimentAuthorizationStatusSchema = z.object({
  schema_version: z.literal("v1"),
  experiment_id: z.string().uuid(),
  source_definition_hash: hash,
  protocol_hash: hash,
  authorization_revision: z.number().int().min(0),
  authorization_state: z.enum(["NOT_ARMED", "ARMED", "DISARMED"]),
  entry_authorized: z.boolean(),
  existing_position_risk_management_preserved: z.literal(true),
  runtime_state: z.literal("NOT_CONNECTED"),
  execution_eligible: z.literal(false),
  paper_trading_only: z.literal(true),
  authorization_event_hash: hash.nullable().default(null),
  updated_at: z.string().datetime({ offset: true }).nullable().default(null),
}).strict().superRefine((status, context) => {
  if (status.entry_authorized !== (status.authorization_state === "ARMED")) {
    context.addIssue({ code: "custom", message: "Authorization state mismatch" });
  }
  if (status.authorization_revision === 0) {
    if (
      status.authorization_state !== "NOT_ARMED"
      || status.authorization_event_hash !== null
      || status.updated_at !== null
    ) {
      context.addIssue({ code: "custom", message: "Invalid initial authorization" });
    }
  } else if (status.authorization_event_hash === null || status.updated_at === null) {
    context.addIssue({ code: "custom", message: "Missing authorization event" });
  }
});

export type ExperimentAuthorizationRequest = z.infer<typeof ExperimentAuthorizationRequestSchema>;
export type ExperimentAuthorizationStatus = z.infer<typeof ExperimentAuthorizationStatusSchema>;
