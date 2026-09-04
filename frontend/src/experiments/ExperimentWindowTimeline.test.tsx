import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { copy as publicCopy } from "../content/copy";
import { ExperimentWindowTimeline } from "./ExperimentWindowTimeline";
import { experimentWindowListSchema } from "./experiment-windows-api";

const copy = publicCopy.experiment.windows;
const closedWindowPayload = {
  schema_version: "v2" as const,
  plan_version: 2,
  protocol: {
    schema_version: "v1" as const,
    name: "SPY structural bullish beta pilot",
    summary: "Bullish direction fixed before the window; one bull call debit spread, 30–45 days to expiry, $4 wide, with defined risk.",
  },
  frozen_at: "2026-09-02T18:15:00Z",
  decision_boundary: "2026-09-03T13:50:00Z",
  entry_window: {
    schema_version: "v1" as const,
    opens_at: "2026-09-03T13:50:00Z",
    closes_at: "2026-09-03T14:25:00Z",
  },
  terminal_decision: {
    schema_version: "v1" as const,
    outcome_code: "ENTRY_APPROVED" as const,
    reason: "Every fixed entry and safety rule passed.",
    decided_at: "2026-09-03T13:52:00Z",
  },
  lifecycle: {
    schema_version: "v1" as const,
    status: "CLOSED" as const,
    opened_at: "2026-09-03T13:58:00Z",
    closed_at: "2026-09-04T13:45:00Z",
    exit_reason: "The frozen schedule required the paper position to close.",
    realized_paper_pnl: "115.25",
  },
};

afterEach(cleanup);

describe("experiment window timeline", () => {
  it("renders every decision and lifecycle outcome newest first", () => {
    const windows = experimentWindowListSchema.parse({
      schema_version: "v1",
      windows: [
        closedWindowPayload,
        {
          ...closedWindowPayload,
          plan_version: 1,
          frozen_at: "2026-09-01T18:15:00Z",
          terminal_decision: {
            schema_version: "v1",
            outcome_code: "PROVIDER_FAILURE_NO_TRADE",
            reason: "A required data source failed, so no trade was allowed.",
            decided_at: "2026-09-02T13:52:00Z",
          },
          lifecycle: null,
        },
      ],
    });

    render(<ExperimentWindowTimeline windows={windows} />);

    const timeline = screen.getByRole("list", { name: copy.timelineLabel });
    const cards = within(timeline).getAllByRole("article");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent(`${copy.window} 2`);
    expect(cards[0]).toHaveTextContent(copy.entryApproved);
    expect(cards[0]).toHaveTextContent("+$115.25");
    expect(cards[0]).toHaveTextContent("The frozen schedule required the paper position to close.");
    expect(cards[1]).toHaveTextContent(`${copy.window} 1`);
    expect(cards[1]).toHaveTextContent(copy.providerFailure);
    expect(cards[1]).toHaveTextContent("A required data source failed");
    expect(cards[1]).toHaveTextContent(copy.noPosition);
  });

  it("renders no-trade and open windows without inventing realized P&L", () => {
    const windows = experimentWindowListSchema.parse({
      schema_version: "v1",
      windows: [
        {
          ...closedWindowPayload,
          lifecycle: {
            ...closedWindowPayload.lifecycle,
            status: "OPEN",
            closed_at: null,
            exit_reason: null,
            realized_paper_pnl: null,
          },
        },
        {
          ...closedWindowPayload,
          plan_version: 1,
          frozen_at: "2026-09-01T18:15:00Z",
          terminal_decision: {
            schema_version: "v1",
            outcome_code: "NO_TRADE",
            reason: "The option quote was too old.",
            decided_at: "2026-09-02T13:52:00Z",
          },
          lifecycle: null,
        },
      ],
    });

    render(<ExperimentWindowTimeline windows={windows} />);

    expect(screen.getByText(copy.openDetail)).toBeVisible();
    expect(screen.getByText("The option quote was too old.")).toBeVisible();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("collapses an aborted burst and shows the linked execution outcome", () => {
    const windows = experimentWindowListSchema.parse({
      schema_version: "v2",
      windows: [
        {
          ...closedWindowPayload,
          schema_version: "v2",
          collapsed_versions: [6, 7, 8],
          status: "ABORTED",
          aborted_reason: "runtime never started",
          terminal_decision: null,
          lifecycle: null,
          tick_outcome_code: null,
          tick_outcome_text: null,
        },
        {
          ...closedWindowPayload,
          schema_version: "v2",
          plan_version: 1,
          protocol: { ...closedWindowPayload.protocol, schema_version: "v2", name: "SPY structural bearish OTM pilot" },
          tick_outcome_code: "EXECUTION_BLOCKED",
          tick_outcome_text: "Entry approved, then execution was blocked before the order was sent.",
          collapsed_versions: [1],
        },
      ],
    });

    render(<ExperimentWindowTimeline windows={windows} />);

    expect(screen.getByText(`${copy.versions} 6–8`)).toBeVisible();
    expect(screen.getByText(copy.abortedReason)).toBeVisible();
    expect(screen.getByText("Entry approved, then execution was blocked before the order was sent.")).toBeVisible();
  });

  it("distinguishes loading, unavailable, and empty states", () => {
    const { rerender } = render(<ExperimentWindowTimeline windows={undefined} />);
    expect(screen.getByText(copy.loading)).toBeVisible();

    rerender(<ExperimentWindowTimeline windows={null} />);
    expect(screen.getByText(copy.unavailable)).toBeVisible();

    rerender(<ExperimentWindowTimeline windows={{ schema_version: "v1", windows: [] }} />);
    expect(screen.getByText(copy.empty)).toBeVisible();
  });
});
