import { z } from "zod";
import {
  ReviewedExperimentCreateRequestSchema,
  ReviewedExperimentDefinitionSchema,
  ReviewedExperimentListResponseSchema,
} from "./reviewed-registry-contracts";
import type {
  ReviewedExperimentCreateRequest,
  ReviewedExperimentDefinition,
  ReviewedExperimentListResponse,
} from "./reviewed-registry-contracts";
import {
  CompiledExperimentVersionSchema,
  CompileExperimentRequestSchema,
} from "./compiled-experiment-contracts";
import type {
  CompiledExperimentVersion,
  CompileExperimentRequest,
} from "./compiled-experiment-contracts";
import {
  ExperimentAuthorizationRequestSchema,
  ExperimentAuthorizationStatusSchema,
} from "./experiment-authorization-contracts";
import type {
  ExperimentAuthorizationRequest,
  ExperimentAuthorizationStatus,
} from "./experiment-authorization-contracts";

const experimentIdSchema = z.string().uuid();
const hashSchema = z.string().regex(/^[0-9a-f]{64}$/);

export class ReviewedExperimentRequestError extends Error {
  constructor(readonly status: number) {
    super("REVIEWED_EXPERIMENT_REQUEST_FAILED");
    this.name = "ReviewedExperimentRequestError";
  }
}

function headers(csrfToken: string, includeBody = false): HeadersInit {
  return {
    Accept: "application/json",
    "Cache-Control": "no-store",
    ...(includeBody ? { "Content-Type": "application/json", Origin: window.location.origin } : {}),
    "X-CSRF-Token": csrfToken,
  };
}

export async function createReviewedExperiment(
  input: ReviewedExperimentCreateRequest,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<ReviewedExperimentDefinition> {
  if (!csrfToken.trim()) throw new ReviewedExperimentRequestError(403);
  const parsed = ReviewedExperimentCreateRequestSchema.safeParse(input);
  if (!parsed.success) throw new ReviewedExperimentRequestError(422);
  const response = await fetcher("/api/owner/experiments", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: headers(csrfToken, true),
    body: JSON.stringify(parsed.data),
  });
  if (!response.ok) throw new ReviewedExperimentRequestError(response.status);
  const result = ReviewedExperimentDefinitionSchema.safeParse(await response.json());
  if (!result.success) throw new ReviewedExperimentRequestError(503);
  return result.data;
}

export async function listReviewedExperiments(
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<ReviewedExperimentListResponse> {
  if (!csrfToken.trim()) throw new ReviewedExperimentRequestError(403);
  const response = await fetcher("/api/owner/experiments", {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: headers(csrfToken),
  });
  if (!response.ok) throw new ReviewedExperimentRequestError(response.status);
  const result = ReviewedExperimentListResponseSchema.safeParse(await response.json());
  if (!result.success) throw new ReviewedExperimentRequestError(503);
  return result.data;
}

export async function compileReviewedExperiment(
  experimentId: string,
  input: CompileExperimentRequest,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<CompiledExperimentVersion> {
  if (!csrfToken.trim()) throw new ReviewedExperimentRequestError(403);
  const parsedId = experimentIdSchema.safeParse(experimentId);
  const parsed = CompileExperimentRequestSchema.safeParse(input);
  if (!parsedId.success || !parsed.success) throw new ReviewedExperimentRequestError(422);
  const response = await fetcher(`/api/owner/experiments/${parsedId.data}/compile`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: headers(csrfToken, true),
    body: JSON.stringify(parsed.data),
  });
  if (!response.ok) throw new ReviewedExperimentRequestError(response.status);
  const result = CompiledExperimentVersionSchema.safeParse(await response.json());
  if (!result.success) throw new ReviewedExperimentRequestError(503);
  if (
    result.data.experiment_id !== parsedId.data
    || result.data.source_definition_hash !== parsed.data.source_definition_hash
  ) throw new ReviewedExperimentRequestError(503);
  return result.data;
}

export async function readCompiledExperiment(
  experimentId: string,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<CompiledExperimentVersion | null> {
  if (!csrfToken.trim()) throw new ReviewedExperimentRequestError(403);
  const parsedId = experimentIdSchema.safeParse(experimentId);
  if (!parsedId.success) throw new ReviewedExperimentRequestError(422);
  const response = await fetcher(`/api/owner/experiments/${parsedId.data}/compiled`, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: headers(csrfToken),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new ReviewedExperimentRequestError(response.status);
  const result = CompiledExperimentVersionSchema.safeParse(await response.json());
  if (!result.success) throw new ReviewedExperimentRequestError(503);
  if (result.data.experiment_id !== parsedId.data) throw new ReviewedExperimentRequestError(503);
  return result.data;
}

function validateAuthorizationIdentity(
  status: ExperimentAuthorizationStatus,
  experimentId: string,
  sourceDefinitionHash: string,
  protocolHash: string,
): ExperimentAuthorizationStatus {
  if (
    status.experiment_id !== experimentId
    || status.source_definition_hash !== sourceDefinitionHash
    || status.protocol_hash !== protocolHash
  ) throw new ReviewedExperimentRequestError(503);
  return status;
}

export async function readExperimentAuthorization(
  experimentId: string,
  sourceDefinitionHash: string,
  protocolHash: string,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<ExperimentAuthorizationStatus> {
  if (!csrfToken.trim()) throw new ReviewedExperimentRequestError(403);
  const parsedId = experimentIdSchema.safeParse(experimentId);
  const parsedSourceHash = hashSchema.safeParse(sourceDefinitionHash);
  const parsedProtocolHash = hashSchema.safeParse(protocolHash);
  if (!parsedId.success || !parsedSourceHash.success || !parsedProtocolHash.success) {
    throw new ReviewedExperimentRequestError(422);
  }
  const response = await fetcher(`/api/owner/experiments/${parsedId.data}/authorization`, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: headers(csrfToken),
  });
  if (!response.ok) throw new ReviewedExperimentRequestError(response.status);
  const result = ExperimentAuthorizationStatusSchema.safeParse(await response.json());
  if (!result.success) throw new ReviewedExperimentRequestError(503);
  return validateAuthorizationIdentity(
    result.data,
    parsedId.data,
    parsedSourceHash.data,
    parsedProtocolHash.data,
  );
}

async function changeExperimentAuthorization(
  operation: "arm" | "disarm",
  experimentId: string,
  input: ExperimentAuthorizationRequest,
  csrfToken: string,
  fetcher: typeof fetch,
): Promise<ExperimentAuthorizationStatus> {
  if (!csrfToken.trim()) throw new ReviewedExperimentRequestError(403);
  const parsedId = experimentIdSchema.safeParse(experimentId);
  const parsed = ExperimentAuthorizationRequestSchema.safeParse(input);
  if (!parsedId.success || !parsed.success) throw new ReviewedExperimentRequestError(422);
  const response = await fetcher(`/api/owner/experiments/${parsedId.data}/${operation}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: headers(csrfToken, true),
    body: JSON.stringify(parsed.data),
  });
  if (!response.ok) throw new ReviewedExperimentRequestError(response.status);
  const result = ExperimentAuthorizationStatusSchema.safeParse(await response.json());
  if (!result.success) throw new ReviewedExperimentRequestError(503);
  const exactStatus = validateAuthorizationIdentity(
    result.data,
    parsedId.data,
    parsed.data.source_definition_hash,
    parsed.data.protocol_hash,
  );
  const expectedState = operation === "arm" ? "ARMED" : "DISARMED";
  if (
    exactStatus.authorization_state !== expectedState
    || exactStatus.authorization_revision !== parsed.data.expected_revision + 1
  ) {
    throw new ReviewedExperimentRequestError(503);
  }
  return exactStatus;
}

export function armExperiment(
  experimentId: string,
  input: ExperimentAuthorizationRequest,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<ExperimentAuthorizationStatus> {
  return changeExperimentAuthorization("arm", experimentId, input, csrfToken, fetcher);
}

export function disarmExperiment(
  experimentId: string,
  input: ExperimentAuthorizationRequest,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<ExperimentAuthorizationStatus> {
  return changeExperimentAuthorization("disarm", experimentId, input, csrfToken, fetcher);
}

export const reviewedExperimentClient = {
  create: createReviewedExperiment,
  list: listReviewedExperiments,
  compile: compileReviewedExperiment,
  readCompiled: readCompiledExperiment,
  readAuthorization: readExperimentAuthorization,
  arm: armExperiment,
  disarm: disarmExperiment,
};
export type ReviewedExperimentClient = typeof reviewedExperimentClient;
