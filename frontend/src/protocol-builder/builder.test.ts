import { describe, expect, it } from "vitest";
import { buildReviewedExecutableProtocolRequest, emptyProtocolBuilderDraft } from "./builder";
import { completeProtocolDraft, readyCurationFixture } from "./test-fixture";

describe("executable protocol builder", () => {
  it("emits the exact closed debit-vertical request only when complete", () => {
    const curation = readyCurationFixture();
    const result = buildReviewedExecutableProtocolRequest(curation, completeProtocolDraft());

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value).toMatchObject({
      review_state: "REVIEWED",
      curation,
      definition: {
        benchmark_symbol: "QQQ",
        schedule: {
          entry_window_start: "2026-09-02T13:45:00Z",
          decision_boundary: "2026-09-02T13:50:00Z",
        },
        selection: {
          minimum_dte: 30,
          target_dte: 38,
          maximum_dte: 45,
          width_dollars: "5",
          quantity: 1,
          maximum_debit_per_share: "2.4",
          maximum_loss_dollars: "240",
        },
      },
      rules: {
        entry_rule: {
          source_text: curation.protocol_fields.entry_rule,
          mapping_state: "FULLY_MAPPED",
          predicates: [{
            kind: "NUMERIC",
            left: { kind: "METRIC", metric: "UNDERLYING_SESSION_CLOSE" },
            operator: "GREATER_THAN",
            right: { kind: "METRIC", metric: "UNDERLYING_SMA_50" },
          }],
        },
      },
    });
  });

  it("does not invent strikes, dates, quality limits, codes, or rule mappings", () => {
    const result = buildReviewedExecutableProtocolRequest(
      readyCurationFixture(),
      emptyProtocolBuilderDraft(readyCurationFixture()),
    );

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.value).toBeNull();
    expect(result.errors).toEqual(expect.objectContaining({
      opportunityKey: "code",
      benchmarkSymbol: "benchmark",
      sessions: "dates",
      windows: "time",
      minimumStrike: "number",
      maximumLossDollars: expect.any(String),
      entryRule: "rule",
      noTradeRule: "rule",
      "invalidationRule.0": "rule",
    }));
  });

  it("allows the strategy symbol to be used as its own benchmark label", () => {
    const result = buildReviewedExecutableProtocolRequest(
      readyCurationFixture(),
      { ...completeProtocolDraft(), benchmarkSymbol: "SPY" },
    );

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.definition.benchmark_symbol).toBe("SPY");
  });

  it("rejects inconsistent risk, DTE, structure, and incomplete curation", () => {
    const curation = readyCurationFixture();
    const draft = {
      ...completeProtocolDraft(),
      direction: "BEARISH" as const,
      maximumLossDollars: "200",
      targetDte: "50",
    };
    const result = buildReviewedExecutableProtocolRequest(curation, draft);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors).toEqual(expect.objectContaining({
      direction: "direction",
      maximumLossDollars: "risk",
      targetDte: "dte",
    }));

    const unreviewed = buildReviewedExecutableProtocolRequest(
      { ...curation, blocking_questions: ["EVIDENCE_REQUIRED"] },
      completeProtocolDraft(),
    );
    expect(unreviewed.ok).toBe(false);
    if (!unreviewed.ok) expect(unreviewed.errors.curation).toBe("review");
  });
});
