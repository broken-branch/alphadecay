import { z } from "zod";
import {
  ExperimentPerformanceProjectionSchema,
  type ExperimentPerformanceProjection,
} from "./experiment-performance-contracts";

const experimentIdSchema = z.string().uuid();

export class ExperimentPerformanceRequestError extends Error {
  constructor(readonly status: number) {
    super("EXPERIMENT_PERFORMANCE_REQUEST_FAILED");
    this.name = "ExperimentPerformanceRequestError";
  }
}

function requestHeaders(csrfToken?: string): HeadersInit {
  return {
    Accept: "application/json",
    "Cache-Control": "no-store",
    ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
  };
}

async function readProjection(
  path: string,
  experimentId: string,
  fetcher: typeof fetch,
  csrfToken?: string,
): Promise<ExperimentPerformanceProjection | null> {
  const parsedId = experimentIdSchema.safeParse(experimentId);
  if (!parsedId.success) throw new ExperimentPerformanceRequestError(422);
  const response = await fetcher(path.replace("{experiment_id}", parsedId.data), {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: requestHeaders(csrfToken),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new ExperimentPerformanceRequestError(response.status);
  const result = ExperimentPerformanceProjectionSchema.safeParse(await response.json());
  if (!result.success || result.data.lineage.experiment_id !== parsedId.data) {
    throw new ExperimentPerformanceRequestError(503);
  }
  return result.data;
}

export function readOwnerExperimentPerformance(
  experimentId: string,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<ExperimentPerformanceProjection | null> {
  if (!csrfToken.trim()) throw new ExperimentPerformanceRequestError(403);
  return readProjection(
    "/api/owner/experiments/{experiment_id}/performance",
    experimentId,
    fetcher,
    csrfToken,
  );
}

export function readPublishedExperimentPerformance(
  experimentId: string,
  fetcher: typeof fetch = fetch,
): Promise<ExperimentPerformanceProjection | null> {
  return readProjection(
    "/api/experiments/{experiment_id}/performance",
    experimentId,
    fetcher,
  );
}

export type ExperimentPerformanceClient = {
  readOwner: typeof readOwnerExperimentPerformance;
  readPublished: typeof readPublishedExperimentPerformance;
};

export const experimentPerformanceClient: ExperimentPerformanceClient = {
  readOwner: readOwnerExperimentPerformance,
  readPublished: readPublishedExperimentPerformance,
};
