import { describe, expect, it, vi } from "vitest";
import {
  compiledExperimentFixture,
  compileRequestFixture,
  experimentAuthorizationFixture,
} from "./compiled-test-fixture";
import {
  armExperiment,
  compileReviewedExperiment,
  disarmExperiment,
  readExperimentAuthorization,
  readCompiledExperiment,
} from "./reviewed-registry-api";

const experimentId = "10000000-0000-4000-8000-000000000001";

describe("compiled experiment owner client", () => {
  it("compiles the exact source with shared CSRF and no-store", async () => {
    const input = compileRequestFixture();
    const fetcher = vi.fn(async () => new Response(
      JSON.stringify(compiledExperimentFixture()),
      { status: 201 },
    ));

    const result = await compileReviewedExperiment(experimentId, input, "csrf-token", fetcher as typeof fetch);

    expect(result.lifecycle_state).toBe("COMPILED");
    expect(result.arm_state).toBe("NOT_ARMED");
    expect(result.execution_eligible).toBe(false);
    expect(fetcher).toHaveBeenCalledWith(
      `/api/owner/experiments/${experimentId}/compile`,
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify(input),
      }),
    );
  });

  it("reads a compiled version fresh and treats a missing version as incomplete setup", async () => {
    const present = vi.fn(async () => new Response(JSON.stringify(compiledExperimentFixture())));
    const missing = vi.fn(async () => new Response(null, { status: 404 }));

    await expect(readCompiledExperiment(experimentId, "csrf-token", present as typeof fetch))
      .resolves.toEqual(compiledExperimentFixture());
    await expect(readCompiledExperiment(experimentId, "csrf-token", missing as typeof fetch))
      .resolves.toBeNull();
    expect(present).toHaveBeenCalledWith(
      `/api/owner/experiments/${experimentId}/compiled`,
      expect.objectContaining({ method: "GET", credentials: "same-origin", cache: "no-store" }),
    );
  });

  it("accepts a stored compiled rule using the backend ANY match mode", async () => {
    const stored = compiledExperimentFixture();
    stored.compiled_protocol.rules.entry_rule.match = "ANY";
    const fetcher = vi.fn(async () => new Response(JSON.stringify(stored)));

    const result = await readCompiledExperiment(
      experimentId,
      "csrf-token",
      fetcher as typeof fetch,
    );

    expect(result?.compiled_protocol.rules.entry_rule.match).toBe("ANY");
    expect(compileRequestFixture().rules.entry_rule.match).toBe("ALL");
  });

  it("rejects malformed compiled authority claims", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      ...compiledExperimentFixture(),
      arm_state: "ARMED",
    })));

    await expect(readCompiledExperiment(experimentId, "csrf-token", fetcher as typeof fetch))
      .rejects.toEqual(expect.objectContaining({ status: 503 }));

    const unrelated = vi.fn(async () => new Response(JSON.stringify({
      ...compiledExperimentFixture(),
      experiment_id: "20000000-0000-4000-8000-000000000002",
    })));
    await expect(readCompiledExperiment(experimentId, "csrf-token", unrelated as typeof fetch))
      .rejects.toEqual(expect.objectContaining({ status: 503 }));
  });

  it("loads exact authorization lineage with shared owner-session headers", async () => {
    const status = experimentAuthorizationFixture();
    const fetcher = vi.fn(async () => new Response(JSON.stringify(status)));

    await expect(readExperimentAuthorization(
      experimentId,
      status.source_definition_hash,
      status.protocol_hash,
      "csrf-token",
      fetcher as typeof fetch,
    )).resolves.toEqual(status);
    expect(fetcher).toHaveBeenCalledWith(
      `/api/owner/experiments/${experimentId}/authorization`,
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      }),
    );
  });

  it("arms and disarms only the exact protocol revision", async () => {
    const initial = experimentAuthorizationFixture();
    const armed = experimentAuthorizationFixture("ARMED", 1);
    const disarmed = experimentAuthorizationFixture("DISARMED", 2);
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(armed)))
      .mockResolvedValueOnce(new Response(JSON.stringify(disarmed)));

    const armRequest = {
      schema_version: "v1" as const,
      source_definition_hash: initial.source_definition_hash,
      protocol_hash: initial.protocol_hash,
      expected_revision: 0,
    };
    await expect(armExperiment(
      experimentId,
      armRequest,
      "csrf-token",
      fetcher as typeof fetch,
    )).resolves.toEqual(armed);
    await expect(disarmExperiment(
      experimentId,
      { ...armRequest, expected_revision: 1 },
      "csrf-token",
      fetcher as typeof fetch,
    )).resolves.toEqual(disarmed);

    expect(fetcher.mock.calls[0][0]).toBe(`/api/owner/experiments/${experimentId}/arm`);
    expect(fetcher.mock.calls[1][0]).toBe(`/api/owner/experiments/${experimentId}/disarm`);
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual(armRequest);
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({
      ...armRequest,
      expected_revision: 1,
    });
  });

  it("rejects authorization from a different source or protocol", async () => {
    const mismatched = experimentAuthorizationFixture();
    mismatched.protocol_hash = "9".repeat(64);
    const fetcher = vi.fn(async () => new Response(JSON.stringify(mismatched)));

    await expect(readExperimentAuthorization(
      experimentId,
      "a".repeat(64),
      "b".repeat(64),
      "csrf-token",
      fetcher as typeof fetch,
    )).rejects.toEqual(expect.objectContaining({ status: 503 }));
  });

  it("rejects a mutation response that does not confirm the requested state", async () => {
    const initial = experimentAuthorizationFixture();
    const fetcher = vi.fn(async () => new Response(JSON.stringify(initial)));

    await expect(armExperiment(
      experimentId,
      {
        schema_version: "v1",
        source_definition_hash: initial.source_definition_hash,
        protocol_hash: initial.protocol_hash,
        expected_revision: 0,
      },
      "csrf-token",
      fetcher as typeof fetch,
    )).rejects.toEqual(expect.objectContaining({ status: 503 }));
  });

  it("rejects a mutation response that skips the requested next revision", async () => {
    const initial = experimentAuthorizationFixture();
    const skipped = experimentAuthorizationFixture("ARMED", 2);
    const fetcher = vi.fn(async () => new Response(JSON.stringify(skipped)));

    await expect(armExperiment(
      experimentId,
      {
        schema_version: "v1",
        source_definition_hash: initial.source_definition_hash,
        protocol_hash: initial.protocol_hash,
        expected_revision: 0,
      },
      "csrf-token",
      fetcher as typeof fetch,
    )).rejects.toEqual(expect.objectContaining({ status: 503 }));
  });
});
