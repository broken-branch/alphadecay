import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { competitionRecordResponseSchema } from "../competition-record/api";
import { ExperimentHistory } from "./ExperimentHistory";
import { copy as publicCopy } from "../content/copy";
import { projectExperimentHistory } from "./experiment-history";

const noTradeId = "a".repeat(64);
const positionId = "b".repeat(64);
const copy = publicCopy.experiment.history;
const openingSpread = {
  structure: "VERTICAL" as const,
  underlying: "SPY",
  option_type: "CALL" as const,
  expiration: "2026-09-18",
  long_strike: "640",
  short_strike: "645",
  quantity: 1,
};

function publishedHistory(state: "OPEN" | "CLOSED" = "OPEN") {
  const entry = {
    event_kind: "EXECUTION" as const,
    action: "ENTRY" as const,
    occurred_at: "2026-09-01T15:00:00Z",
    reason_category: "POSITION_OPENED" as const,
    cashflow_usd: "-180.00",
    execution_status: "FILLED" as const,
    resulting_state: "OPEN" as const,
    spread_after: openingSpread,
  };
  const hold = {
    event_kind: "ASSESSMENT" as const,
    action: "HOLD" as const,
    occurred_at: "2026-09-01T16:00:00Z",
    reason_category: "POSITION_REVIEWED" as const,
  };
  const close = {
    event_kind: "EXECUTION" as const,
    action: "CLOSE" as const,
    occurred_at: "2026-09-02T15:00:00Z",
    reason_category: "POSITION_CLOSED" as const,
    cashflow_usd: "240.00",
    execution_status: "FILLED" as const,
    resulting_state: "CLOSED" as const,
    spread_after: null,
  };
  const closed = state === "CLOSED";

  return competitionRecordResponseSchema.parse({
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [
      {
        schema_version: "v1",
        kind: "NO_TRADE",
        public_record_id: noTradeId,
        occurred_at: "2026-08-31T15:00:00Z",
        published_at: "2026-08-31T15:01:00Z",
        projection_hash: "c".repeat(64),
        publication_hash: "d".repeat(64),
        predecessor_hash: null,
        payload: {
          schema_version: "v1",
          record_kind: "NO_TRADE",
          public_record_id: noTradeId,
          status: "NO_TRADE",
          reason_category: "STRATEGY_NOT_READY",
          decided_at: "2026-08-31T15:00:00Z",
          observed_at: "2026-08-31T15:00:01Z",
          paper_trading: true,
        },
      },
      {
        schema_version: "v1",
        kind: "POSITION",
        public_record_id: positionId,
        occurred_at: entry.occurred_at,
        published_at: closed ? "2026-09-02T15:01:00Z" : "2026-09-01T16:01:00Z",
        projection_hash: "e".repeat(64),
        publication_hash: "f".repeat(64),
        predecessor_hash: "d".repeat(64),
        payload: {
          schema_version: "v1",
          record_kind: "POSITION",
          public_record_id: positionId,
          state,
          underlying: "SPY",
          opening_spread: openingSpread,
          current_spread: closed ? null : openingSpread,
          opened_at: entry.occurred_at,
          as_of: closed ? close.occurred_at : hold.occurred_at,
          closed_at: closed ? close.occurred_at : null,
          thesis: {
            direction: "BULLISH",
            volatility_view: "NEUTRAL",
            target_at: "2026-09-03T15:00:00Z",
          },
          events: closed ? [entry, hold, close] : [entry, hold],
          current_exposure: closed ? null : {
            delta: "41",
            gamma: null,
            theta_per_day: null,
            vega_per_iv_point: null,
          },
          execution_status: "FILLED",
          paper_trading: true,
        },
      },
    ],
  });
}

afterEach(cleanup);

describe("experiment history projection", () => {
  it("preserves validated publication chronology and keeps exact records separate", () => {
    const history = projectExperimentHistory(publishedHistory());

    expect(history.sourceState).toBe("PUBLISHED");
    expect(history.records.map((record) => record.publicRecordId)).toEqual([noTradeId, positionId]);
    expect(new Set(history.records.map((record) => record.selectionKey)).size).toBe(2);
    expect(history.records[0].label).toBe(copy.noTrade);
    expect(history.records[1].label).toBe(`${copy.position} · SPY`);
  });

  it("projects only contract-backed thesis, state, and latest decision fields", () => {
    const open = projectExperimentHistory(publishedHistory("OPEN")).records[1];
    expect(open.state).toBe(copy.positionOpen);
    expect(open.thesis).toEqual({
      direction: copy.bullish,
      volatilityView: copy.neutralVolatility,
      targetAt: "2026-09-03T15:00:00Z",
    });
    expect(open.latestDecision).toEqual({
      action: copy.hold,
      reason: copy.positionReviewed,
      occurredAt: "2026-09-01T16:00:00Z",
    });
    expect(open.maximumRisk).toBeNull();
    expect(open.result).toBeNull();
    expect(open).not.toHaveProperty("pnl");
    expect(open).not.toHaveProperty("benchmark");
    expect(open).not.toHaveProperty("accountProof");

    const closed = projectExperimentHistory(publishedHistory("CLOSED")).records[1];
    expect(closed.state).toBe(copy.positionClosed);
    expect(closed.latestDecision.action).toBe(copy.close);
  });

  it("keeps no-trade thesis and results unavailable", () => {
    const record = projectExperimentHistory(publishedHistory()).records[0];
    expect(record.thesis).toBeNull();
    expect(record.maximumRisk).toBeNull();
    expect(record.result).toBeNull();
    expect(record.latestDecision.reason).toBe(copy.strategyNotReady);
  });

  it("distinguishes pending, unavailable, and not-published sources", () => {
    expect(projectExperimentHistory(undefined)).toEqual({ sourceState: "PENDING", records: [] });
    expect(projectExperimentHistory(null)).toEqual({ sourceState: "UNAVAILABLE", records: [] });
    const notPublished = competitionRecordResponseSchema.parse({
      schema_version: "v1",
      publication_status: "NOT_PUBLISHED",
      records: [],
    });
    expect(projectExperimentHistory(notPublished)).toEqual({ sourceState: "NOT_PUBLISHED", records: [] });
  });
});

describe("experiment history component", () => {
  it("uses controlled selection and exposes the lineage limitation", () => {
    const onSelect = vi.fn();
    const history = projectExperimentHistory(publishedHistory());
    const positionKey = history.records[1].selectionKey;
    render(<ExperimentHistory history={history} selectedRecordKey={positionKey} onSelectRecord={onSelect} />);

    expect(screen.getByText(copy.limitation)).toBeVisible();
    const selected = screen.getByRole("button", { name: /Position · SPY/ });
    expect(selected).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("heading", { name: "Position · SPY" })).toBeVisible();
    expect(screen.getByText(copy.riskUnavailable)).toBeVisible();
    expect(screen.getByText(copy.resultUnavailable)).toBeVisible();
    expect(screen.queryByText("$60.00")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: new RegExp(copy.noTrade, "i") }));
    expect(onSelect).toHaveBeenCalledWith(history.records[0].selectionKey);
  });

  it("does not choose a record on the caller's behalf", () => {
    const history = projectExperimentHistory(publishedHistory());
    render(<ExperimentHistory history={history} selectedRecordKey={null} />);
    expect(screen.getByText(copy.chooseRecord)).toBeVisible();
    expect(screen.queryByRole("heading", { level: 3 })).not.toBeInTheDocument();
  });

  it("renders an honest empty state for each unavailable source", () => {
    const { rerender } = render(
      <ExperimentHistory history={projectExperimentHistory(undefined)} selectedRecordKey={null} />,
    );
    expect(screen.getByText(copy.loading)).toBeVisible();
    rerender(<ExperimentHistory history={projectExperimentHistory(null)} selectedRecordKey={null} />);
    expect(screen.getByText(copy.unavailable)).toBeVisible();
  });
});
