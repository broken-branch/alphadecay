import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { copy } from "../content/copy";
import type { CompetitionRecordResponse } from "./api";
import { CompetitionRecordView } from "./CompetitionRecord";

const recordId = "a".repeat(64);

function positionArchive(): CompetitionRecordResponse {
  return {
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [{
      schema_version: "v1",
      kind: "POSITION",
      public_record_id: recordId,
      occurred_at: "2026-08-31T14:35:00Z",
      published_at: "2026-08-31T15:02:00Z",
      projection_hash: "b".repeat(64),
      publication_hash: "c".repeat(64),
      predecessor_hash: null,
      payload: {
        schema_version: "v1",
        record_kind: "POSITION",
        public_record_id: recordId,
        state: "OPEN",
        underlying: "ACME",
        opening_spread: {
          structure: "VERTICAL",
          underlying: "ACME",
          option_type: "CALL",
          expiration: "2026-09-18",
          long_strike: "130",
          short_strike: "135",
          quantity: 2,
        },
        current_spread: {
          structure: "VERTICAL",
          underlying: "ACME",
          option_type: "CALL",
          expiration: "2026-09-25",
          long_strike: "130",
          short_strike: "135",
          quantity: 2,
        },
        opened_at: "2026-08-31T14:35:00Z",
        as_of: "2026-09-01T15:00:00Z",
        closed_at: null,
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
            cashflow_usd: "-470.00",
            execution_status: "FILLED",
            resulting_state: "OPEN",
            spread_after: {
              structure: "VERTICAL",
              underlying: "ACME",
              option_type: "CALL",
              expiration: "2026-09-18",
              long_strike: "130",
              short_strike: "135",
              quantity: 2,
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
            action: "ROLL",
            occurred_at: "2026-09-01T15:00:00Z",
            reason_category: "POSITION_ROLLED",
            cashflow_usd: "25.00",
            execution_status: "FILLED",
            resulting_state: "OPEN",
            spread_after: {
              structure: "VERTICAL",
              underlying: "ACME",
              option_type: "CALL",
              expiration: "2026-09-25",
              long_strike: "130",
              short_strike: "135",
              quantity: 2,
            },
          },
        ],
        current_exposure: { delta: "51", gamma: "2", theta_per_day: "-5", vega_per_iv_point: "4" },
        execution_status: "FILLED",
        paper_trading: true,
      },
    }],
  };
}

afterEach(cleanup);

describe("Competition record view", () => {
  it("shows loading, unavailable, and deliberately empty states distinctly", () => {
    const loading = render(<CompetitionRecordView archive={undefined} />);
    expect(screen.getByRole("heading", { name: copy.competitionRecord.loading })).toBeVisible();
    loading.unmount();

    const unavailable = render(<CompetitionRecordView archive={null} />);
    expect(screen.getByRole("heading", { name: copy.competitionRecord.unavailable })).toBeVisible();
    expect(screen.getByText(copy.competitionRecord.unavailableDetail)).toBeVisible();
    unavailable.unmount();

    render(<CompetitionRecordView archive={{ schema_version: "v1", publication_status: "NOT_PUBLISHED", records: [] }} />);
    expect(screen.getByRole("heading", { name: copy.competitionRecord.notPublished })).toBeVisible();
  });

  it("renders a real position as a chronological paper timeline", () => {
    render(<CompetitionRecordView archive={positionArchive()} />);

    expect(screen.getByRole("heading", { name: "ACME" })).toBeVisible();
    expect(screen.getByText("130 / 135 call vertical")).toBeVisible();
    const timeline = screen.getByRole("list", { name: copy.competitionRecord.timeline });
    expect(timeline).toHaveTextContent(copy.competitionRecord.entryFilled);
    expect(timeline).toHaveTextContent(copy.competitionRecord.holdReview);
    expect(timeline).toHaveTextContent(copy.competitionRecord.rollFilled);
    expect(timeline).toHaveTextContent("−$470.00");
    expect(timeline).toHaveTextContent("+$25.00");
    expect(timeline.querySelectorAll("li")).toHaveLength(3);
  });
});
