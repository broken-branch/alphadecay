import { afterEach, describe, expect, it, vi } from "vitest";
import { createStrategyDraft, StrategyDraftRequestError } from "./api";
import type { StrategyDraftResponse } from "./contracts";
import type { StrategyDraftRequest } from "./types";

const request: StrategyDraftRequest = {
  source: {
    kind: "PASTED_TEXT",
    content: "SPY may rise as earnings participation improves across sectors.",
  },
  market_scope: "SPY",
  direction: "BULLISH",
  horizon: "A few weeks",
  evidence: ["Earnings revisions broaden for several weeks."],
  invalidation: ["SPY closes below its 50-day moving average."],
  risk_budget: { max_loss_dollars: "225.00" },
};

const response: StrategyDraftResponse = {
  schema_version: "v1",
  status: "DRAFT_REVIEW_REQUIRED",
  curation_status: "NOT_CURATED",
  automation_state: "OFF",
  execution_eligible: false,
  intake: request,
  assumptions: ["USER_BRIEF_UNVERIFIED", "OPTIONS_ONLY", "PAPER_ONLY", "DEFINED_RISK_ONLY"],
  questions: [],
  required_before_promotion: [
    "MODEL_CURATION_REQUIRED",
    "EVIDENCE_REVIEW_REQUIRED",
    "RISK_REVIEW_REQUIRED",
    "OWNER_REVIEW_REQUIRED",
  ],
  structure_constraints: {
    options_required: true,
    defined_risk_required: true,
    naked_short_options_allowed: false,
    direction: "BULLISH",
    candidate_families: ["BULL_CALL_DEBIT_SPREAD"],
  },
  evidence_plan: {
    submitted_evidence: request.evidence,
    required_checks: [
      "VERIFY_THESIS_CLAIMS",
      "CHECK_MARKET_DATA_RECENCY",
      "CHECK_OPTION_LIQUIDITY",
      "CHECK_INVALIDATION_STATE",
    ],
  },
  risk_rules: {
    budget: request.risk_budget,
    loss_must_be_bounded: true,
    size_must_fit_budget: true,
  },
  exit_rules: {
    invalidation: request.invalidation,
    required_before_promotion: ["PROFIT_EXIT_REQUIRED", "LOSS_EXIT_REQUIRED", "TIME_EXIT_REQUIRED"],
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.restoreAllMocks());

describe("strategy draft API", () => {
  it("posts only the reviewed brief with owner CSRF and no-store headers", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(response));

    await expect(createStrategyDraft(request, "csrf-token", fetcher as typeof fetch)).resolves.toEqual(response);

    expect(fetcher).toHaveBeenCalledOnce();
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe("/api/owner/strategy-drafts");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        Origin: window.location.origin,
        "X-CSRF-Token": "csrf-token",
      },
    });
    expect(JSON.parse(String(init?.body))).toEqual(request);
    expect(String(url)).not.toMatch(/order|arm|autonomy|execute/i);
    expect(String(init?.body)).not.toMatch(/order|arm|autonomy|execute/i);
  });

  it("fails before fetch when owner CSRF is missing", async () => {
    const fetcher = vi.fn();

    await expect(createStrategyDraft(request, "", fetcher as typeof fetch)).rejects.toEqual(
      expect.objectContaining({ status: 403 }),
    );
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("preserves authentication errors without exposing a server body", async () => {
    const fetcher = vi.fn(async () => jsonResponse({ detail: "OWNER_REQUEST_REJECTED" }, 403));

    await expect(createStrategyDraft(request, "csrf-token", fetcher as typeof fetch)).rejects.toEqual(
      expect.objectContaining({ name: "StrategyDraftRequestError", status: 403 }),
    );
  });

  it("rejects malformed input before fetch and an invalid success response after fetch", async () => {
    const inputFetcher = vi.fn();
    await expect(createStrategyDraft(
      { ...request, source: { kind: "PASTED_TEXT", content: "too short" } },
      "csrf-token",
      inputFetcher as typeof fetch,
    )).rejects.toEqual(expect.objectContaining({ status: 422 }));
    expect(inputFetcher).not.toHaveBeenCalled();

    const responseFetcher = vi.fn(async () => jsonResponse({ ...response, automation_state: "ON" }));
    await expect(createStrategyDraft(request, "csrf-token", responseFetcher as typeof fetch)).rejects.toEqual(
      expect.objectContaining({ status: 503 }),
    );
  });

  it("uses one generic client error without retaining response prose", () => {
    expect(new StrategyDraftRequestError(503)).toMatchObject({
      name: "StrategyDraftRequestError",
      message: "STRATEGY_DRAFT_REQUEST_FAILED",
      status: 503,
    });
  });
});
