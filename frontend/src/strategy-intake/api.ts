import { StrategyDraftRequestSchema, StrategyDraftResponseSchema } from "./contracts";
import type { StrategyDraftResponse } from "./contracts";
import type { StrategyDraftRequest } from "./types";

export class StrategyDraftRequestError extends Error {
  constructor(readonly status: number) {
    super("STRATEGY_DRAFT_REQUEST_FAILED");
    this.name = "StrategyDraftRequestError";
  }
}

export async function createStrategyDraft(
  input: StrategyDraftRequest,
  csrfToken: string,
  fetcher: typeof fetch = fetch,
): Promise<StrategyDraftResponse> {
  if (!csrfToken.trim()) throw new StrategyDraftRequestError(403);
  let payload: StrategyDraftRequest;
  try {
    payload = StrategyDraftRequestSchema.parse(input);
  } catch {
    throw new StrategyDraftRequestError(422);
  }
  const response = await fetcher("/api/owner/strategy-drafts", {
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
  if (!response.ok) throw new StrategyDraftRequestError(response.status);
  try {
    return StrategyDraftResponseSchema.parse(await response.json());
  } catch {
    throw new StrategyDraftRequestError(503);
  }
}

export const strategyDraftClient = { create: createStrategyDraft };
export type StrategyDraftClient = typeof strategyDraftClient;
