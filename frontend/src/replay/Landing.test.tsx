import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CompetitionPerformanceProofResponse } from "../contracts/v1";
import type { CompetitionRecordResponse } from "../competition-record/api";
import { copy } from "../content/copy";
import type { ExperimentWindowList } from "../experiments";
import { Landing } from "./Landing";

const archive: CompetitionRecordResponse = { schema_version: "v1", publication_status: "NOT_PUBLISHED", records: [] };
const proof: CompetitionPerformanceProofResponse = {
  schema_version: "v1", publication_status: "PUBLISHED", baseline_status: "BASELINE_CLEAN",
  published_at: "2026-09-02T14:00:00Z", linked_certificate_ids: [], publication_hash: "a".repeat(64), predecessor_hash: null,
  point: {
    schema_version: "v1", scheduled_for: "2026-09-02T13:00:00Z", attempted_at: "2026-09-02T13:01:00Z",
    measured_at: "2026-09-02T13:02:00Z", status: "COMPLETE", failure_code: null, current_equity_usd: "100000",
    account_equity_change_usd: "0", account_equity_return_pct: "0", reconciled_lifecycle_cashflow_usd: "0",
    open_position_liquidation_pnl_usd: "0", broker_write_count: 0, simulator_limitations_code: "ALPACA_PAPER_SIMULATION",
  },
};

function windows(status: ExperimentWindowList["windows"][number]["status"] = "DECIDED"): ExperimentWindowList {
  return {
    schema_version: "v2",
    windows: [{
      schema_version: "v2", plan_version: 1,
      protocol: { schema_version: "v2", name: "SPY morning options test", summary: "A bounded options window." },
      frozen_at: "2026-09-02T13:00:00Z", decision_boundary: "2026-09-02T14:00:00Z",
      entry_window: { schema_version: "v2", opens_at: "2026-09-02T14:00:00Z", closes_at: "2026-09-02T14:30:00Z" },
      terminal_decision: status === "DECIDED" ? { schema_version: "v2", outcome_code: "NO_TRADE", reason: "Quote was stale.", decided_at: "2026-09-02T14:01:00Z" } : null,
      lifecycle: null, status, aborted_reason: status === "ABORTED" ? "Runtime did not start." : null,
      tick_outcome_code: "NO_TRADE", tick_outcome_text: "Tick recorded no trade.", collapsed_versions: [1],
    }],
  };
}

describe("signed-out landing", () => {
  afterEach(cleanup);

  it("puts the decided competition record and 60-second path first", () => {
    render(<Landing archive={archive} proof={proof} windows={windows()} onOpenReplay={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "SPY morning options test" })).toBeVisible();
    expect(screen.getByText(copy.productShell.landing.recordNoTrade)).toBeVisible();
    expect(screen.getByText("Tick recorded no trade.")).toBeVisible();
    expect(screen.getByText(copy.productShell.landing.brokerWrites).parentElement).toHaveTextContent("0");
    expect(screen.getByRole("heading", { name: copy.productShell.landing.pathTitle })).toBeVisible();
  });

  it("explains pending, aborted, and unavailable states without inventing a result", () => {
    const { rerender } = render(<Landing archive={archive} proof={proof} windows={windows("PENDING")} onOpenReplay={vi.fn()} />);
    expect(screen.getByText(copy.productShell.landing.recordPendingDetail)).toBeVisible();
    rerender(<Landing archive={archive} proof={proof} windows={windows("ABORTED")} onOpenReplay={vi.fn()} />);
    expect(screen.getByText(copy.productShell.landing.recordAborted)).toBeVisible();
    rerender(<Landing archive={null} proof={null} windows={null} onOpenReplay={vi.fn()} />);
    expect(screen.getByText(copy.productShell.landing.recordUnavailable)).toBeVisible();
  });

  it("shows each Alpaca proof row with its artifact link", () => {
    render(<Landing archive={archive} proof={proof} windows={windows()} onOpenReplay={vi.fn()} />);
    expect(screen.getByText(copy.productShell.landing.tradingApi)).toBeVisible();
    expect(screen.getByText(copy.productShell.landing.mcpServer)).toBeVisible();
    expect(screen.getByText(copy.productShell.landing.cli)).toBeVisible();
    expect(screen.getAllByRole("link", { name: copy.productShell.landing.openArtifact })).toHaveLength(3);
  });
});
