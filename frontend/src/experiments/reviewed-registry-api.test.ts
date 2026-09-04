import { describe, expect, it, vi } from "vitest";
import { curatedProtocolFixture } from "../strategy-protocol/test-fixture";
import {
  createReviewedExperiment,
  listReviewedExperiments,
  ReviewedExperimentRequestError,
} from "./reviewed-registry-api";

function definition(id = "10000000-0000-4000-8000-000000000001") {
  const curation = curatedProtocolFixture();
  return {
    schema_version: "v1" as const,
    experiment_id: id,
    version: 1 as const,
    definition_hash: "a".repeat(64),
    lifecycle_state: "REVIEWED" as const,
    automation_state: "OFF" as const,
    execution_eligible: false as const,
    paper_trading_only: true as const,
    original_thesis: curation.intake,
    reviewed_protocol: curation.protocol_fields,
    curation,
    created_at: "2026-09-01T20:00:00Z",
  };
}

describe("reviewed experiment client", () => {
  it("sends the exact reviewed source with shared CSRF and no-store", async () => {
    const curation = curatedProtocolFixture();
    const fetcher = vi.fn(async () => new Response(JSON.stringify(definition()), { status: 201 }));

    const result = await createReviewedExperiment(
      { original_thesis: curation.intake, reviewed_protocol: curation.protocol_fields, curation },
      "csrf-token",
      fetcher as typeof fetch,
    );

    expect(result.lifecycle_state).toBe("REVIEWED");
    expect(fetcher).toHaveBeenCalledWith("/api/owner/experiments", expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      body: JSON.stringify({
        original_thesis: curation.intake,
        reviewed_protocol: curation.protocol_fields,
        curation,
      }),
    }));
  });

  it("reads the owner registry without selectors or browser persistence", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      schema_version: "v1",
      experiments: [definition()],
    })));

    const result = await listReviewedExperiments("csrf-token", fetcher as typeof fetch);

    expect(result.experiments).toHaveLength(1);
    expect(fetcher).toHaveBeenCalledWith("/api/owner/experiments", expect.objectContaining({
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    }));
  });

  it("rejects malformed authority claims instead of accepting READY or ARMED", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      ...definition(),
      lifecycle_state: "READY",
    })));

    const armedFetcher = vi.fn(async () => new Response(JSON.stringify({
      schema_version: "v1",
      experiments: [{ ...definition(), lifecycle_state: "ARMED" }],
    })));
    await expect(listReviewedExperiments(
      "csrf-token",
      armedFetcher as typeof fetch,
    )).rejects.toEqual(expect.objectContaining({ status: 503 }));
    await expect(createReviewedExperiment(
      {
        original_thesis: curatedProtocolFixture().intake,
        reviewed_protocol: curatedProtocolFixture().protocol_fields,
        curation: curatedProtocolFixture(),
      },
      "csrf-token",
      fetcher as typeof fetch,
    )).rejects.toBeInstanceOf(ReviewedExperimentRequestError);
  });
});
