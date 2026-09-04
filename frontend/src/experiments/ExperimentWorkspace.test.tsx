import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CompetitionPerformanceProofResponse } from "../contracts/v1";
import type { PositionRecord } from "../competition-record/api";
import { copy as publicCopy } from "../content/copy";
import { ExperimentPerformanceProjectionSchema } from "./experiment-performance-contracts";
import type { ExperimentPerformanceClient } from "./experiment-performance-api";
import { ExperimentWorkspace } from "./ExperimentWorkspace";
import type { ExperimentDefinition } from "./types";

const copy = publicCopy.experiment;

const definition: ExperimentDefinition = {
  id: "acme-event-continuation-v1",
  name: "ACME event continuation",
  underlying: "ACME",
  thesis: "A confirmed move can continue while the spread keeps the loss bounded.",
  whyChosen: ["The signal was fixed before the session.", "The spread has a defined loss."],
  invalidation: ["The event is contradicted.", "The quote is too old to use."],
  status: "CLOSED",
  source: "PAPER",
  structure: "130 / 135 call vertical",
  maximumRiskUsd: "400.00",
  policyVersion: "event-continuation-v1",
};

const position: PositionRecord = {
  schema_version: "v1",
  kind: "POSITION",
  public_record_id: "a".repeat(64),
  occurred_at: "2026-08-31T14:35:00Z",
  published_at: "2026-09-01T15:02:00Z",
  projection_hash: "b".repeat(64),
  publication_hash: "c".repeat(64),
  predecessor_hash: null,
  payload: {
    schema_version: "v1",
    record_kind: "POSITION",
    public_record_id: "a".repeat(64),
    state: "CLOSED",
    underlying: "ACME",
    opening_spread: {
      structure: "VERTICAL",
      underlying: "ACME",
      option_type: "CALL",
      expiration: "2026-09-18",
      long_strike: "130",
      short_strike: "135",
      quantity: 1,
    },
    current_spread: null,
    opened_at: "2026-08-31T14:35:00Z",
    as_of: "2026-09-01T15:00:00Z",
    closed_at: "2026-09-01T15:00:00Z",
    thesis: {
      direction: "BULLISH",
      volatility_view: "NEUTRAL",
      target_at: "2026-09-04T15:00:00Z",
    },
    events: [
      {
        event_kind: "EXECUTION",
        action: "ENTRY",
        occurred_at: "2026-08-31T14:35:00Z",
        reason_category: "POSITION_OPENED",
        cashflow_usd: "-400.00",
        execution_status: "FILLED",
        resulting_state: "OPEN",
        spread_after: {
          structure: "VERTICAL",
          underlying: "ACME",
          option_type: "CALL",
          expiration: "2026-09-18",
          long_strike: "130",
          short_strike: "135",
          quantity: 1,
        },
      },
      {
        event_kind: "ASSESSMENT",
        action: "HOLD",
        occurred_at: "2026-08-31T16:00:00Z",
        reason_category: "POSITION_REVIEWED",
      },
      {
        event_kind: "EXECUTION",
        action: "CLOSE",
        occurred_at: "2026-09-01T15:00:00Z",
        reason_category: "POSITION_CLOSED",
        cashflow_usd: "600.00",
        execution_status: "FILLED",
        resulting_state: "CLOSED",
        spread_after: null,
      },
    ],
    current_exposure: null,
    execution_status: "FILLED",
    paper_trading: true,
  },
};

const proof: CompetitionPerformanceProofResponse = {
  schema_version: "v1",
  publication_status: "PUBLISHED",
  baseline_status: "BASELINE_CLEAN",
  published_at: "2026-09-01T15:05:00Z",
  linked_certificate_ids: [],
  publication_hash: "d".repeat(64),
  predecessor_hash: null,
  point: {
    schema_version: "v1",
    scheduled_for: "2026-09-01T15:00:00Z",
    attempted_at: "2026-09-01T15:00:01Z",
    measured_at: "2026-09-01T15:00:02Z",
    status: "COMPLETE",
    failure_code: null,
    current_equity_usd: "100200.00",
    account_equity_change_usd: "200.00",
    account_equity_return_pct: "0.200000000",
    reconciled_lifecycle_cashflow_usd: "200.00",
    open_position_liquidation_pnl_usd: "0.00",
    simulator_limitations_code: "ALPACA_PAPER_SIMULATION",
  },
};

const certifiedPerformance = ExperimentPerformanceProjectionSchema.parse({
  lineage: {
    experiment_id: "00000000-0000-4000-8000-000000000001",
    source_definition_hash: "1".repeat(64),
    protocol_hash: "2".repeat(64),
  },
  decision_count: 3,
  opened_trade_count: 1,
  closed_trade_count: 1,
  terminal_state: "CLOSED",
  total_defined_maximum_risk_at_entry: { value: "500", unavailable_reason: null },
  entry_cash_flow: { value: "-100", unavailable_reason: null },
  management_cash_flow: { value: "20", unavailable_reason: null },
  exit_cash_flow: { value: "130", unavailable_reason: null },
  realized_strategy_pnl: { value: "50", unavailable_reason: null },
  win_count: { value: 1, unavailable_reason: null },
  loss_count: { value: 0, unavailable_reason: null },
  breakeven_count: { value: 0, unavailable_reason: null },
});

afterEach(cleanup);

describe("experiment workspace", () => {
  it("shows a quiet loading state while the selected experiment projection is read", () => {
    const pending = new Promise<never>(() => undefined);
    const client = {
      readOwner: vi.fn(() => pending),
      readPublished: vi.fn(() => pending),
    } as unknown as ExperimentPerformanceClient;

    render(
      <ExperimentWorkspace
        definition={{ ...definition, id: certifiedPerformance.lineage.experiment_id }}
        performanceConnection={{ authenticated: false, csrfToken: null, client }}
      />,
    );

    expect(screen.getAllByText(copy.performance.projectionLoading)).not.toHaveLength(0);
    expect(client.readPublished).toHaveBeenCalledWith(certifiedPerformance.lineage.experiment_id);
    expect(client.readOwner).not.toHaveBeenCalled();
    expect(screen.queryByText(copy.performance.paperPnl)).not.toBeInTheDocument();
  });

  it("loads the owner projection and renders its decision spine and certified fields", async () => {
    const client = {
      readOwner: vi.fn(async () => certifiedPerformance),
      readPublished: vi.fn(),
    } as unknown as ExperimentPerformanceClient;

    render(
      <ExperimentWorkspace
        definition={{ ...definition, id: certifiedPerformance.lineage.experiment_id }}
        performanceConnection={{ authenticated: true, csrfToken: "csrf-token", client }}
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByText(copy.performance.projectionCertified)).not.toHaveLength(0);
    });
    expect(client.readOwner).toHaveBeenCalledWith(
      certifiedPerformance.lineage.experiment_id,
      "csrf-token",
    );
    expect(client.readPublished).not.toHaveBeenCalled();
    const performance = screen.getByRole("region", { name: copy.workspace.decisionSpine });
    expect(within(performance).getByText(copy.workspace.outcome)).toBeVisible();
    expect(within(performance).getByText(copy.performance.paperPnl)).toBeVisible();
    expect(within(performance).getByText("+$50.00")).toBeVisible();
  });

  it("shows the unavailable state after an empty or failed projection read", async () => {
    const client = {
      readOwner: vi.fn(),
      readPublished: vi.fn(async () => null),
    } as unknown as ExperimentPerformanceClient;

    render(
      <ExperimentWorkspace
        definition={{ ...definition, id: certifiedPerformance.lineage.experiment_id }}
        performanceConnection={{ authenticated: false, csrfToken: null, client }}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(copy.performance.projectionUnavailable)).toBeVisible();
    });
    expect(screen.queryByText(copy.performance.paperPnl)).not.toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows honest absent-data states without inventing a result", () => {
    render(
      <ExperimentWorkspace
        definition={{ ...definition, status: "WATCHING" }}
        proof={{
          schema_version: "v1",
          publication_status: "NOT_PUBLISHED",
          baseline_status: null,
          published_at: null,
          point: null,
          linked_certificate_ids: [],
          publication_hash: null,
          predecessor_hash: null,
        }}
      />,
    );

    expect(screen.getByRole("heading", { name: definition.name })).toBeVisible();
    expect(screen.getAllByText(copy.performance.decisionPending)).not.toHaveLength(0);
    expect(screen.getByText(copy.performance.noPath)).toBeVisible();
    expect(screen.getByText(copy.performance.noComparison)).toBeVisible();
    expect(screen.getAllByText(copy.trade.notEntered)).toHaveLength(3);
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("distinguishes no-trade and open-position outcomes from missing data", () => {
    const noTrade = render(
      <ExperimentWorkspace definition={{ ...definition, status: "REJECTED" }} />,
    );
    expect(screen.getByText(copy.performance.noTradeOutcome)).toBeVisible();
    noTrade.unmount();

    const openPosition: PositionRecord = {
      ...position,
      payload: {
        ...position.payload,
        state: "OPEN",
        current_spread: position.payload.opening_spread,
        closed_at: null,
        events: position.payload.events.slice(0, 2),
      },
    };
    render(
      <ExperimentWorkspace
        definition={{ ...definition, status: "OPEN" }}
        position={openPosition}
      />,
    );
    expect(screen.getByText(copy.performance.openPosition)).toBeVisible();
  });

  it("states what was tested, its current state, risk, outcome, and reasons on the first screen", () => {
    const { container } = render(
      <ExperimentWorkspace
        benchmark={{
          label: "ACME shares",
          returnPct: "0.11",
          path: [
            { at: "2026-08-31T14:35:00Z", value: 100000 },
            { at: "2026-09-01T15:00:00Z", value: 100110 },
          ],
        }}
        createdAt="2026-08-31T13:00:00Z"
        definition={definition}
        evidence={[{
          id: "event-confirmation",
          title: "Scheduled event confirmed",
          detail: "The published result matched the signal required by the plan.",
          state: "SUPPORTS",
          observedAt: "2026-08-31T13:30:00Z",
          sourceLabel: "Company filing",
        }]}
        payoff={{
          breakevenUsd: "134.00",
          maximumProfitUsd: "100.00",
          points: [
            { underlyingPrice: 125, pnlUsd: -400 },
            { underlyingPrice: 134, pnlUsd: 0 },
            { underlyingPrice: 140, pnlUsd: 100 },
          ],
        }}
        performanceProjection={certifiedPerformance}
        position={position}
        proof={proof}
        valuePath={[
          { at: "2026-08-31T14:35:00Z", value: 100000 },
          { at: "2026-08-31T18:00:00Z", value: 100080 },
          { at: "2026-09-01T15:00:00Z", value: 100200 },
        ]}
      />,
    );

    const workspace = screen.getByRole("article", { name: definition.name });
    expect(within(workspace).getAllByText(definition.thesis)).not.toHaveLength(0);
    expect(within(workspace).getByText(copy.workspace.status)).toBeVisible();
    expect(within(workspace).getAllByText(copy.status.closed)).not.toHaveLength(0);
    const performance = within(workspace).getByRole("region", { name: copy.workspace.decisionSpine });
    expect(within(performance).getByText(copy.workspace.frozenProtocol)).toBeVisible();
    expect(within(performance).getByText(copy.workspace.entryDecision)).toBeVisible();
    expect(within(performance).getByText(copy.workspace.lifecycle)).toBeVisible();
    expect(within(performance).getByText(copy.workspace.outcome)).toBeVisible();
    expect(within(performance).getByText(copy.performance.projectionCertified)).toBeVisible();
    expect(within(performance).getByText("$500.00")).toBeVisible();
    expect(within(performance).getByText("-$100.00")).toBeVisible();
    expect(within(performance).getByText("+$20.00")).toBeVisible();
    expect(within(performance).getByText("+$130.00")).toBeVisible();
    expect(within(performance).getByText("+$50.00")).toBeVisible();
    expect(within(performance).getAllByText(copy.performance.terminalClosed)).not.toHaveLength(0);
    expect(within(performance).getByText(copy.performance.wins).closest("div")).toHaveTextContent("1");
    expect(within(performance).getByText(copy.performance.losses).closest("div")).toHaveTextContent("0");
    expect(within(performance).getByText(copy.performance.breakevens).closest("div")).toHaveTextContent("0");
    expect(within(workspace).getByText(copy.trade.maximumRisk)).toBeVisible();
    expect(screen.getByText("$400.00")).toBeVisible();
    expect(screen.getByRole("img", { name: copy.performance.pathDescription })).toBeVisible();
    expect(screen.getByRole("img", { name: copy.trade.payoffDescription })).toBeVisible();
    expect(container.querySelectorAll(".experiment-chart__point")).toHaveLength(2);
    expect(screen.getByText("Scheduled event confirmed")).toBeVisible();
    expect(screen.getAllByText(copy.timeline.entry)).not.toHaveLength(0);
    expect(screen.getByText(copy.timeline.close)).toBeVisible();
    expect(screen.getByText(copy.reason.positionClosed)).toBeVisible();
    expect(screen.getByText(definition.whyChosen[0])).toBeVisible();
    expect(screen.getAllByText("+$600.00")).toHaveLength(2);
    const technical = screen.getByText(copy.workspace.technical).closest("details");
    expect(technical).not.toHaveAttribute("open");
  });

  it("leaves unsupported experiment metrics unavailable and keeps account proof outside the experiment", () => {
    render(<ExperimentWorkspace definition={definition} position={position} proof={proof} />);

    const workspace = screen.getByRole("article", { name: definition.name });
    expect(within(workspace).getByText(copy.performance.projectionUnavailable)).toBeVisible();
    expect(within(workspace).queryByText(copy.performance.paperPnl)).not.toBeInTheDocument();
    expect(within(workspace).queryByText(copy.performance.currentEquity)).not.toBeInTheDocument();
    expect(within(workspace).queryByText("+$200.00")).not.toBeInTheDocument();
    expect(within(workspace).queryByText("+50.00%")).not.toBeInTheDocument();
    expect(within(workspace).queryByText("$100,200.00")).not.toBeInTheDocument();
  });

  it("never presents Replay as paper performance even if proof is supplied", () => {
    render(<ExperimentWorkspace definition={{ ...definition, source: "REPLAY" }} proof={proof} />);

    const workspace = screen.getByRole("article", { name: definition.name });
    expect(within(workspace).getByText(copy.workspace.replay)).toBeVisible();
    expect(within(workspace).getAllByText(copy.performance.replayOnly)).not.toHaveLength(0);
    expect(within(workspace).queryByText("+$200.00")).not.toBeInTheDocument();
    expect(within(workspace).queryByText(copy.performance.currentEquity)).not.toBeInTheDocument();
    expect(within(workspace).queryByText(copy.workspace.paper)).not.toBeInTheDocument();
  });
});
