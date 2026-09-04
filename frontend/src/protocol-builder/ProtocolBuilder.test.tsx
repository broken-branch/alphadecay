// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copy as publicCopy } from "../content/copy";
import { ProtocolBuilder } from "./ProtocolBuilder";
import { completeProtocolDraft, readyCurationFixture } from "./test-fixture";

const copy = publicCopy.protocolBuilder;
afterEach(cleanup);

describe("ProtocolBuilder", () => {
  it("uses plain controls and emits the typed request after explicit review", async () => {
    const user = userEvent.setup();
    const onComplete = vi.fn();
    render(
      <ProtocolBuilder
        curation={readyCurationFixture()}
        initialDraft={completeProtocolDraft()}
        onComplete={onComplete}
      />,
    );

    expect(screen.getByRole("heading", { name: copy.title })).toBeVisible();
    expect(screen.getByText(readyCurationFixture().protocol_fields.entry_rule!)).toBeVisible();
    expect(screen.getByText(copy.qualitySummary)).toBeVisible();
    expect(screen.queryByRole("textbox", { name: /json/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: copy.complete }));

    expect(onComplete).toHaveBeenCalledOnce();
    expect(onComplete.mock.calls[0][0].review_state).toBe("REVIEWED");
    expect(onComplete.mock.calls[0][0]).not.toHaveProperty("arm_state");
    expect(onComplete.mock.calls[0][0]).not.toHaveProperty("execution_eligible");
  });

  it("shows concise field-level issues and emits nothing when incomplete", async () => {
    const user = userEvent.setup();
    const onComplete = vi.fn();
    render(<ProtocolBuilder curation={readyCurationFixture()} onComplete={onComplete} />);

    await user.click(screen.getByRole("button", { name: copy.complete }));

    expect(screen.getByRole("alert")).toHaveTextContent(copy.missingSummary);
    expect(screen.getAllByText(copy.issue.number).length).toBeGreaterThan(0);
    expect(screen.getAllByText(copy.issue.rule).length).toBeGreaterThan(0);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("blocks repeated submission while an owner compile is in progress", () => {
    render(
      <ProtocolBuilder
        curation={readyCurationFixture()}
        initialDraft={completeProtocolDraft()}
        onComplete={vi.fn()}
        submitting
      />,
    );

    const button = screen.getByRole("button", { name: copy.completing });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });

  it("contains no network, persistence, order, arm, or thick-border path", () => {
    const source = ["ProtocolBuilder.tsx", "builder.ts", "types.ts"]
      .map((file) => readFileSync(`frontend/src/protocol-builder/${file}`, "utf8"))
      .join("\n");
    const styles = readFileSync("frontend/src/protocol-builder/protocol-builder.css", "utf8");
    for (const forbidden of [
      "fetch(", "localStorage", "sessionStorage", "indexedDB", "document.cookie",
      "/api/", "submitOrder", "armAutomation",
    ]) expect(source).not.toContain(forbidden);
    expect(styles).not.toMatch(/gradient\s*\(/i);
    expect(styles).not.toMatch(/border-left:\s*[2-9]/);
    expect(styles).toContain("@media (max-width: 620px)");
    expect(styles).toContain("prefers-reduced-motion");
  });
});
