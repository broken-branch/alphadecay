// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copy as publicCopy } from "../content/copy";
import { CuratedProtocolReview } from "./CuratedProtocolReview";
import type { StrategyCurationResponse } from "./contracts";
import { curatedProtocolFixture } from "./test-fixture";

const copy = publicCopy.strategyProtocol;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("CuratedProtocolReview", () => {
  it("renders bounded classifications and explicit non-execution boundaries", () => {
    const protocol = curatedProtocolFixture();
    render(<CuratedProtocolReview protocol={protocol} onReview={vi.fn()} />);

    expect(screen.getByRole("heading", { name: copy.header.title })).toBeVisible();
    for (const text of [
      copy.state.reviewRequired,
      copy.state.curated,
      copy.state.automationOff,
      copy.state.noOrder,
      copy.direction.bullish,
      copy.structure.bullCall,
      copy.readiness.ready,
      copy.readiness.needsInput,
      copy.readiness.conflictReview,
      copy.confidence.medium,
      copy.boundaries.paper,
      copy.boundaries.options,
      copy.boundaries.definedRisk,
      copy.boundaries.notArmed,
    ]) {
      expect(screen.getAllByText(text).length).toBeGreaterThan(0);
    }
    expect(screen.getByText(copy.questions.evidence)).toBeVisible();
    expect(screen.getByText(copy.questions.time)).toBeVisible();
    expect(screen.queryByText("CURATED_REVIEW_REQUIRED")).not.toBeInTheDocument();
    expect(screen.queryByText("BULL_CALL_DEBIT_SPREAD")).not.toBeInTheDocument();
  });

  it("does not display supporting excerpts or accept model prose fields", () => {
    const protocol = curatedProtocolFixture();
    render(<CuratedProtocolReview protocol={protocol} onReview={vi.fn()} />);

    expect(screen.getByText("1")).toBeVisible();
    expect(screen.queryByText(protocol.supporting_evidence[0].excerpt)).not.toBeInTheDocument();
  });

  it("emits exact protocol_fields as the owner edits and confirms them", async () => {
    const user = userEvent.setup();
    const onProtocolFieldsChange = vi.fn();
    const onReview = vi.fn();
    render(
      <CuratedProtocolReview
        protocol={curatedProtocolFixture()}
        onProtocolFieldsChange={onProtocolFieldsChange}
        onReview={onReview}
      />,
    );

    const entry = screen.getByRole("textbox", { name: copy.rules.entry.label });
    await user.clear(entry);
    await user.type(entry, "Enter after the reviewed close confirms direction.");
    const invalidation = screen.getByRole("textbox", { name: copy.rules.invalidation.label });
    await user.clear(invalidation);
    await user.type(invalidation, "SPY closes below support.\nEvidence breadth reverses.");
    await user.click(screen.getByRole("button", { name: copy.form.review }));

    expect(onProtocolFieldsChange).toHaveBeenLastCalledWith(expect.objectContaining({
      invalidation_rules: ["SPY closes below support.", "Evidence breadth reverses."],
    }));
    expect(onReview).toHaveBeenCalledWith(expect.objectContaining({
      entry_rule: "Enter after the reviewed close confirms direction.",
      invalidation_rules: ["SPY closes below support.", "Evidence breadth reverses."],
    }));
  });

  it("focuses the first missing rule and explains how to fix it", async () => {
    const user = userEvent.setup();
    const protocol: StrategyCurationResponse = {
      ...curatedProtocolFixture(),
      protocol_fields: {
        ...curatedProtocolFixture().protocol_fields,
        entry_rule: null,
        no_trade_rule: null,
      },
    };
    const onReview = vi.fn();
    render(<CuratedProtocolReview protocol={protocol} onReview={onReview} />);

    await user.click(screen.getByRole("button", { name: copy.form.review }));

    const entry = screen.getByRole("textbox", { name: copy.rules.entry.label });
    expect(entry).toHaveFocus();
    expect(entry).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent(copy.form.fixRules);
    expect(screen.getAllByText(copy.rules.required)).toHaveLength(2);
    expect(onReview).not.toHaveBeenCalled();
  });

  it("contains no persistence, provider, arming, order, or network path", () => {
    const source = ["CuratedProtocolReview.tsx", "contracts.ts"]
      .map((name) => readFileSync(`frontend/src/strategy-protocol/${name}`, "utf8"))
      .join("\n");
    for (const forbidden of [
      "localStorage",
      "sessionStorage",
      "indexedDB",
      "document.cookie",
      "fetch(",
      "WebSocket",
      "/api/owner/autonomy",
      "/api/internal/agent/run",
      "/v2/orders",
      "submitOrder",
      "armAutomation",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("uses a responsive quiet rule sheet without templated effects", () => {
    const styles = readFileSync("frontend/src/strategy-protocol/strategy-protocol.css", "utf8");
    expect(styles).not.toMatch(/gradient\s*\(/i);
    expect(styles).not.toMatch(/border-left:\s*[2-9]/);
    expect(styles).toContain("@media (max-width: 620px)");
    expect(styles).toContain("prefers-reduced-motion");
    expect(styles).toContain("grid-template-columns: 1fr");
  });
});
