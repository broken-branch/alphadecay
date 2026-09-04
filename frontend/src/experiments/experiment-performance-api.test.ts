import { describe, expect, it, vi } from "vitest";
import {
  ExperimentPerformanceRequestError,
  readOwnerExperimentPerformance,
  readPublishedExperimentPerformance,
} from "./experiment-performance-api";

const experimentId = "00000000-0000-4000-8000-000000000001";

function projection() {
  return {
    lineage: {
      experiment_id: experimentId,
      source_definition_hash: "1".repeat(64),
      protocol_hash: "2".repeat(64),
    },
    decision_count: 1,
    opened_trade_count: 0,
    closed_trade_count: 0,
    terminal_state: "NO_POSITION",
    total_defined_maximum_risk_at_entry: { value: null, unavailable_reason: "NO_OPENED_TRADES" },
    entry_cash_flow: { value: null, unavailable_reason: "NO_OPENED_TRADES" },
    management_cash_flow: { value: null, unavailable_reason: "NO_OPENED_TRADES" },
    exit_cash_flow: { value: null, unavailable_reason: "NO_CLOSED_TRADES" },
    realized_strategy_pnl: { value: null, unavailable_reason: "NO_CLOSED_TRADES" },
    win_count: { value: null, unavailable_reason: "NO_CLOSED_TRADES" },
    loss_count: { value: null, unavailable_reason: "NO_CLOSED_TRADES" },
    breakeven_count: { value: null, unavailable_reason: "NO_CLOSED_TRADES" },
  };
}

describe("experiment performance client", () => {
  it("reads owner and published projections with same-origin no-store requests", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(projection())));

    await expect(readOwnerExperimentPerformance(
      experimentId,
      "csrf-token",
      fetcher,
    )).resolves.toMatchObject({ decision_count: 1 });
    await expect(readPublishedExperimentPerformance(
      experimentId,
      fetcher,
    )).resolves.toMatchObject({ terminal_state: "NO_POSITION" });

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      `/api/owner/experiments/${experimentId}/performance`,
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      }),
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      `/api/experiments/${experimentId}/performance`,
      expect.objectContaining({ method: "GET", credentials: "same-origin", cache: "no-store" }),
    );
    expect(fetcher.mock.calls[1][1]?.headers).not.toHaveProperty("X-CSRF-Token");
  });

  it("returns an honest empty result for an unpublished projection", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => new Response(null, { status: 404 }));

    await expect(readPublishedExperimentPerformance(
      experimentId,
      fetcher,
    )).resolves.toBeNull();
  });

  it("raises a typed error for request and contract failures", async () => {
    const unavailable = vi.fn<typeof fetch>(async () => new Response(null, { status: 503 }));
    const malformed = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({ decision_count: 1 })));

    await expect(readOwnerExperimentPerformance(
      experimentId,
      "csrf-token",
      unavailable,
    )).rejects.toEqual(expect.objectContaining({ status: 503 }));
    await expect(readPublishedExperimentPerformance(
      experimentId,
      malformed,
    )).rejects.toBeInstanceOf(ExperimentPerformanceRequestError);
  });
});
