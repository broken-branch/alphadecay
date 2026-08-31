import { afterEach, describe, expect, it, vi } from "vitest";
import { competitionRecordResponseSchema, loadCompetitionRecord } from "./api";

const hash = "a".repeat(64);

function noTradeResponse() {
  return {
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [{
      schema_version: "v1",
      kind: "NO_TRADE",
      public_record_id: hash,
      occurred_at: "2026-08-31T14:00:00Z",
      published_at: "2026-08-31T14:01:00Z",
      payload: {
        schema_version: "v1",
        record_kind: "NO_TRADE",
        public_record_id: hash,
        status: "NO_TRADE",
        reason_category: "STRATEGY_NOT_READY",
        decided_at: "2026-08-31T14:00:00Z",
        observed_at: "2026-08-31T14:00:30Z",
        paper_trading: true,
      },
      projection_hash: "b".repeat(64),
      publication_hash: "c".repeat(64),
      predecessor_hash: null,
    }],
  };
}

function positionResponse() {
  const spread = {
    structure: "VERTICAL",
    underlying: "ACME",
    option_type: "CALL",
    expiration: "2026-09-18",
    long_strike: "130.000000",
    short_strike: "135.000000",
    quantity: 1,
  };
  return {
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [{
      schema_version: "v1",
      kind: "POSITION",
      public_record_id: hash,
      occurred_at: "2026-08-31T14:00:00Z",
      published_at: "2026-08-31T14:06:00Z",
      payload: {
        schema_version: "v1",
        record_kind: "POSITION",
        public_record_id: hash,
        state: "OPEN",
        underlying: "ACME",
        opening_spread: spread,
        current_spread: spread,
        opened_at: "2026-08-31T14:00:00Z",
        as_of: "2026-08-31T14:05:00Z",
        closed_at: null,
        thesis: {
          direction: "BULLISH",
          volatility_view: "LONG",
          target_at: "2026-09-07T14:00:00Z",
        },
        events: [{
          event_kind: "EXECUTION",
          action: "ENTRY",
          occurred_at: "2026-08-31T14:00:00Z",
          reason_category: "POSITION_OPENED",
          cashflow_usd: "-235.000000",
          execution_status: "FILLED",
          resulting_state: "OPEN",
          spread_after: spread,
        }],
        current_exposure: {
          delta: "45.000000000",
          gamma: null,
          theta_per_day: "-5.000000000",
          vega_per_iv_point: "4.000000000",
        },
        execution_status: "FILLED",
        paper_trading: true,
      },
      projection_hash: "b".repeat(64),
      publication_hash: "c".repeat(64),
      predecessor_hash: null,
    }],
  };
}

function parsedPositionResponse() {
  const response = competitionRecordResponseSchema.parse(positionResponse());
  const payload = response.records[0].payload;
  if (payload.record_kind !== "POSITION") throw new Error("Expected position fixture");
  return { response, payload };
}

afterEach(() => vi.restoreAllMocks());

describe("competition record API adapter", () => {
  it("accepts a sanitized published record", () => {
    expect(competitionRecordResponseSchema.parse(noTradeResponse()).records).toHaveLength(1);
  });

  it("rejects an empty published response and mismatched record identity", () => {
    expect(() => competitionRecordResponseSchema.parse({
      schema_version: "v1",
      publication_status: "PUBLISHED",
      records: [],
    })).toThrow();

    const mismatch = noTradeResponse();
    mismatch.records[0].payload.public_record_id = "d".repeat(64);
    expect(() => competitionRecordResponseSchema.parse(mismatch)).toThrow();
  });

  it("rejects a broken publication chain", () => {
    const source = noTradeResponse();
    const broken = {
      ...source,
      records: [{ ...source.records[0], predecessor_hash: "d".repeat(64) }],
    };
    expect(() => competitionRecordResponseSchema.parse(broken)).toThrow("Competition record chain is inconsistent");
  });

  it("accepts one coherent position lifecycle", () => {
    expect(competitionRecordResponseSchema.parse(positionResponse()).records).toHaveLength(1);
  });

  it("rejects impossible position measurements and lifecycle states", () => {
    const allNullExposure = parsedPositionResponse();
    allNullExposure.payload.current_exposure = {
      delta: null,
      gamma: null,
      theta_per_day: null,
      vega_per_iv_point: null,
    };
    expect(() => competitionRecordResponseSchema.parse(allNullExposure.response)).toThrow();

    const equalStrikes = parsedPositionResponse();
    if (!equalStrikes.payload.current_spread) throw new Error("Expected current spread");
    equalStrikes.payload.current_spread.short_strike = "130.000000";
    expect(() => competitionRecordResponseSchema.parse(equalStrikes.response)).toThrow();

    const closedWithoutClose = parsedPositionResponse();
    closedWithoutClose.payload.state = "CLOSED";
    closedWithoutClose.payload.current_spread = null;
    closedWithoutClose.payload.closed_at = "2026-08-31T14:05:00Z";
    expect(() => competitionRecordResponseSchema.parse(closedWithoutClose.response)).toThrow();
  });

  it("rejects non-UTC timestamps and out-of-contract decimals", () => {
    const offset = noTradeResponse();
    offset.records[0].payload.decided_at = "2026-08-31T09:00:00-05:00";
    expect(() => competitionRecordResponseSchema.parse(offset)).toThrow();

    const unbounded = parsedPositionResponse();
    if (!unbounded.payload.current_exposure) throw new Error("Expected current exposure");
    unbounded.payload.current_exposure.delta = "1234567890123.1234567890";
    expect(() => competitionRecordResponseSchema.parse(unbounded.response)).toThrow();
  });

  it("uses the anonymous read-only endpoint without browser credentials", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(noTradeResponse()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(loadCompetitionRecord()).resolves.toMatchObject({ publication_status: "PUBLISHED" });
    expect(fetchMock).toHaveBeenCalledWith("/api/competition-record", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "omit",
    });
  });

  it("fails closed on an unavailable endpoint", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 503 }));
    await expect(loadCompetitionRecord()).rejects.toThrow("COMPETITION_RECORD_UNAVAILABLE");
  });
});
