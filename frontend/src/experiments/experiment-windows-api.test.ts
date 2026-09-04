import { describe, expect, it, vi } from "vitest";
import { experimentWindowListSchema, loadExperimentWindows } from "./experiment-windows-api";

export const closedWindowPayload = {
  schema_version: "v2" as const,
  plan_version: 2,
  protocol: {
    schema_version: "v2" as const,
    name: "SPY structural bullish beta pilot",
    summary: "Bullish direction fixed before the window; one bull call debit spread, 30–45 days to expiry, $4 wide, with defined risk.",
  },
  frozen_at: "2026-09-02T18:15:00Z",
  decision_boundary: "2026-09-03T13:50:00Z",
  entry_window: {
    schema_version: "v2" as const,
    opens_at: "2026-09-03T13:50:00Z",
    closes_at: "2026-09-03T14:25:00Z",
  },
  terminal_decision: {
    schema_version: "v2" as const,
    outcome_code: "ENTRY_APPROVED" as const,
    reason: "Every fixed entry and safety rule passed.",
    decided_at: "2026-09-03T13:52:00Z",
  },
  lifecycle: {
    schema_version: "v2" as const,
    status: "CLOSED" as const,
    opened_at: "2026-09-03T13:58:00Z",
    closed_at: "2026-09-04T13:45:00Z",
    exit_reason: "The frozen schedule required the paper position to close.",
    realized_paper_pnl: "115.25",
  },
  status: "DECIDED" as const,
  aborted_reason: null,
  tick_outcome_code: "FILLED",
  tick_outcome_text: "The paper order filled and the position was reconciled.",
  collapsed_versions: [2],
};

describe("experiment window API", () => {
  it("loads the anonymous no-store route without browser credentials", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ schema_version: "v2", windows: [closedWindowPayload] })),
    );

    const result = await loadExperimentWindows(fetcher);

    expect(result.windows[0].lifecycle?.realized_paper_pnl).toBe("115.25");
    expect(fetcher).toHaveBeenCalledWith("/api/experiments/windows", {
      method: "GET",
      credentials: "omit",
      cache: "no-store",
      headers: { Accept: "application/json", "Cache-Control": "no-store" },
    });
  });

  it("rejects private additions and invalid lifecycle claims", () => {
    expect(experimentWindowListSchema.safeParse({
      schema_version: "v2",
      windows: [{ ...closedWindowPayload, account_value: "100000" }],
    }).success).toBe(false);
    expect(experimentWindowListSchema.safeParse({
      schema_version: "v2",
      windows: [{
        ...closedWindowPayload,
        lifecycle: {
          ...closedWindowPayload.lifecycle,
          status: "OPEN",
          realized_paper_pnl: "115.25",
        },
      }],
    }).success).toBe(false);
  });
});
