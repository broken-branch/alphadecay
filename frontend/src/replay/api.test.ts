import { describe, expect, it } from "vitest";
import { copy } from "../content/copy";
import { replayFixtures } from "./fixtures";
import { mergeReplayResponse } from "./api";

const hash = "a".repeat(64);

function response() {
  return {
    schema_version: "v1",
    scenario: "THETA_TAKEOVER",
    provenance_label: "REPLAY / FIXTURE DATA",
    input_hash: hash,
    assessment_hash: "b".repeat(64),
    assessment: {
      action: "ROLL",
      quality: "COMPLETE",
      thesis_status: "WEAKENING",
      rationale_code: "EXPOSURE_ROLL",
      execution_decision: "ROLL_APPROVED",
      actual_exposure: {
        schema_version: "v1",
        delta: "85",
        gamma: "1",
        theta_per_day: "-16",
        vega_per_iv_point: "4",
      },
      components: {
        schema_version: "v1",
        evidence_drift: "50",
        exposure_mismatch: "60",
        time_pressure: "40",
        volatility_mismatch: "0",
        risk_utilization: "56",
        unrounded_score: "46.90",
        display_score: 47,
        dominant_non_evidence_component: "EXPOSURE",
      },
      alternatives: [
        { action: "HOLD", eligible: false },
        { action: "CLOSE", eligible: false },
        { action: "ROLL", eligible: true },
      ],
      policy_hash: "replay-policy-v0.1",
    },
    certificate: {
      lineage_hash: "c".repeat(64),
      account_role: "REPLAY",
      thesis: {
        thesis: {
          underlying: "ACME",
          thesis_code: "POST_EVENT_CONTINUATION_V1",
          invalidation_codes: ["PRIMARY_CONTRADICTION"],
          intended_exposure: {
            delta: "50",
            gamma: "2",
            theta_per_day: "-4",
            vega_per_iv_point: "4",
          },
        },
      },
      expected_after_exposure: {
        delta: "55",
        gamma: "2",
        theta_per_day: "-5",
        vega_per_iv_point: "4",
      },
      actual_after_exposure: null,
      attempts: [],
      execution_state: "NOT_REQUESTED",
    },
    presentation: {
      opening: {
        underlying: "ACME",
        reference_spot: "132.50",
        spread_kind: "BULL_CALL_SPREAD",
        long_strike: "130",
        short_strike: "135",
        expiration_date: "2026-09-18",
        quantity: 2,
        contract_multiplier: 100,
        entry_net_debit_per_share_usd: "2.35",
        maximum_loss: "470",
        approved_risk_cap: "500",
        delta_low: "40",
        delta_high: "60",
        vega_low: "2",
        vega_high: "6",
        maximum_daily_theta: "8",
        minimum_dte: 14,
        maximum_dte: 35,
        selection_state: "PRESELECTED_SAMPLE",
      },
      market: {
        assessed_at: "2026-09-03T15:15:00Z",
        review_by: "2026-09-03T15:20:00Z",
        urgency: "SOON",
        quote_status: "FRESH",
        quote_age_seconds: 0,
        dte: 15,
        mark: "1.60",
        bid: "1.54",
        ask: "1.66",
        liquidation_value: "308",
        open_pnl: "-162",
        implied_volatility: "0.50",
        iv_change_points: "0",
      },
      evidence: {
        status: "CLASSIFIED",
        classifications: [
          {
            source_id: "FIXTURE-PRIMARY-01",
            headline: "Sample update: outlook reaffirmed",
            observed_at: "2026-09-03T15:15:00Z",
            event_code: "OUTLOOK_REAFFIRMED",
            relation: "SUPPORTS",
            materiality: 2,
            relevance: "0.90",
            confidence: "0.90",
            source_tier: "PRIMARY",
            invalidates: false,
          },
          {
            source_id: "FIXTURE-PRIMARY-02",
            headline: "Sample update: timing moved later",
            observed_at: "2026-09-03T15:15:00Z",
            event_code: "TIMING_DELAYED",
            relation: "CONTRADICTS",
            materiality: 2,
            relevance: "0.90",
            confidence: "0.90",
            source_tier: "PRIMARY",
            invalidates: false,
          },
        ],
      },
      integration: {
        fixture_validation: "COMPLETE",
        deterministic_policy: "COMPLETE",
        trading_api: "NOT_RUN",
        mcp: "NOT_RUN",
        model: "NOT_RUN",
        cli: "NOT_RUN",
        order_entry: "DISABLED",
      },
      roll: {
        expiration_date: "2026-09-25",
        long_strike: "130",
        short_strike: "135",
        quantity: 2,
        contract_multiplier: 100,
        estimated_net_debit_per_share_usd: "0.10",
        resulting_maximum_loss: "490",
      },
    },
    execution_enabled: false,
  };
}

describe("Replay API adapter", () => {
  it("keeps the prospective thesis and sample market math consistent across outcomes", () => {
    const fixtures = Object.values(replayFixtures);
    expect(new Set(fixtures.map(({ openingThesis }) => openingThesis))).toHaveLength(1);

    for (const fixture of fixtures.filter(({ blockedState }) => blockedState !== "STALE")) {
      const entryDebit = Number(fixture.market.entryPrice.match(/\d+\.\d+/)?.[0]);
      const bid = Number(fixture.market.bidAsk.split("/")[0].replace("$", "").trim());
      const liquidationValue = Number(fixture.market.liquidationValue.replace("$", ""));
      const openPnl = Number(fixture.market.openPnl.replace("$", ""));

      expect(bid * 2 * 100).toBeCloseTo(liquidationValue, 2);
      expect(liquidationValue - entryDebit * 2 * 100).toBeCloseTo(openPnl, 2);
      expect(fixture.market.iv).toBe("50.0%");
      expect(fixture.market.ivChange).toBe("0.0 pts");
      expect(fixture.market.orderState).toBe("Fixture only · no order or fill");
    }
  });

  it("hydrates presentation data from the canonical response", () => {
    const fixture = mergeReplayResponse(response(), replayFixtures.THETA_TAKEOVER);

    expect(fixture.measured.delta).toBe(85);
    expect(fixture.intended.delta).toBe(50);
    expect(fixture.openingThesis).toBe(replayFixtures.THETA_TAKEOVER.openingThesis);
    expect(fixture.invalidationCode).toBe("PRIMARY_CONTRADICTION");
    expect(fixture.expectedAfter?.thetaPerDay).toBe(-5);
    expect(fixture.comparison.find(({ key }) => key === "evidence")?.measuredValue).toBe("50");
    expect(fixture.drift.map(({ points }) => points)).toEqual([60, 0, 40, 50, 56]);
    expect(fixture.position).toEqual({
      symbol: "ACME",
      strikes: "$130 / $135",
      expiry: "18 Sep",
      quantity: "2 spreads",
    });
    expect(fixture.evidenceCards).toHaveLength(2);
    expect(fixture.market.riskCap).toBe("$500");
    expect(fixture.market.openPnl).toBe("-$162");
    expect(fixture.measured.maxLoss).toBe(470);
    expect(fixture.lineage.inputHash).toBe(hash);
    expect(fixture.lineage.assessmentHash).toBe("b".repeat(64));
    expect(fixture.alternatives.find(({ action }) => action === "ROLL")?.state).toBe("SELECTED");
  });

  it("derives classification copy from the response instead of fallback prose", () => {
    const fallback = {
      ...replayFixtures.THETA_TAKEOVER,
      provenance: {
        ...replayFixtures.THETA_TAKEOVER.provenance,
        classification: "untrusted fallback",
      },
    };

    const fixture = mergeReplayResponse(response(), fallback);
    expect(fixture.provenance.classification).toBe(copy.provenanceDetail.weakenedClassification);
    expect(fixture.provenance.classification).not.toBe("untrusted fallback");
  });

  it("rejects quote-quality and roll-width contract drift", () => {
    const qualityMismatch = response();
    qualityMismatch.assessment.quality = "STALE";
    expect(() => mergeReplayResponse(qualityMismatch, replayFixtures.THETA_TAKEOVER)).toThrow(
      "REPLAY_QUOTE_QUALITY_MISMATCH",
    );

    const widthMismatch = response();
    widthMismatch.presentation.roll.short_strike = "140";
    expect(() => mergeReplayResponse(widthMismatch, replayFixtures.THETA_TAKEOVER)).toThrow();

    const negativeDebit = response();
    negativeDebit.presentation.roll.estimated_net_debit_per_share_usd = "-0.10";
    negativeDebit.presentation.roll.resulting_maximum_loss = "450";
    expect(() => mergeReplayResponse(negativeDebit, replayFixtures.THETA_TAKEOVER)).toThrow(
      "Expected a non-negative roll debit",
    );
  });

  it("rejects a response for a different scenario", () => {
    expect(() =>
      mergeReplayResponse(
        { ...response(), scenario: "THESIS_INTACT" },
        replayFixtures.THETA_TAKEOVER,
      ),
    ).toThrow("REPLAY_SCENARIO_MISMATCH");
  });

  it("rejects an outcome-labeled opening thesis", () => {
    const raw = response();
    raw.certificate.thesis.thesis.thesis_code = "THETA_TAKEOVER";
    expect(() => mergeReplayResponse(raw, replayFixtures.THETA_TAKEOVER)).toThrow(
      "REPLAY_THESIS_CODE_MISMATCH",
    );
  });

  it("rejects a broker outcome on an execution-disabled Replay", () => {
    const raw = response();

    expect(() =>
      mergeReplayResponse(
        {
          ...raw,
          certificate: {
            ...raw.certificate,
            actual_after_exposure: raw.certificate.expected_after_exposure,
          },
        },
        replayFixtures.THETA_TAKEOVER,
      ),
    ).toThrow();
  });

  it("keeps a stale quote assessment blocked with no expected action result", () => {
    const raw = response();
    const blocked = {
      ...raw,
      scenario: "STALE_QUOTE",
      assessment: {
        ...raw.assessment,
        action: "NO_ACTION",
        quality: "STALE",
        thesis_status: "INTACT",
        rationale_code: "EXECUTION_DATA_MISSING",
        execution_decision: "NO_ACTION",
        components: null,
        alternatives: raw.assessment.alternatives.map((alternative) => ({
          ...alternative,
          eligible: false,
        })),
      },
      certificate: {
        ...raw.certificate,
        thesis: {
          thesis: {
            ...raw.certificate.thesis.thesis,
            thesis_code: "POST_EVENT_CONTINUATION_V1",
          },
        },
        expected_after_exposure: null,
      },
      presentation: {
        ...raw.presentation,
        market: {
          ...raw.presentation.market,
          assessed_at: "2026-08-28T15:15:00Z",
          review_by: null,
          urgency: "WAITING",
          quote_status: "STALE",
          quote_age_seconds: 45,
          dte: 21,
          mark: null,
          bid: null,
          ask: null,
          liquidation_value: null,
          open_pnl: null,
          implied_volatility: null,
          iv_change_points: null,
        },
        evidence: { status: "NOT_RUN", classifications: [] },
        roll: null,
      },
    };

    const fixture = mergeReplayResponse(blocked, replayFixtures.STALE_QUOTE);

    expect(fixture.action).toBe("NO_ACTION");
    expect(fixture.blockedState).toBe("STALE");
    expect(fixture.provenance.support).toBe("UNKNOWN");
    expect(fixture.provenance.classification).toBe(copy.provenanceDetail.unknownClassification);
    expect(fixture.expectedAfter).toBeNull();
    expect(fixture.alternatives.every(({ state }) => state === "UNAVAILABLE")).toBe(true);
  });
});
