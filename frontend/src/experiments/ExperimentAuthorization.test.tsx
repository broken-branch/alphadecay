import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copy as publicCopy } from "../content/copy";
import { readyCurationFixture } from "../protocol-builder/test-fixture";
import {
  compiledExperimentFixture,
  experimentAuthorizationFixture,
} from "./compiled-test-fixture";
import { ReviewedExperimentRegistry } from "./ReviewedExperimentRegistry";
import {
  reviewedExperimentClient,
  ReviewedExperimentRequestError,
} from "./reviewed-registry-api";
import type { ReviewedExperimentClient } from "./reviewed-registry-api";
import type { ReviewedExperimentDefinition } from "./reviewed-registry-contracts";

const copy = publicCopy.experiment.registry.authorization;

function reviewedExperiment(): ReviewedExperimentDefinition {
  const curation = readyCurationFixture();
  return {
    schema_version: "v1",
    experiment_id: "10000000-0000-4000-8000-000000000001",
    version: 1,
    definition_hash: "a".repeat(64),
    lifecycle_state: "REVIEWED",
    automation_state: "OFF",
    execution_eligible: false,
    paper_trading_only: true,
    original_thesis: curation.intake,
    reviewed_protocol: curation.protocol_fields,
    curation,
    created_at: "2026-09-01T20:00:00Z",
  };
}

function client(
  overrides: Partial<ReviewedExperimentClient> = {},
): ReviewedExperimentClient {
  return {
    ...reviewedExperimentClient,
    readCompiled: vi.fn(async () => compiledExperimentFixture()),
    readAuthorization: vi.fn(async () => experimentAuthorizationFixture()),
    ...overrides,
  };
}

afterEach(cleanup);

describe("experiment authorization controls", () => {
  it("loads current state, revision, runtime, and exact source/protocol lineage", async () => {
    const user = userEvent.setup();
    render(
      <ReviewedExperimentRegistry
        experiments={[reviewedExperiment()]}
        csrfToken="csrf-token"
        client={client()}
      />,
    );

    const panel = await screen.findByRole("heading", { name: copy.title });
    const section = panel.closest("section");
    expect(section).not.toBeNull();
    expect(within(section!).getByText(copy.state.NOT_ARMED)).toBeVisible();
    expect(within(section!).getByText("0")).toBeVisible();
    expect(within(section!).getByText(copy.notConnected)).toBeVisible();
    await user.click(within(section!).getByText(copy.lineageSummary));
    expect(within(section!).getByText("a".repeat(64))).toBeVisible();
    expect(within(section!).getByText("b".repeat(64))).toBeVisible();
    expect(within(section!).getByText(copy.executionUnavailable)).toBeVisible();
  });

  it("arms the selected compiled experiment without claiming execution", async () => {
    const user = userEvent.setup();
    const arm = vi.fn(async () => experimentAuthorizationFixture("ARMED", 1));
    render(
      <ReviewedExperimentRegistry
        experiments={[reviewedExperiment()]}
        csrfToken="csrf-token"
        client={client({ arm })}
      />,
    );

    await user.click(await screen.findByRole("button", { name: copy.armAction }));

    expect((await screen.findAllByText(copy.state.ARMED))[0]).toBeVisible();
    expect(screen.getByText(copy.armedBoundary)).toBeVisible();
    expect(screen.getAllByText(copy.executionUnavailable)[0]).toBeVisible();
    expect(arm).toHaveBeenCalledWith(
      reviewedExperiment().experiment_id,
      {
        schema_version: "v1",
        source_definition_hash: "a".repeat(64),
        protocol_hash: "b".repeat(64),
        expected_revision: 0,
      },
      "csrf-token",
    );
  });

  it("disarms future entry while preserving risk-reducing position management", async () => {
    const user = userEvent.setup();
    const disarm = vi.fn(async () => experimentAuthorizationFixture("DISARMED", 2));
    render(
      <ReviewedExperimentRegistry
        experiments={[reviewedExperiment()]}
        csrfToken="csrf-token"
        client={client({
          readAuthorization: vi.fn(async () => experimentAuthorizationFixture("ARMED", 1)),
          disarm,
        })}
      />,
    );

    await user.click(await screen.findByRole("button", { name: copy.disarmAction }));

    expect((await screen.findAllByText(copy.state.DISARMED))[0]).toBeVisible();
    expect(screen.getByText(copy.managementBoundary)).toBeVisible();
    expect(disarm).toHaveBeenCalledWith(
      reviewedExperiment().experiment_id,
      expect.objectContaining({ expected_revision: 1 }),
      "csrf-token",
    );
  });

  it("reloads the latest state after a stale authorization revision", async () => {
    const user = userEvent.setup();
    const readAuthorization = vi.fn()
      .mockResolvedValueOnce(experimentAuthorizationFixture())
      .mockResolvedValueOnce(experimentAuthorizationFixture("ARMED", 2));
    render(
      <ReviewedExperimentRegistry
        experiments={[reviewedExperiment()]}
        csrfToken="csrf-token"
        client={client({
          readAuthorization,
          arm: vi.fn(async () => { throw new ReviewedExperimentRequestError(409); }),
        })}
      />,
    );

    await user.click(await screen.findByRole("button", { name: copy.armAction }));

    expect(await screen.findByRole("alert")).toHaveTextContent(copy.stale);
    expect(await screen.findByRole("button", { name: copy.disarmAction })).toBeVisible();
    expect(readAuthorization).toHaveBeenCalledTimes(2);
  });

  it("invalidates the shared owner session after an authorization read rejection", async () => {
    const onSessionRejected = vi.fn();
    render(
      <ReviewedExperimentRegistry
        experiments={[reviewedExperiment()]}
        csrfToken="csrf-token"
        client={client({
          readAuthorization: vi.fn(async () => { throw new ReviewedExperimentRequestError(403); }),
        })}
        onSessionRejected={onSessionRejected}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(copy.unavailable);
    expect(onSessionRejected).toHaveBeenCalledOnce();
  });

  it("uses the supplied owner CSRF token without browser persistence or direct cookie reads", () => {
    const component = readFileSync(
      "frontend/src/experiments/ReviewedExperimentRegistry.tsx",
      "utf8",
    );
    const clientSource = readFileSync(
      "frontend/src/experiments/reviewed-registry-api.ts",
      "utf8",
    );
    for (const source of [component, clientSource]) {
      expect(source).not.toMatch(/document\.cookie|localStorage|sessionStorage|indexedDB|caches\./);
    }
    expect(clientSource).toContain('"X-CSRF-Token": csrfToken');
    expect(copy.armedBoundary).toMatch(/runtime and order path remain disconnected/i);
  });
});
