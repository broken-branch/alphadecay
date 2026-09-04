import {
  StrategyCurationRequestSchema,
  StrategyCurationResponseSchema,
} from "./contracts";
import type {
  StrategyCurationRequest,
  StrategyCurationResponse,
} from "./contracts";

export class StrategyCurationRequestError extends Error {
  constructor(readonly status: number) {
    super("STRATEGY_CURATION_REQUEST_FAILED");
    this.name = "StrategyCurationRequestError";
  }
}

export async function createStrategyCuration(
  input: StrategyCurationRequest,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<StrategyCurationResponse> {
  if (!csrfToken.trim()) throw new StrategyCurationRequestError(403);
  let payload: StrategyCurationRequest;
  try {
    payload = StrategyCurationRequestSchema.parse(input);
  } catch {
    throw new StrategyCurationRequestError(422);
  }
  const response = await fetcher("/api/owner/strategy-curations", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Cache-Control": "no-store",
      "Content-Type": "application/json",
      Origin: window.location.origin,
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new StrategyCurationRequestError(response.status);
  try {
    return StrategyCurationResponseSchema.parse(await response.json());
  } catch {
    throw new StrategyCurationRequestError(503);
  }
}

export const strategyCurationClient = { create: createStrategyCuration };
export type StrategyCurationClient = typeof strategyCurationClient;
