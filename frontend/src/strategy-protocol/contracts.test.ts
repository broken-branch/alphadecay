import { describe, expect, it } from "vitest";
import {
  StrategyCurationRequestSchema,
  StrategyCurationResponseSchema,
} from "./contracts";
import { curatedProtocolFixture } from "./test-fixture";

describe("strategy curation contracts", () => {
  it("accepts the exact review-only curated response", () => {
    expect(StrategyCurationResponseSchema.parse(curatedProtocolFixture())).toEqual(
      curatedProtocolFixture(),
    );
  });

  it("accepts nullable protocol rules and an exact curation request", () => {
    const response = curatedProtocolFixture();
    const protocolFields = {
      ...response.protocol_fields,
      entry_rule: null,
      invalidation_rules: [],
    };
    expect(StrategyCurationRequestSchema.parse({
      brief: {
        source: response.intake.source,
        market_scope: response.intake.market_scope,
        direction: response.intake.direction,
        horizon: response.intake.horizon,
        evidence: response.intake.evidence,
        invalidation: response.intake.invalidation,
        risk_budget: response.intake.risk_budget,
      },
      protocol_fields: protocolFields,
    }).protocol_fields).toEqual(protocolFields);
  });

  it("validates without rewriting user-owned rule or evidence text", () => {
    const response = curatedProtocolFixture();
    response.protocol_fields.entry_rule = "  Enter after confirmation.  ";
    response.supporting_evidence[0].excerpt = "  Exact user evidence.  ";

    const parsed = StrategyCurationResponseSchema.parse(response);

    expect(parsed.protocol_fields.entry_rule).toBe("  Enter after confirmation.  ");
    expect(parsed.supporting_evidence[0].excerpt).toBe("  Exact user evidence.  ");
  });

  it("rejects executable, free-form, duplicate, and malformed response data", () => {
    expect(StrategyCurationResponseSchema.safeParse({
      ...curatedProtocolFixture(),
      execution_eligible: true,
    }).success).toBe(false);
    expect(StrategyCurationResponseSchema.safeParse({
      ...curatedProtocolFixture(),
      model_summary: "Unbounded model prose",
    }).success).toBe(false);
    expect(StrategyCurationResponseSchema.safeParse({
      ...curatedProtocolFixture(),
      blocking_questions: ["EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED"],
    }).success).toBe(false);
    expect(StrategyCurationResponseSchema.safeParse({
      ...curatedProtocolFixture(),
      supporting_evidence: [{ evidence_id: "source-1", excerpt: "User evidence" }],
    }).success).toBe(false);
  });
});
