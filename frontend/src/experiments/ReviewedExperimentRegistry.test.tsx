// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { copy as publicCopy } from "../content/copy";
import { curatedProtocolFixture } from "../strategy-protocol/test-fixture";
import { ReviewedExperimentRegistry } from "./ReviewedExperimentRegistry";
import { reviewedExperimentClient } from "./reviewed-registry-api";
import type { ReviewedExperimentDefinition } from "./reviewed-registry-contracts";

const copy = publicCopy.experiment.registry;

function definition(
  id: string,
  market: string,
  createdAt: string,
): ReviewedExperimentDefinition {
  const curation = curatedProtocolFixture();
  return {
    schema_version: "v1",
    experiment_id: id,
    version: 1,
    definition_hash: id.endsWith("1") ? "a".repeat(64) : "b".repeat(64),
    lifecycle_state: "REVIEWED",
    automation_state: "OFF",
    execution_eligible: false,
    paper_trading_only: true,
    original_thesis: { ...curation.intake, market_scope: market },
    reviewed_protocol: curation.protocol_fields,
    curation: { ...curation, intake: { ...curation.intake, market_scope: market } },
    created_at: createdAt,
  };
}

function client() {
  return { ...reviewedExperimentClient, readCompiled: async () => null };
}

afterEach(cleanup);

describe("ReviewedExperimentRegistry", () => {
  it("shows the newest saved source first and selects exact definitions", async () => {
    const user = userEvent.setup();
    const newest = definition(
      "10000000-0000-4000-8000-000000000001",
      "SPY",
      "2026-09-01T20:00:00Z",
    );
    const older = definition(
      "10000000-0000-4000-8000-000000000002",
      "QQQ",
      "2026-09-01T19:00:00Z",
    );
    render(<ReviewedExperimentRegistry experiments={[older, newest]} csrfToken="csrf" client={client()} />);

    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("SPY");
    expect(screen.getByRole("heading", { name: "SPY", level: 2 })).toBeVisible();
    expect(await screen.findByRole("heading", { name: copy.setupIncomplete })).toBeVisible();
    expect(screen.getByText(copy.automationOff)).toBeVisible();
    expect(screen.getByText(copy.noOrderAccess)).toBeVisible();

    await user.click(screen.getByRole("button", { name: /QQQ/ }));
    expect(screen.getByRole("heading", { name: "QQQ", level: 2 })).toBeVisible();
  });

  it("opens plain protocol setup while keeping the selected source visible", async () => {
    const user = userEvent.setup();
    const experiment = definition(
      "10000000-0000-4000-8000-000000000001",
      "SPY",
      "2026-09-01T20:00:00Z",
    );
    render(<ReviewedExperimentRegistry experiments={[experiment]} csrfToken="csrf" client={client()} />);

    await user.click(await screen.findByRole("button", { name: copy.setupAction }));

    expect(screen.getByText(experiment.original_thesis.source.content)).toBeVisible();
    expect(screen.getByRole("heading", { name: publicCopy.protocolBuilder.title })).toBeVisible();
    expect(screen.getByRole("button", { name: copy.compileAction })).toBeEnabled();
  });

  it("contains no performance inference, browser persistence, or decorated card effects", () => {
    const source = readFileSync("frontend/src/experiments/ReviewedExperimentRegistry.tsx", "utf8");
    const styles = readFileSync("frontend/src/experiments/reviewed-registry.css", "utf8");
    for (const forbidden of ["localStorage", "sessionStorage", "document.cookie", "profitLoss", "benchmark"] ) {
      expect(source).not.toContain(forbidden);
    }
    expect(styles).not.toMatch(/gradient\s*\(/i);
    expect(styles).not.toMatch(/border-left:\s*[2-9]/);
    expect(styles).toContain("@media (max-width: 760px)");
    expect(styles).toContain("prefers-reduced-motion");
  });
});
