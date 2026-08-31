import { describe, expect, it } from "vitest";
import health from "../../../../contracts/examples/v1/health.json";
import point from "../../../../contracts/examples/v1/performance-point.json";
import {
  CompetitionPerformanceProofResponseSchema,
  HealthResponseSchema,
  PerformancePointSchema,
} from ".";

describe("canonical contract examples", () => {
  it("validates the health response", () => {
    expect(HealthResponseSchema.parse(health)).toEqual(health);
  });

  it("validates a missing performance point", () => {
    expect(PerformancePointSchema.parse(point)).toEqual(point);
  });

  it("keeps an unpublished proof empty", () => {
    const unpublished = {
      schema_version: "v1",
      publication_status: "NOT_PUBLISHED",
      baseline_status: null,
      published_at: null,
      point: null,
      linked_certificate_ids: [],
      publication_hash: null,
      predecessor_hash: null,
    };
    expect(CompetitionPerformanceProofResponseSchema.parse(unpublished)).toEqual(unpublished);
    expect(() =>
      CompetitionPerformanceProofResponseSchema.parse({
        ...unpublished,
        publication_hash: "a".repeat(64),
      }),
    ).toThrow();
  });

  it("validates clean proof arithmetic and time order", () => {
    const published = {
      schema_version: "v1",
      publication_status: "PUBLISHED",
      baseline_status: "BASELINE_CLEAN",
      published_at: "2026-08-28T15:01:00Z",
      point: {
        schema_version: "v1",
        scheduled_for: "2026-08-28T15:00:00Z",
        attempted_at: "2026-08-28T15:00:05Z",
        measured_at: "2026-08-28T15:00:10Z",
        status: "COMPLETE",
        failure_code: null,
        current_equity_usd: "100250",
        account_equity_change_usd: "250",
        account_equity_return_pct: "0.25",
        reconciled_lifecycle_cashflow_usd: "0",
        open_position_liquidation_pnl_usd: null,
        simulator_limitations_code: "ALPACA_PAPER_SIMULATION",
      },
      linked_certificate_ids: [],
      publication_hash: "a".repeat(64),
      predecessor_hash: null,
    };
    expect(CompetitionPerformanceProofResponseSchema.parse(published)).toEqual(published);
    expect(() =>
      CompetitionPerformanceProofResponseSchema.parse({
        ...published,
        point: { ...published.point, account_equity_return_pct: "0.30" },
      }),
    ).toThrow();
    expect(() =>
      CompetitionPerformanceProofResponseSchema.parse({
        ...published,
        published_at: "2026-08-28T15:00:06Z",
      }),
    ).toThrow();
    expect(() =>
      CompetitionPerformanceProofResponseSchema.parse({
        ...published,
        linked_certificate_ids: [
          "00000000-0000-4000-8000-000000000002",
          "00000000-0000-4000-8000-000000000001",
        ],
      }),
    ).toThrow();
    expect(() =>
      CompetitionPerformanceProofResponseSchema.parse({
        ...published,
        linked_certificate_ids: [
          "00000000-0000-4000-8000-000000000001",
          "00000000-0000-4000-8000-000000000001",
        ],
      }),
    ).toThrow();

    const microdollar = {
      ...published,
      point: {
        ...published.point,
        current_equity_usd: "100000.000001",
        account_equity_change_usd: "0.000001",
        account_equity_return_pct: "0.000000001",
      },
    };
    expect(CompetitionPerformanceProofResponseSchema.parse(microdollar)).toEqual(microdollar);
    expect(() =>
      CompetitionPerformanceProofResponseSchema.parse({
        ...microdollar,
        point: { ...microdollar.point, current_equity_usd: "1E+5" },
      }),
    ).toThrow();
  });
});
