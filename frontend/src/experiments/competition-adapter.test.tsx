import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CompetitionPerformanceProofResponseSchema } from "../contracts/v1";
import { competitionRecordResponseSchema } from "../competition-record/api";
import { copy as publicCopy } from "../content/copy";
import { ExperimentWorkspace } from "./ExperimentWorkspace";
import { adaptCompetitionExperiment } from "./competition-adapter";
import type { CompetitionExperimentAdapterInput, ExperimentDefinition } from "./types";

const copy = publicCopy.experiment;

const recordId = "a".repeat(64);
const certificateId = "00000000-0000-4000-8000-000000000001";

const definition: ExperimentDefinition = {
  id: "spy-structured-pilot",
  name: "SPY structured pilot",
  underlying: "SPY",
  thesis: "A bounded bullish spread tests whether the session can hold its direction.",
  whyChosen: ["The plan was fixed before the session."],
  invalidation: ["The required market condition fails."],
  status: "WATCHING",
  source: "PAPER",
  structure: "740 / 744 call vertical",
  maximumRiskUsd: "200.00",
  policyVersion: "structured-pilot-v1",
};

function positionArchive(state: "OPEN" | "CLOSED") {
  const openingSpread = {
    structure: "VERTICAL" as const,
    underlying: "SPY",
    option_type: "CALL" as const,
    expiration: "2026-10-09",
    long_strike: "740",
    short_strike: "744",
    quantity: 1,
  };
  const entry = {
    event_kind: "EXECUTION" as const,
    action: "ENTRY" as const,
    occurred_at: "2026-09-02T13:55:00Z",
    reason_category: "POSITION_OPENED" as const,
    cashflow_usd: "-200.00",
    execution_status: "FILLED" as const,
    resulting_state: "OPEN" as const,
    spread_after: openingSpread,
  };
  const hold = {
    event_kind: "ASSESSMENT" as const,
    action: "HOLD" as const,
    occurred_at: "2026-09-02T14:05:00Z",
    reason_category: "POSITION_REVIEWED" as const,
  };
  const close = {
    event_kind: "EXECUTION" as const,
    action: "CLOSE" as const,
    occurred_at: "2026-09-03T13:45:00Z",
    reason_category: "POSITION_CLOSED" as const,
    cashflow_usd: "250.00",
    execution_status: "FILLED" as const,
    resulting_state: "CLOSED" as const,
    spread_after: null,
  };
  const closed = state === "CLOSED";
  const asOf = closed ? close.occurred_at : hold.occurred_at;
  return competitionRecordResponseSchema.parse({
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [{
      schema_version: "v1",
      kind: "POSITION",
      public_record_id: recordId,
      occurred_at: entry.occurred_at,
      published_at: closed ? "2026-09-03T13:50:00Z" : "2026-09-02T14:10:00Z",
      projection_hash: "b".repeat(64),
      publication_hash: "c".repeat(64),
      predecessor_hash: null,
      payload: {
        schema_version: "v1",
        record_kind: "POSITION",
        public_record_id: recordId,
        state,
        underlying: "SPY",
        opening_spread: openingSpread,
        current_spread: closed ? null : openingSpread,
        opened_at: entry.occurred_at,
        as_of: asOf,
        closed_at: closed ? close.occurred_at : null,
        thesis: {
          direction: "BULLISH",
          volatility_view: "NEUTRAL",
          target_at: "2026-09-03T14:00:00Z",
        },
        events: closed ? [entry, hold, close] : [entry, hold],
        current_exposure: closed
          ? null
          : { delta: "55", gamma: "1.2", theta_per_day: "-3", vega_per_iv_point: "2" },
        execution_status: "FILLED",
        paper_trading: true,
      },
    }],
  });
}

function noTradeArchive() {
  return competitionRecordResponseSchema.parse({
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [{
      schema_version: "v1",
      kind: "NO_TRADE",
      public_record_id: recordId,
      occurred_at: "2026-09-02T14:00:00Z",
      published_at: "2026-09-02T14:01:00Z",
      projection_hash: "b".repeat(64),
      publication_hash: "c".repeat(64),
      predecessor_hash: null,
      payload: {
        schema_version: "v1",
        record_kind: "NO_TRADE",
        public_record_id: recordId,
        status: "NO_TRADE",
        reason_category: "STRATEGY_NOT_READY",
        decided_at: "2026-09-02T14:00:00Z",
        observed_at: "2026-09-02T14:00:01Z",
        paper_trading: true,
      },
    }],
  });
}

function proof(
  state: "OPEN" | "CLOSED",
  linkedIds: string[] = [certificateId],
  measuredAt?: string,
) {
  const closed = state === "CLOSED";
  const change = closed ? "50.00" : "25.00";
  return CompetitionPerformanceProofResponseSchema.parse({
    schema_version: "v1",
    publication_status: "PUBLISHED",
    baseline_status: "BASELINE_CLEAN",
    published_at: closed ? "2026-09-03T14:00:00Z" : "2026-09-02T14:20:00Z",
    linked_certificate_ids: linkedIds,
    publication_hash: "d".repeat(64),
    predecessor_hash: null,
    point: {
      schema_version: "v1",
      scheduled_for: closed ? "2026-09-03T13:55:00Z" : "2026-09-02T14:15:00Z",
      attempted_at: closed ? "2026-09-03T13:55:01Z" : "2026-09-02T14:15:01Z",
      measured_at: measuredAt ?? (closed ? "2026-09-03T13:55:02Z" : "2026-09-02T14:15:02Z"),
      status: "COMPLETE",
      failure_code: null,
      current_equity_usd: closed ? "100050.00" : "100025.00",
      account_equity_change_usd: change,
      account_equity_return_pct: closed ? "0.050000000" : "0.025000000",
      reconciled_lifecycle_cashflow_usd: closed ? "50.00" : "-200.00",
      open_position_liquidation_pnl_usd: closed ? "0.00" : "25.00",
      simulator_limitations_code: "ALPACA_PAPER_SIMULATION",
    },
  });
}

function input(overrides: Partial<CompetitionExperimentAdapterInput> = {}): CompetitionExperimentAdapterInput {
  return {
    definition,
    archive: positionArchive("OPEN"),
    proof: proof("OPEN"),
    lineage: { publicRecordId: recordId, certificateId },
    ...overrides,
  };
}

afterEach(cleanup);

describe("competition experiment adapter", () => {
  it("binds an open position to one exact certificate without presenting account metrics as strategy results", () => {
    const result = adaptCompetitionExperiment(input());

    expect(result.archiveState).toBe("POSITION");
    expect(result.proofState).toBe("MATCHED");
    expect(result.workspace.position?.payload.state).toBe("OPEN");
    expect(result.workspace.proof?.point?.open_position_liquidation_pnl_usd).toBe("25.00");
    expect(result.workspace.definition.maximumRiskUsd).toBe("200.00");
    expect(result.workspace.performanceProjection).toBeNull();
    expect(result.workspace.benchmark).toBeNull();
    expect(result.workspace.payoff).toBeNull();
    expect(result.workspace.valuePath).toEqual([]);

    render(<ExperimentWorkspace {...result.workspace} />);
    expect(screen.getByText(copy.performance.projectionUnavailable)).toBeVisible();
    expect(screen.getAllByText(copy.trade.notRecorded)).not.toHaveLength(0);
    expect(screen.queryByText("+$25.00")).not.toBeInTheDocument();
    expect(screen.queryByText("+12.50%")).not.toBeInTheDocument();
  });

  it("preserves closed lifecycle chronology without deriving performance from fill cash flow", () => {
    const result = adaptCompetitionExperiment(input({
      archive: positionArchive("CLOSED"),
      proof: proof("CLOSED"),
    }));

    expect(result.proofState).toBe("MATCHED");
    expect(result.workspace.position?.payload.events.map((event) => event.occurred_at)).toEqual([
      "2026-09-02T13:55:00Z",
      "2026-09-02T14:05:00Z",
      "2026-09-03T13:45:00Z",
    ]);
    render(<ExperimentWorkspace {...result.workspace} />);
    expect(screen.queryByText("+$50.00")).not.toBeInTheDocument();
    expect(screen.queryByText("+25.00%")).not.toBeInTheDocument();
    expect(screen.getByText(copy.timeline.close)).toBeVisible();
  });

  it("maps a no-trade record to a rejected experiment without attaching account proof", () => {
    const result = adaptCompetitionExperiment(input({
      archive: noTradeArchive(),
      lineage: { publicRecordId: recordId, certificateId: null },
    }));

    expect(result.archiveState).toBe("NO_TRADE");
    expect(result.proofState).toBe("NOT_APPLICABLE");
    expect(result.workspace.definition.status).toBe("REJECTED");
    expect(result.workspace.position).toBeNull();
    expect(result.workspace.proof).toBeUndefined();
    expect(result.workspace.rejectionReason).toBe(copy.adapter.strategyNotReady);
    expect(result.workspace.benchmark).toBeNull();
    expect(result.workspace.payoff).toBeNull();
    expect(result.workspace.performanceProjection).toBeNull();
    render(<ExperimentWorkspace {...result.workspace} />);
    expect(screen.getByText(copy.performance.projectionUnavailable)).toBeVisible();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("keeps not-published and unavailable sources distinct", () => {
    const notPublishedArchive = competitionRecordResponseSchema.parse({
      schema_version: "v1",
      publication_status: "NOT_PUBLISHED",
      records: [],
    });
    const notPublishedProof = CompetitionPerformanceProofResponseSchema.parse({
      schema_version: "v1",
      publication_status: "NOT_PUBLISHED",
      baseline_status: null,
      published_at: null,
      point: null,
      linked_certificate_ids: [],
      publication_hash: null,
      predecessor_hash: null,
    });

    const absent = adaptCompetitionExperiment(input({
      archive: notPublishedArchive,
      proof: notPublishedProof,
    }));
    expect(absent.archiveState).toBe("NOT_PUBLISHED");
    expect(absent.proofState).toBe("NOT_PUBLISHED");
    expect(absent.workspace.position).toBeNull();
    expect(absent.workspace.valuePath).toEqual([]);

    const positionWithoutProof = adaptCompetitionExperiment(input({ proof: notPublishedProof }));
    expect(positionWithoutProof.archiveState).toBe("POSITION");
    expect(positionWithoutProof.proofState).toBe("NOT_PUBLISHED");
    expect(positionWithoutProof.workspace.position?.payload.state).toBe("OPEN");
    expect(positionWithoutProof.workspace.proof?.publication_status).toBe("NOT_PUBLISHED");

    const positionWithUnavailableProof = adaptCompetitionExperiment(input({ proof: null }));
    expect(positionWithUnavailableProof.archiveState).toBe("POSITION");
    expect(positionWithUnavailableProof.proofState).toBe("UNAVAILABLE");
    expect(positionWithUnavailableProof.workspace.position?.payload.state).toBe("OPEN");
    expect(positionWithUnavailableProof.workspace.proof).toBeNull();

    const unavailable = adaptCompetitionExperiment(input({ archive: null, proof: null }));
    expect(unavailable.archiveState).toBe("UNAVAILABLE");
    expect(unavailable.proofState).toBe("UNAVAILABLE");
    expect(unavailable.workspace.proof).toBeNull();
  });

  it("retains a published position but refuses unrelated or stale proof", () => {
    const unrelated = adaptCompetitionExperiment(input({
      proof: proof("OPEN", ["00000000-0000-4000-8000-000000000002"]),
    }));
    expect(unrelated.archiveState).toBe("POSITION");
    expect(unrelated.proofState).toBe("LINEAGE_MISMATCH");
    expect(unrelated.workspace.position).not.toBeNull();
    expect(unrelated.workspace.proof).toBeNull();

    const aggregate = adaptCompetitionExperiment(input({
      proof: proof("OPEN", [certificateId, "00000000-0000-4000-8000-000000000002"].sort()),
    }));
    expect(aggregate.proofState).toBe("LINEAGE_MISMATCH");
    expect(aggregate.workspace.proof).toBeNull();

    const laterPosition = structuredClone(positionArchive("OPEN"));
    const laterRecord = laterPosition.records[0];
    if (laterRecord.payload.record_kind === "POSITION") {
      laterRecord.payload.as_of = "2026-09-02T14:30:00Z";
    }
    const stale = adaptCompetitionExperiment(input({
      archive: competitionRecordResponseSchema.parse(laterPosition),
      proof: proof("OPEN"),
    }));
    expect(stale.proofState).toBe("LINEAGE_MISMATCH");
    expect(stale.workspace.proof).toBeNull();
  });

  it("does not join a position when the public record lineage is missing", () => {
    const wrongRecord = adaptCompetitionExperiment(input({
      lineage: { publicRecordId: "f".repeat(64), certificateId },
    }));
    expect(wrongRecord.archiveState).toBe("LINEAGE_MISMATCH");
    expect(wrongRecord.workspace.position).toBeNull();
    expect(wrongRecord.workspace.proof).toBeUndefined();
  });

  it("keeps Replay definitions separate from competition records and proof", () => {
    const result = adaptCompetitionExperiment(input({
      definition: { ...definition, source: "REPLAY" },
    }));

    expect(result.archiveState).toBe("LINEAGE_MISMATCH");
    expect(result.workspace.position).toBeNull();
    expect(result.workspace.proof).toBeUndefined();
    expect(result.workspace.definition.source).toBe("REPLAY");
  });
});
