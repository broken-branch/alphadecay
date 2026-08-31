import { z } from "zod";

export const OwnerModelProviderSchema = z.enum(["GEMINI", "OPENAI_COMPATIBLE"]);
export type OwnerModelProvider = z.infer<typeof OwnerModelProviderSchema>;

export const SessionCreateRequestSchema = z
  .object({
    schema_version: z.literal("v1").default("v1"),
    access_code: z.string().min(16).max(256),
  })
  .strict();

export const SessionResponseSchema = z
  .object({
    schema_version: z.literal("v1"),
    authenticated: z.boolean(),
    expires_at: z.string().datetime().nullable().optional(),
  })
  .strict();

export const ProviderSettingsResponseSchema = z
  .object({
    schema_version: z.literal("v1"),
    configured: z.boolean(),
    provider: OwnerModelProviderSchema.nullable().optional(),
    endpoint: z.string().nullable().optional(),
    model: z.string().nullable().optional(),
    generation: z.number().int().min(1).nullable().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    const complete =
      value.provider != null &&
      value.endpoint != null &&
      value.model != null &&
      value.generation != null;
    if (value.configured !== complete) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Inconsistent provider status" });
    }
  });
export type ProviderSettingsResponse = z.infer<typeof ProviderSettingsResponseSchema>;

export const ProviderSettingsUpdateRequestSchema = z
  .object({
    schema_version: z.literal("v1").default("v1"),
    provider: OwnerModelProviderSchema,
    model: z.string().trim().min(1).max(256),
    api_key: z.string().min(1).max(16_384),
    endpoint: z.string().trim().min(1).max(2048).nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.provider === "GEMINI" && value.endpoint !== null) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["endpoint"], message: "Unexpected endpoint" });
    }
    if (value.provider === "OPENAI_COMPATIBLE" && value.endpoint === null) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["endpoint"], message: "Endpoint required" });
    }
  });
export type ProviderSettingsUpdateRequest = z.infer<typeof ProviderSettingsUpdateRequestSchema>;
