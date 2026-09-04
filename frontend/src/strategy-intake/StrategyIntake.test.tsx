// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copy as publicCopy } from "../content/copy";
import { StrategyIntake } from "./StrategyIntake";
import { parseStrategyText } from "./parser";

const copy = publicCopy.strategyIntake;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("strategy text import", () => {
  it("accepts ordinary prose without requiring a file schema", () => {
    expect(parseStrategyText("A rate surprise could push bank shares higher.")).toEqual({
      thesis: "A rate surprise could push bank shares higher.",
    });
  });

  it("maps plain text and Markdown labels into editable fields", () => {
    expect(parseStrategyText([
      "# Symbol or market",
      "XLF",
      "",
      "## Thesis",
      "Bank shares may rise if the rate path stays higher.",
      "Direction: up",
      "Time window: weeks",
      "Evidence: Relative strength after the policy update.",
      "Invalidation: Banks lag the wider market after the update.",
      "Maximum risk: $180",
      "Notes: Use a spread with limited loss.",
    ].join("\n"))).toEqual({
      market: "XLF",
      thesis: "Bank shares may rise if the rate path stays higher.",
      direction: "BULLISH",
      horizon: "WEEKS",
      evidence: "Relative strength after the policy update.",
      invalidation: "Banks lag the wider market after the update.",
      maximumRiskUsd: "180",
      notes: "Use a spread with limited loss.",
    });
  });
});

describe("StrategyIntake", () => {
  it("starts with a direct form and a visible non-executable draft preview", () => {
    const { container } = render(<StrategyIntake />);

    expect(screen.getByRole("heading", { name: copy.intro.title })).toBeVisible();
    expect(screen.getByRole("textbox", { name: new RegExp(copy.form.thesis) })).toBeVisible();
    expect(screen.getByRole("heading", { name: copy.preview.title })).toBeVisible();
    expect(screen.getByText(copy.preview.status)).toBeVisible();
    expect(screen.getByText(copy.preview.notArmed)).toBeVisible();
    expect(screen.getByText(copy.preview.noOrder)).toBeVisible();
    expect(screen.getByText(copy.import.title)).toBeVisible();
    const form = container.querySelector(".strategy-form");
    const preview = container.querySelector(".strategy-intake__aside");
    if (!form || !preview) throw new Error("Strategy intake regions missing");
    expect(form.compareDocumentPosition(preview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows focused validation and does not emit an incomplete draft", async () => {
    const user = userEvent.setup();
    const onDraftReady = vi.fn();
    render(<StrategyIntake onDraftReady={onDraftReady} />);

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));

    expect(screen.getByRole("alert")).toHaveTextContent(copy.form.fixFields);
    const market = screen.getByRole("textbox", { name: new RegExp(copy.form.market) });
    expect(market).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(market).toHaveFocus();
    expect(onDraftReady).not.toHaveBeenCalled();
  });

  it("emits one normalized DRAFT protocol without execution authority", async () => {
    const user = userEvent.setup();
    const onDraftReady = vi.fn();
    render(<StrategyIntake onDraftReady={onDraftReady} />);

    await user.type(screen.getByRole("textbox", { name: new RegExp(copy.form.market) }), "  QQQ  ");
    await user.type(
      screen.getByRole("textbox", { name: new RegExp(copy.form.thesis) }),
      "  Large technology shares may recover after a broad selloff.  ",
    );
    await user.click(screen.getByRole("radio", { name: copy.form.bullish }));
    await user.selectOptions(screen.getByRole("combobox", { name: new RegExp(copy.form.horizon) }), "DAYS");
    await user.type(
      screen.getByRole("textbox", { name: new RegExp(copy.form.evidence) }),
      "  Price recovers the prior close on broad participation.  ",
    );
    await user.type(
      screen.getByRole("textbox", { name: new RegExp(copy.form.invalidation) }),
      "  The recovery fails and breadth weakens again.  ",
    );
    await user.type(screen.getByRole("spinbutton", { name: new RegExp(copy.form.risk) }), "200");
    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));

    expect(onDraftReady).toHaveBeenCalledWith({
      source: {
        kind: "PASTED_TEXT",
        content: "Large technology shares may recover after a broad selloff.",
      },
      market_scope: "QQQ",
      direction: "BULLISH",
      horizon: copy.form.days,
      evidence: ["Price recovers the prior close on broad participation."],
      invalidation: ["The recovery fails and breadth weakens again."],
      risk_budget: { max_loss_dollars: "200.00" },
    });
    expect(screen.getByText(copy.preview.readyTitle)).toBeVisible();
  });

  it("imports a labeled text note into fields for human review", async () => {
    const user = userEvent.setup();
    const onDraftReady = vi.fn();
    render(<StrategyIntake onDraftReady={onDraftReady} />);
    const content =
      "Symbol: IWM\nThesis: Small caps may strengthen.\nDirection: up\nTime window: days\nEvidence: Breadth improves.\nInvalidation: Breadth breaks.\nMaximum risk: 90";
    const file = new File([
      content,
    ], "idea.txt", { type: "text/plain" });

    fireEvent.change(screen.getByLabelText(copy.import.action), { target: { files: [file] } });

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(copy.import.loaded));
    expect(screen.getByRole("textbox", { name: new RegExp(copy.form.market) })).toHaveValue("IWM");
    expect(screen.getByRole("textbox", { name: new RegExp(copy.form.thesis) })).toHaveValue(
      "Small caps may strengthen.",
    );
    expect(screen.getByRole("radio", { name: copy.form.bullish })).toBeChecked();
    expect(screen.getByRole("spinbutton", { name: new RegExp(copy.form.risk) })).toHaveValue(90);
    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));
    expect(onDraftReady).toHaveBeenCalledWith(expect.objectContaining({
      source: { kind: "TEXT_FILE", content, filename: "idea.txt" },
      market_scope: "IWM",
    }));
    expect(onDraftReady.mock.calls[0]?.[0]).not.toHaveProperty("execution_eligible");
  });

  it("rejects unsupported and oversized imports before reading them", () => {
    render(<StrategyIntake />);
    const input = screen.getByLabelText(copy.import.action);

    fireEvent.change(input, { target: { files: [new File(["{}"], "idea.json")] } });
    expect(screen.getByRole("status")).toHaveTextContent(copy.import.unsupported);

    fireEvent.change(input, {
      target: { files: [new File(["x".repeat(20_001)], "idea.md")] },
    });
    expect(screen.getByRole("status")).toHaveTextContent(copy.import.tooLarge);
  });

  it("contains no storage, provider, or order call path", () => {
    const source = readFileSync("frontend/src/strategy-intake/StrategyIntake.tsx", "utf8");
    for (const forbidden of [
      "localStorage",
      "sessionStorage",
      "indexedDB",
      "caches.",
      "document.cookie",
      "fetch(",
      "WebSocket",
      "EventSource",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("uses quiet structural rules without gradients or thick edge accents", () => {
    const styles = readFileSync("frontend/src/strategy-intake/strategy-intake.css", "utf8");
    expect(styles).not.toMatch(/gradient\s*\(/i);
    expect(styles).not.toMatch(/border-left:\s*[2-9]/);
    expect(styles).not.toContain("order: -1");
    expect(styles).toContain("prefers-reduced-motion");
  });
});
