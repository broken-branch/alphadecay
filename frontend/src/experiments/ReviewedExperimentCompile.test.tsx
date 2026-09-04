import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copy as publicCopy } from "../content/copy";
import {
  buildReviewedExecutableProtocolRequest,
} from "../protocol-builder";
import { completeProtocolDraft, readyCurationFixture } from "../protocol-builder/test-fixture";
import { compiledExperimentFixture } from "./compiled-test-fixture";
import type { CompileExperimentRequest } from "./compiled-experiment-contracts";
import { ReviewedExperimentRegistry } from "./ReviewedExperimentRegistry";
import {
  reviewedExperimentClient,
  ReviewedExperimentRequestError,
} from "./reviewed-registry-api";
import type { ReviewedExperimentClient } from "./reviewed-registry-api";
import type { ReviewedExperimentDefinition } from "./reviewed-registry-contracts";

vi.mock("../protocol-builder", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../protocol-builder")>();
  return {
    ...actual,
    ProtocolBuilder: ({ onComplete, submitting }: {
      onComplete: (value: unknown) => void;
      submitting: boolean;
    }) => (
      <button
        type="button"
        disabled={submitting}
        onClick={() => {
          const built = buildReviewedExecutableProtocolRequest(
            readyCurationFixture(),
            completeProtocolDraft(),
          );
          if (built.ok) onComplete(built.value);
        }}
      >
        {publicCopy.experiment.registry.compileAction}
      </button>
    ),
  };
});

const copy = publicCopy.experiment.registry;

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

afterEach(cleanup);

describe("reviewed experiment compile workflow", () => {
  it("binds the selected source hash and shows the immutable off state", async () => {
    const user = userEvent.setup();
    let receivedId = "";
    let receivedRequest: CompileExperimentRequest | null = null;
    const compile = vi.fn(async (...args: Parameters<ReviewedExperimentClient["compile"]>) => {
      [receivedId, receivedRequest] = args;
      return compiledExperimentFixture();
    });
    const client = {
      ...reviewedExperimentClient,
      readCompiled: vi.fn(async () => null),
      readAuthorization: vi.fn(async () => ({
        schema_version: "v1" as const,
        experiment_id: reviewedExperiment().experiment_id,
        source_definition_hash: reviewedExperiment().definition_hash,
        protocol_hash: compiledExperimentFixture().protocol_hash,
        authorization_revision: 0,
        authorization_state: "NOT_ARMED" as const,
        entry_authorized: false,
        existing_position_risk_management_preserved: true as const,
        runtime_state: "NOT_CONNECTED" as const,
        execution_eligible: false as const,
        paper_trading_only: true as const,
        authorization_event_hash: null,
        updated_at: null,
      })),
      compile,
    };
    render(
      <ReviewedExperimentRegistry
        experiments={[reviewedExperiment()]}
        csrfToken="csrf-token"
        client={client}
      />,
    );

    await user.click(await screen.findByRole("button", { name: copy.setupAction }));
    await user.click(screen.getByRole("button", { name: copy.compileAction }));

    expect(await screen.findByText(copy.compiledIntro)).toBeVisible();
    expect(screen.getAllByText(copy.notArmed)[0]).toBeVisible();
    expect(screen.getAllByText(copy.authorization.notConnected)[0]).toBeVisible();
    expect(screen.getByText(copy.authorization.executionUnavailable)).toBeVisible();
    expect(compile).toHaveBeenCalledOnce();
    expect(receivedId).toBe(reviewedExperiment().experiment_id);
    expect((receivedRequest as CompileExperimentRequest | null)?.source_definition_hash)
      .toBe(reviewedExperiment().definition_hash);
    expect(receivedRequest).not.toHaveProperty("curation");
  });

  it("keeps the reviewed source and setup open after a sanitized compile failure", async () => {
    const user = userEvent.setup();
    const source = reviewedExperiment();
    const client = {
      ...reviewedExperimentClient,
      readCompiled: vi.fn(async () => null),
      compile: vi.fn(async () => {
        throw new ReviewedExperimentRequestError(422);
      }),
    };
    render(
      <ReviewedExperimentRegistry experiments={[source]} csrfToken="csrf-token" client={client} />,
    );

    await user.click(await screen.findByRole("button", { name: copy.setupAction }));
    await user.click(screen.getByRole("button", { name: copy.compileAction }));

    expect(await screen.findByRole("alert")).toHaveTextContent(copy.compileFailed);
    expect(screen.getByText(source.original_thesis.source.content)).toBeVisible();
    expect(screen.getByRole("button", { name: copy.compileAction })).toBeEnabled();
  });
});
