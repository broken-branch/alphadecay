import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CompetitionPerformanceProofResponseSchema,
  type CompetitionPerformanceProofResponse,
} from "../contracts/v1";
import type { CompetitionRecordResponse } from "../competition-record/api";
import { copy } from "../content/copy";
import type { ExperimentWindowList } from "../experiments";
import { ReplayShell } from "./ReplayShell";
import type { OperationalState } from "./types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

type PerformancePoint = NonNullable<CompetitionPerformanceProofResponse["point"]>;

const guideKeys = [
  copy.keyboardGuide.previousScenarioKey,
  copy.keyboardGuide.nextScenarioKey,
  copy.keyboardGuide.previousTabKey,
  copy.keyboardGuide.nextTabKey,
  copy.keyboardGuide.tabKey,
  copy.keyboardGuide.questionKey,
  copy.keyboardGuide.escapeKey,
];

const completePoint: PerformancePoint = {
  schema_version: "v1",
  scheduled_for: "2026-09-04T14:30:00Z",
  attempted_at: "2026-09-04T14:31:00Z",
  measured_at: "2026-09-04T14:32:00Z",
  status: "COMPLETE",
  failure_code: null,
  current_equity_usd: "101250.50",
  account_equity_change_usd: "1250.50",
  account_equity_return_pct: "1.2505",
  reconciled_lifecycle_cashflow_usd: "210.25",
  open_position_liquidation_pnl_usd: "-45.75",
  simulator_limitations_code: "ALPACA_PAPER_SIMULATION",
};

function publishedProof(
  point: PerformancePoint,
  baseline: NonNullable<CompetitionPerformanceProofResponse["baseline_status"]>,
  predecessorHash: string | null = null,
): CompetitionPerformanceProofResponse {
  return {
    schema_version: "v1",
    publication_status: "PUBLISHED",
    baseline_status: baseline,
    published_at: "2026-09-04T14:36:00Z",
    point,
    linked_certificate_ids: [
      "00000000-0000-0000-0000-000000000001",
      "00000000-0000-0000-0000-000000000002",
    ],
    publication_hash: "a".repeat(64),
    predecessor_hash: predecessorHash,
  };
}

function publishedArchive(): CompetitionRecordResponse {
  const recordId = "d".repeat(64);
  return {
    schema_version: "v1",
    publication_status: "PUBLISHED",
    records: [{
      schema_version: "v1",
      kind: "NO_TRADE",
      public_record_id: recordId,
      occurred_at: "2026-08-31T14:00:00Z",
      published_at: "2026-08-31T14:01:00Z",
      projection_hash: "e".repeat(64),
      publication_hash: "f".repeat(64),
      predecessor_hash: null,
      payload: {
        schema_version: "v1",
        record_kind: "NO_TRADE",
        public_record_id: recordId,
        status: "NO_TRADE",
        reason_category: "STRATEGY_NOT_READY",
        decided_at: "2026-08-31T14:00:00Z",
        observed_at: "2026-08-31T14:00:30Z",
        paper_trading: true,
      },
    }],
  };
}

function notPublishedArchive(): CompetitionRecordResponse {
  return {
    schema_version: "v1",
    publication_status: "NOT_PUBLISHED",
    records: [],
  };
}

function frozenWindows(): ExperimentWindowList {
  return {
    schema_version: "v2",
    windows: [{
      schema_version: "v2",
      plan_version: 2,
      protocol: {
        schema_version: "v2",
        name: "SPY structural bullish beta pilot",
        summary: "Bullish direction fixed before the window; one defined-risk options vertical.",
      },
      frozen_at: "2026-09-02T18:15:00Z",
      decision_boundary: "2026-09-03T13:50:00Z",
      entry_window: {
        schema_version: "v2",
        opens_at: "2026-09-03T13:50:00Z",
        closes_at: "2026-09-03T14:25:00Z",
      },
      terminal_decision: {
        schema_version: "v2",
        outcome_code: "NO_TRADE",
        reason: "The option quote was too old.",
        decided_at: "2026-09-03T13:52:00Z",
      },
      lifecycle: null,
      status: "DECIDED",
      aborted_reason: null,
      tick_outcome_code: null,
      tick_outcome_text: null,
      collapsed_versions: [2],
    }],
  };
}

describe("public Replay shell", () => {
  it("shows that opening selection happened before the fixed Replay", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    await user.click(screen.getByRole("tab", { name: copy.navigation.run }));
    const openingChecks = screen.getByRole("heading", { name: copy.acquisition.title }).parentElement
      ?.parentElement;
    expect(openingChecks).toHaveTextContent(copy.acquisition.eventValue);
    expect(openingChecks).toHaveTextContent(copy.acquisition.triggerValue);
    expect(openingChecks).toHaveTextContent(copy.acquisition.directionValue);
    expect(openingChecks).toHaveTextContent(copy.acquisition.candidateCheckValue);
    expect(openingChecks).toHaveTextContent(copy.acquisition.selected);
    expect(screen.getByRole("combobox", { name: copy.positionReview.scenarioLabel })).toBeVisible();
  });

  it("links the brand to the position review", () => {
    render(<ReplayShell />);

    expect(screen.getByRole("link", { name: new RegExp(copy.brand.name) })).toHaveAttribute(
      "href",
      "#position-review",
    );
  });

  it("uses the full lockup on desktop and the mark source on mobile without visible text duplication", () => {
    render(<ReplayShell />);

    const brand = screen.getByRole("link", { name: new RegExp(copy.brand.name) });
    const pictures = brand.querySelectorAll("picture");
    expect(pictures).toHaveLength(2);
    for (const picture of pictures) {
      expect(picture.querySelector("img")).toHaveAttribute("data-brand-variant", "desktop-lockup");
      const mobileSource = picture.querySelector("source");
      expect(mobileSource).toHaveAttribute("media", "(max-width: 760px)");
      expect(mobileSource).toHaveAttribute("data-brand-variant", "mobile-mark");
      expect(mobileSource?.getAttribute("srcset")).toContain("data:image/svg+xml");
    }
    expect(brand.querySelectorAll(".brand__tagline")).toHaveLength(0);
    expect(brand.querySelectorAll(".sr-only")).toHaveLength(1);
    expect(screen.getAllByText(copy.brand.name)).toHaveLength(1);
  });

  it("defaults every mount to Dark", () => {
    render(<ReplayShell />);

    const toggle = screen.getByRole("switch", { name: copy.theme.label });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(toggle).toHaveAttribute("title", copy.theme.dark);
    expect(toggle).toHaveTextContent("");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("keeps a theme choice for the current mount only", async () => {
    const user = userEvent.setup();
    const first = render(<ReplayShell />);

    const toggle = screen.getByRole("switch", { name: copy.theme.label });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(toggle).toHaveAttribute("title", copy.theme.light);
    expect(toggle).toHaveTextContent("");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");

    first.unmount();
    render(<ReplayShell />);
    expect(screen.getByRole("switch", { name: copy.theme.label })).toHaveAttribute("aria-checked", "false");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("does not reserve bare letter keys for review navigation", async () => {
    const user = userEvent.setup();
    render(<ReplayShell initialScenario="THESIS_INTACT" />);
    const scenario = screen.getByRole("combobox", { name: copy.positionReview.scenarioLabel });

    await user.keyboard("hjklwasd");
    expect(scenario).toHaveValue("THESIS_INTACT");
    expect(screen.getByRole("switch", { name: copy.theme.label })).toHaveAttribute("title", copy.theme.dark);
  });

  it("moves through focused detail tabs with native arrow keys", async () => {
    const user = userEvent.setup();
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    render(<ReplayShell />);

    const evidence = screen.getByRole("tab", { name: copy.navigation.overview });
    evidence.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: copy.navigation.comparison })).toHaveFocus();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("keeps shortcut, native-key, and clicked tab selections fully visible", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);
    const tabList = screen.getByRole("tablist");
    Object.defineProperty(tabList, "scrollLeft", { configurable: true, value: 0, writable: true });
    vi.spyOn(tabList, "getBoundingClientRect").mockReturnValue({
      bottom: 40,
      height: 40,
      left: 20,
      right: 370,
      top: 0,
      width: 350,
      x: 20,
      y: 0,
      toJSON: () => ({}),
    });
    const clippedRect = {
      bottom: 40,
      height: 40,
      left: 304,
      right: 415,
      top: 0,
      width: 111,
      x: 304,
      y: 0,
      toJSON: () => ({}),
    };

    const evidence = screen.getByRole("tab", { name: copy.navigation.overview });
    const record = screen.getByRole("tab", { name: copy.navigation.record });
    vi.spyOn(record, "getBoundingClientRect").mockReturnValue(clippedRect);
    evidence.focus();
    await user.keyboard("{ArrowLeft}");
    expect(tabList.scrollLeft).toBe(45);

    record.focus();
    const activity = screen.getByRole("tab", { name: copy.navigation.run });
    vi.spyOn(activity, "getBoundingClientRect").mockReturnValue(clippedRect);
    await user.keyboard("{ArrowLeft}");
    expect(tabList.scrollLeft).toBe(90);

    const choices = screen.getByRole("tab", { name: copy.navigation.comparison });
    vi.spyOn(choices, "getBoundingClientRect").mockReturnValue(clippedRect);
    await user.click(choices);
    expect(tabList.scrollLeft).toBe(135);
  });

  it("ignores review keys in controls, editable content, dialogs, repeats, and modified events", async () => {
    const user = userEvent.setup();
    render(<ReplayShell initialScenario="THESIS_INTACT" />);
    const scenario = screen.getByRole("combobox", { name: copy.positionReview.scenarioLabel });

    scenario.focus();
    await user.keyboard("j");
    expect(scenario).toHaveValue("THESIS_INTACT");

    fireEvent.keyDown(screen.getByRole("link", { name: new RegExp(copy.brand.name) }), { key: "j" });
    fireEvent.keyDown(screen.getByRole("switch", { name: copy.theme.label }), { key: "j" });
    expect(scenario).toHaveValue("THESIS_INTACT");

    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    document.body.append(editable);
    fireEvent.keyDown(editable, { key: "j" });
    expect(scenario).toHaveValue("THESIS_INTACT");
    editable.remove();

    for (const control of [document.createElement("input"), document.createElement("textarea")]) {
      document.body.append(control);
      fireEvent.keyDown(control, { key: "j" });
      expect(scenario).toHaveValue("THESIS_INTACT");
      control.remove();
    }

    fireEvent.keyDown(document.body, { key: "j", ctrlKey: true });
    fireEvent.keyDown(document.body, { key: "j", altKey: true });
    fireEvent.keyDown(document.body, { key: "j", metaKey: true });
    fireEvent.keyDown(document.body, { key: "j", shiftKey: true });
    fireEvent.keyDown(document.body, { key: "j", repeat: true });
    expect(scenario).toHaveValue("THESIS_INTACT");
    fireEvent.keyDown(document.body, { key: "?", ctrlKey: true, shiftKey: true });
    expect(screen.queryByRole("dialog", { name: copy.ownerSettings.title })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: copy.ownerSettings.entry }));
    const settings = await screen.findByRole("dialog", { name: copy.ownerSettings.title });
    fireEvent.keyDown(screen.getByRole("button", { name: copy.ownerSettings.close }), {
      key: "?",
      shiftKey: true,
    });
    expect(settings).toBeInTheDocument();
    fireEvent.keyDown(settings, { key: "j" });
    expect(scenario).toHaveValue("THESIS_INTACT");
  });

  it("keeps Shortcuts inside Settings and returns focus when closed", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);
    const trigger = screen.getByRole("button", { name: copy.ownerSettings.entry });

    await user.click(trigger);
    const settings = await screen.findByRole("dialog", { name: copy.ownerSettings.title });
    expect(settings).toHaveAttribute("aria-modal", "true");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("heading", { name: copy.keyboardGuide.title })).toBeInTheDocument();
    for (const key of guideKeys) expect(screen.getByText(key)).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: copy.ownerSettings.title })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    fireEvent.keyDown(document.body, { key: "?", shiftKey: true });
    expect(await screen.findByRole("dialog", { name: copy.ownerSettings.title })).toBeInTheDocument();
  });

  it("opens the Replay keyboard guide with ? from a focused navigation button", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    const replay = screen.getByRole("button", { name: copy.productShell.replay });
    await user.click(replay);
    expect(replay).toHaveFocus();
    await user.keyboard("?");

    expect(
      await screen.findByRole("dialog", { name: copy.ownerSettings.title }),
    ).toBeVisible();
  });

  it("labels an unpublished competition record without implying a measured return", async () => {
    const user = userEvent.setup();
    const proof: CompetitionPerformanceProofResponse = {
      schema_version: "v1",
      publication_status: "NOT_PUBLISHED",
      baseline_status: null,
      published_at: null,
      point: null,
      linked_certificate_ids: [],
      publication_hash: null,
      predecessor_hash: null,
    };

    render(<ReplayShell proofLoader={async () => proof} />);

    await user.click(screen.getByRole("button", { name: copy.productShell.experiments }));
    const record = screen.getByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.notPublished }),
    ).toBeInTheDocument();
    expect(record).toHaveTextContent(copy.performance.sourceLabel);
    expect(screen.getByText(copy.performance.notPublishedDetail)).toBeInTheDocument();
    expect(record).not.toHaveTextContent(/%/);
  });

  it("shows proof API failure as unavailable rather than not published", async () => {
    const user = userEvent.setup();
    const unavailable = async () => {
      throw new Error("offline");
    };
    render(<ReplayShell proofLoader={unavailable} />);

    await user.click(screen.getByRole("button", { name: copy.productShell.experiments }));
    const record = screen.getByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.unavailable }),
    ).toBeInTheDocument();
    expect(record).toHaveTextContent(copy.performance.sourceLabel);
    expect(screen.queryByText(copy.performance.notPublished)).not.toBeInTheDocument();
  });

  it("shows every field in a complete clean competition proof", async () => {
    const user = userEvent.setup();
    render(
      <ReplayShell proofLoader={async () => publishedProof(completePoint, "BASELINE_CLEAN")} />,
    );

    const proof = await screen.findByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.complete }),
    ).toBeInTheDocument();
    expect(proof).toHaveTextContent(copy.performance.sourceLabel);
    expect(proof).toHaveTextContent(copy.performance.captureComplete);
    expect(proof).toHaveTextContent(copy.performance.baselineClean);
    expect(proof).toHaveTextContent(copy.performance.startingEquity);
    expect(proof).toHaveTextContent(copy.performance.startingEquityValue);
    expect(proof).toHaveTextContent("$101,250.50");
    expect(proof).toHaveTextContent("$1,250.50");
    expect(proof).toHaveTextContent("1.2505%");
    expect(proof).toHaveTextContent("$210.25");
    expect(proof).toHaveTextContent("-$45.75");
    expect(proof).toHaveTextContent(copy.performance.lifecycleCashflow);
    expect(proof).toHaveTextContent(copy.performance.liquidationPnl);
    expect(proof).toHaveTextContent("2026-09-04T14:30:00Z");
    expect(proof).toHaveTextContent("2026-09-04T14:31:00Z");
    expect(proof).toHaveTextContent("2026-09-04T14:32:00Z");
    expect(proof).toHaveTextContent("2026-09-04T14:36:00Z");
    const publicationHash = screen.getByText("a".repeat(64));
    expect(publicationHash).not.toBeVisible();
    await user.click(screen.getByText(copy.performance.technicalSummary));
    expect(publicationHash).toBeVisible();
    expect(proof).toHaveTextContent(copy.performance.firstPublication);
    expect(proof).toHaveTextContent(`${copy.performance.linkedCertificates}2`);
    expect(proof).toHaveTextContent(copy.performance.simulatorDetail);
  });

  it("preserves contract-limit money values to the micro-unit", async () => {
    const point: PerformancePoint = {
      ...completePoint,
      current_equity_usd: "999999999999.999999",
      account_equity_change_usd: "999999899999.999999",
      account_equity_return_pct: "999999899.999999999",
      reconciled_lifecycle_cashflow_usd: "+999999999999.999998",
      open_position_liquidation_pnl_usd: "-0.000001",
    };
    const parsedProof = CompetitionPerformanceProofResponseSchema.parse(
      publishedProof(point, "BASELINE_CLEAN"),
    );
    render(<ReplayShell proofLoader={async () => parsedProof} />);

    const proof = await screen.findByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.complete }),
    ).toBeInTheDocument();
    expect(proof).toHaveTextContent("$999,999,999,999.999999");
    expect(proof).toHaveTextContent("$999,999,899,999.999999");
    expect(proof).toHaveTextContent("999999899.999999999%");
    expect(proof).toHaveTextContent("+$999,999,999,999.999998");
    expect(proof).toHaveTextContent("-$0.000001");
  });

  it("announces proof loading and completion once, without extra detail", async () => {
    let resolveProof: (proof: CompetitionPerformanceProofResponse) => void = () => undefined;
    const pendingProof = new Promise<CompetitionPerformanceProofResponse>((resolve) => {
      resolveProof = resolve;
    });
    render(<ReplayShell proofLoader={() => pendingProof} />);

    await userEvent.setup().click(screen.getByRole("button", { name: copy.productShell.experiments }));
    const announcement = screen.getByRole("heading", { name: copy.performance.loading });
    expect(announcement).toHaveAttribute("aria-live", "polite");
    expect(announcement).toHaveAttribute("aria-atomic", "true");
    expect(screen.getAllByText(copy.performance.loading)).toHaveLength(1);

    await act(async () => {
      resolveProof(publishedProof(completePoint, "BASELINE_CLEAN"));
    });
    expect(announcement).toHaveTextContent(copy.performance.complete);
    expect(screen.getAllByText(copy.performance.complete)).toHaveLength(1);
    expect(announcement).not.toHaveTextContent(copy.performance.completeDetail);
  });

  it("labels a missing capture and leaves measured values unavailable", async () => {
    const point: PerformancePoint = {
      ...completePoint,
      attempted_at: "2026-09-04T14:36:00Z",
      measured_at: null,
      status: "MISSING",
      failure_code: "CAPTURE_NOT_STARTED",
      current_equity_usd: null,
      account_equity_change_usd: null,
      account_equity_return_pct: null,
      reconciled_lifecycle_cashflow_usd: null,
      open_position_liquidation_pnl_usd: null,
    };
    render(
      <ReplayShell
        proofLoader={async () => publishedProof(point, "BASELINE_CLEAN", "b".repeat(64))}
      />,
    );

    const proof = await screen.findByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.missing }),
    ).toBeInTheDocument();
    expect(proof).toHaveTextContent(copy.performance.captureMissing);
    expect(proof).toHaveTextContent(copy.performance.failureCaptureNotStarted);
    expect(proof).toHaveTextContent(copy.performance.notMeasured);
    expect(proof).toHaveTextContent(copy.performance.notAvailable);
    expect(proof).toHaveTextContent(copy.performance.linkedPublication);
    expect(proof).toHaveTextContent("b".repeat(64));
  });

  it("suppresses normalized values when the baseline is contaminated", async () => {
    const point: PerformancePoint = {
      ...completePoint,
      current_equity_usd: "99900",
      account_equity_change_usd: null,
      account_equity_return_pct: null,
    };
    render(
      <ReplayShell proofLoader={async () => publishedProof(point, "BASELINE_CONTAMINATED")} />,
    );

    const proof = await screen.findByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.contaminated }),
    ).toBeInTheDocument();
    expect(proof).toHaveTextContent(copy.performance.baselineContaminated);
    expect(proof).toHaveTextContent("$99,900.00");
    expect(proof).toHaveTextContent(copy.performance.contaminatedDetail);
    expect(proof).toHaveTextContent(copy.performance.notAvailable);
  });

  it("labels an unknown capture without inventing account values", async () => {
    const point: PerformancePoint = {
      ...completePoint,
      measured_at: null,
      status: "UNKNOWN",
      failure_code: "ACCOUNT_STATE_INCOMPLETE",
      current_equity_usd: null,
      account_equity_change_usd: null,
      account_equity_return_pct: null,
      reconciled_lifecycle_cashflow_usd: null,
      open_position_liquidation_pnl_usd: null,
    };
    render(<ReplayShell proofLoader={async () => publishedProof(point, "BASELINE_UNKNOWN")} />);

    const proof = await screen.findByRole("region", { name: copy.performance.label });
    expect(
      await screen.findByRole("heading", { name: copy.performance.unknown }),
    ).toBeInTheDocument();
    expect(proof).toHaveTextContent(copy.performance.captureUnknown);
    expect(proof).toHaveTextContent(copy.performance.baselineUnknownValue);
    expect(proof).toHaveTextContent(copy.performance.failureAccountIncomplete);
  });

  it("keeps Replay provenance visible while a visitor changes scenarios", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    expect(screen.getByRole("status")).toHaveTextContent(copy.provenance.banner);
    expect(
      screen.getAllByRole("heading", { name: copy.scenarios.THETA_TAKEOVER.answer }).length,
    ).toBeGreaterThan(0);
    await user.selectOptions(
      screen.getByRole("combobox", { name: copy.positionReview.scenarioLabel }),
      "CATALYST_BROKEN",
    );

    expect(screen.getByRole("status")).toHaveTextContent(copy.provenance.banner);
    expect(
      screen.getAllByRole("heading", { name: copy.scenarios.CATALYST_BROKEN.answer }).length,
    ).toBeGreaterThan(0);
    await user.click(screen.getByRole("tab", { name: copy.navigation.record }));
    expect(screen.getByRole("tabpanel")).toHaveTextContent(copy.alternatives.closeBroken);
  });

  it("shows the stale quote gate before any Replay action", async () => {
    const user = userEvent.setup();
    render(<ReplayShell initialScenario="STALE_QUOTE" />);

    expect(screen.getByRole("status")).toHaveTextContent(copy.provenance.banner);
    expect(screen.getByRole("combobox", { name: copy.positionReview.scenarioLabel })).toHaveValue(
      "STALE_QUOTE",
    );
    expect(screen.getByRole("heading", { name: copy.states.stale.heading })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: copy.alternatives.noAction })).not.toHaveTextContent(
      copy.positionReview.noOrder,
    );
    await user.click(screen.getByRole("tab", { name: copy.navigation.record }));
    expect(screen.getAllByText(copy.alternatives.unavailable)).toHaveLength(3);
    await user.click(screen.getByRole("tab", { name: copy.navigation.run }));
    expect(screen.getByRole("tabpanel", { name: copy.navigation.run })).toHaveTextContent(
      copy.run.certify,
    );
  });

  it("does not imply source attribution for fixture-only Replay state", () => {
    const { container } = render(<ReplayShell initialScenario="CATALYST_BROKEN" />);

    expect(container).not.toHaveTextContent(/\b(?:attributed|cited|evidence|source)\b/i);
  });

  it("opens with the decision and moves the saved thesis to its own section", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    const decision = screen.getByRole("region", { name: copy.alternatives.roll });
    const changed = screen.getByRole("region", { name: copy.scenarios.THETA_TAKEOVER.answer });
    const exposure = screen.getByRole("region", { name: copy.exposure.title });
    expect(decision).toHaveTextContent(copy.scenarios.THETA_TAKEOVER.decision);
    expect(changed).toHaveTextContent(copy.scenarios.THETA_TAKEOVER.summary);
    expect(exposure).toHaveTextContent(copy.positionReview.tradePlan);
    expect(exposure).toHaveTextContent(copy.positionReview.positionNow);
    expect(exposure).toHaveTextContent(copy.positionReview.afterRoll);
    expect(exposure).toHaveTextContent(copy.exposure.noBrokerOutcome);
    expect(exposure).toHaveTextContent("+55");
    expect(exposure).toHaveTextContent("-$5");
    expect(exposure).toHaveTextContent("-$16");
    expect(screen.getAllByLabelText(`15 ${copy.positionReview.dteLong}`).length).toBeGreaterThan(0);
    expect(screen.queryByRole("region", { name: copy.thesis.openingTitle })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
    const openingThesis = screen.getByRole("region", { name: copy.thesis.openingTitle });
    expect(openingThesis).toHaveTextContent(copy.acquisition.openingThesisValue);
    expect(openingThesis).not.toHaveTextContent(copy.scenarios.THETA_TAKEOVER.name);
    expect(openingThesis).toHaveTextContent(copy.thesis.invalidationValue);
  });

  it("shows the sample acquisition, autonomous cycle, provenance, and option context", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    await user.click(screen.getByRole("tab", { name: copy.navigation.run }));
    const acquisition = screen.getByRole("region", { name: copy.acquisition.title });
    expect(acquisition).toHaveTextContent(copy.acquisition.eventValue);
    expect(acquisition).toHaveTextContent(copy.acquisition.structureValue);
    expect(acquisition).toHaveTextContent(copy.acquisition.selected);
    expect(screen.getByRole("region", { name: copy.market.title })).toHaveTextContent(
      copy.market.noOrderState,
    );

    await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
    const evidence = screen.getByRole("region", { name: copy.provenanceDetail.title });
    expect(evidence).toHaveTextContent(copy.provenanceDetail.sourceValue);
    expect(evidence).toHaveTextContent(copy.provenanceDetail.weakenedClassification);

    await user.click(screen.getByRole("tab", { name: copy.navigation.record }));
    const cycle = screen.getByRole("region", { name: copy.autonomy.title });
    expect(cycle).not.toBeVisible();
    expect(screen.getByText(copy.run.technologyTitle)).not.toBeVisible();
    await user.click(screen.getByText(copy.certificate.howChecked));
    expect(cycle).toHaveTextContent(copy.autonomy.disarmed);
    expect(cycle).toBeVisible();
    expect(cycle).toHaveTextContent(copy.autonomy.emptyBookRouteValue);
    expect(cycle).toHaveTextContent(copy.autonomy.managedPositionRouteValue);
  });

  it("keeps one prospective thesis separate from every later outcome", async () => {
    const user = userEvent.setup();
    for (const scenario of [
      "THESIS_INTACT",
      "THETA_TAKEOVER",
      "CATALYST_BROKEN",
      "STALE_QUOTE",
    ] as const) {
      const view = render(<ReplayShell initialScenario={scenario} />);
      await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
      const openingThesis = screen.getByRole("region", { name: copy.thesis.openingTitle });
      expect(openingThesis).toHaveTextContent(copy.acquisition.openingThesisValue);
      expect(openingThesis).not.toHaveTextContent(copy.scenarios[scenario].name);
      view.unmount();
    }
  });

  it("shows review timing and concrete replacement terms with the roll", () => {
    render(<ReplayShell initialScenario="THETA_TAKEOVER" />);

    const decision = screen.getByRole("region", { name: copy.alternatives.roll });
    expect(decision).toHaveTextContent(copy.positionReview.sampleAssessedAt);
    expect(decision).toHaveTextContent(copy.positionReview.quoteAge);
    expect(decision).toHaveTextContent(copy.positionReview.reviewBy);
    expect(decision).toHaveTextContent(copy.positionReview.urgencySoon);

    const replacement = screen.getByText(copy.positionReview.replacementTerms);
    const exposure = screen.getByRole("region", { name: copy.exposure.title });
    expect(replacement).toBeInTheDocument();
    expect(exposure).toHaveTextContent("25 Sep");
    expect(exposure).toHaveTextContent("$130 / $135");
    expect(exposure).toHaveTextContent("$0.10 net debit per share");
    expect(exposure).toHaveTextContent("$490");
  });

  it("reserves Broken for thesis invalidation and gives mismatch scores a scale", async () => {
    const user = userEvent.setup();
    const theta = render(<ReplayShell initialScenario="THETA_TAKEOVER" />);
    await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
    const thetaEvidence = screen.getByRole("tabpanel", { name: copy.navigation.comparison });
    expect(thetaEvidence).toHaveTextContent(copy.thesis.outsidePlan);
    expect(thetaEvidence).not.toHaveTextContent(copy.thesis.broken);
    expect(thetaEvidence).toHaveTextContent(copy.drift.scale);
    expect(thetaEvidence).toHaveTextContent(`60${copy.drift.scoreScale}`);

    theta.unmount();
    render(<ReplayShell initialScenario="CATALYST_BROKEN" />);
    await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
    expect(screen.getByRole("tabpanel", { name: copy.navigation.comparison })).toHaveTextContent(
      copy.thesis.broken,
    );
  });

  it("marks stale readings wherever a trader sees the measured position", async () => {
    const user = userEvent.setup();
    render(<ReplayShell initialScenario="STALE_QUOTE" />);

    expect(screen.getByRole("region", { name: copy.positionReview.selectedPosition })).toHaveTextContent(
      `${copy.positionReview.quoteAge} ${copy.positionReview.separator} 45 seconds`,
    );
    const exposure = screen.getByRole("region", { name: copy.exposure.title });
    expect(exposure).toHaveTextContent(
      `${copy.positionReview.lastObserved} ${copy.positionReview.separator} 45 seconds ${copy.positionReview.oldSuffix}`,
    );
    await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
    expect(screen.getByRole("columnheader", { name: copy.positionReview.evidenceLastObserved })).toBeInTheDocument();
  });

  it("keeps the actual competition record separate from the sample review", async () => {
    const user = userEvent.setup();
    render(
      <ReplayShell proofLoader={async () => publishedProof(completePoint, "BASELINE_CLEAN")} />,
    );

    const proof = await screen.findByRole("region", { name: copy.performance.label });
    expect(screen.getByRole("button", { name: copy.productShell.experiments })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByRole("tabpanel", { name: copy.navigation.overview })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: copy.productShell.replay }));
    const evidence = screen.getByRole("tabpanel", { name: copy.navigation.overview });
    expect(proof).not.toBeInTheDocument();
    expect(evidence).toBeVisible();
  });

  it("opens a published competition timeline without mixing in Replay", async () => {
    const user = userEvent.setup();
    render(<ReplayShell archiveLoader={async () => publishedArchive()} />);

    await waitFor(() => expect(
      screen.getByRole("button", { name: copy.productShell.experiments }),
    ).toHaveAttribute("aria-current", "page"));
    expect(screen.getByRole("heading", { name: copy.gateway.competitionTitle })).toBeVisible();
    expect(screen.getByRole("link", { name: copy.productShell.viewCompetitionExperiment })).toHaveAttribute(
      "href",
      "#competition-experiment-workspace",
    );
    expect(
      await screen.findByRole("heading", { name: copy.experiment.adapter.noTradeName }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: copy.experiment.status.rejected })).toBeVisible();
    expect(screen.queryByText(copy.provenance.banner)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: copy.productShell.replay }));
    expect(screen.getByText(copy.provenance.banner)).toBeVisible();
    expect(screen.queryByRole("heading", { name: copy.experiment.adapter.noTradeName })).not.toBeInTheDocument();
  });

  it("shows every frozen window on the logged-out Experiments page", async () => {
    render(
      <ReplayShell
        archiveLoader={async () => notPublishedArchive()}
        experimentWindowsLoader={async () => frozenWindows()}
      />,
    );

    expect(await screen.findByRole("heading", { name: copy.experiment.windows.title })).toBeVisible();
    const timeline = screen.getByRole("list", { name: copy.experiment.windows.timelineLabel });
    expect(timeline).toHaveTextContent("SPY structural bullish beta pilot");
    expect(timeline).toHaveTextContent(copy.experiment.windows.noTrade);
    expect(timeline).toHaveTextContent("The option quote was too old.");
    expect(screen.queryByText(copy.experiment.history.notPublished)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: copy.productShell.openReplay })).toBeVisible();
  });

  it("waits for the competition record before choosing the first view", async () => {
    let resolveArchive: (archive: CompetitionRecordResponse) => void = () => undefined;
    const pendingArchive = new Promise<CompetitionRecordResponse>((resolve) => {
      resolveArchive = resolve;
    });
    render(
      <ReplayShell
        archiveLoader={() => pendingArchive}
        experimentWindowsLoader={async () => ({ schema_version: "v2", windows: [] })}
      />,
    );

    expect(screen.getByRole("heading", { name: copy.competitionRecord.loading })).toBeVisible();
    expect(screen.getByRole("button", { name: copy.productShell.experiments })).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.getByRole("button", { name: copy.productShell.replay })).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.queryByText(copy.provenance.banner)).not.toBeInTheDocument();

    await act(async () => resolveArchive(notPublishedArchive()));
    expect(screen.getByRole("button", { name: copy.productShell.experiments })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("heading", { name: copy.productShell.landing.recordPending })).toBeVisible();
    expect(screen.getByRole("button", { name: copy.productShell.openReplay })).toBeVisible();
    expect(screen.queryByText(copy.provenance.banner)).not.toBeInTheDocument();
  });

  it("uses the healthy empty archive instead of an earlier performance response", async () => {
    render(
      <ReplayShell
        archiveLoader={async () => notPublishedArchive()}
        proofLoader={async () => publishedProof(completePoint, "BASELINE_CLEAN")}
      />,
    );

    const experiments = screen.getByRole("button", { name: copy.productShell.experiments });
    await waitFor(() => expect(experiments).toHaveAttribute("aria-current", "page"));
    expect(screen.queryByText(copy.provenance.banner)).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: copy.performance.label })).not.toBeInTheDocument();
  });

  it("never changes the selected view after the visitor interacts", async () => {
    const user = userEvent.setup();
    let resolveArchive: (archive: CompetitionRecordResponse) => void = () => undefined;
    const pendingArchive = new Promise<CompetitionRecordResponse>((resolve) => {
      resolveArchive = resolve;
    });
    render(<ReplayShell archiveLoader={() => pendingArchive} />);

    const demo = screen.getByRole("button", { name: copy.productShell.replay });
    await user.click(demo);
    expect(demo).toHaveAttribute("aria-current", "page");

    await act(async () => resolveArchive(publishedArchive()));
    expect(demo).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("heading", { name: copy.competitionRecord.noTrade })).not.toBeInTheDocument();
    expect(screen.getByText(copy.provenance.banner)).toBeVisible();
  });

  it("shows one unavailable message in the competition record view", async () => {
    const user = userEvent.setup();
    const unavailable = async () => {
      throw new Error("unavailable");
    };
    render(<ReplayShell archiveLoader={unavailable} proofLoader={unavailable} />);

    await user.click(screen.getByRole("button", { name: copy.productShell.experiments }));

    expect(
      await screen.findAllByRole("heading", { name: copy.competitionRecord.unavailable }),
    ).toHaveLength(1);
    expect(screen.queryByRole("region", { name: copy.performance.label })).not.toBeInTheDocument();
  });

  it("opens the experiment record when no competition result is published", async () => {
    const proof: CompetitionPerformanceProofResponse = {
      schema_version: "v1",
      publication_status: "NOT_PUBLISHED",
      baseline_status: null,
      published_at: null,
      point: null,
      linked_certificate_ids: [],
      publication_hash: null,
      predecessor_hash: null,
    };
    render(<ReplayShell proofLoader={async () => proof} />);

    const experiments = screen.getByRole("button", { name: copy.productShell.experiments });
    await waitFor(() => expect(experiments).toHaveAttribute("aria-current", "page"));
    expect(screen.queryByText(copy.provenance.banner)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: copy.performance.label })).toHaveTextContent(
      copy.performance.notPublished,
    );
  });

  it("keeps the hosted Replay free of private account and provider controls", async () => {
    const user = userEvent.setup();
    render(
      <ReplayShell
        runtimeLoader={async () => ({
          schema_version: "v1",
          status: "ok",
          build: "review",
          runtime_mode: "REPLAY_ONLY",
        })}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.productShell.settings }));
    expect(screen.getByRole("button", { name: copy.productShell.settings })).toHaveClass(
      "workspace-nav__secondary",
    );
    expect(screen.getByRole("heading", { name: copy.gateway.setupTitle })).toBeVisible();
    expect(screen.getByRole("link", { name: copy.gateway.selfHostAction })).toHaveAttribute(
      "href",
      "https://github.com/broken-branch/alphadecay#readme",
    );
    expect(screen.queryByText(/live money/i)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: copy.gateway.publicTitle })).toBeVisible();
    expect(screen.queryByRole("button", { name: copy.gateway.ownerAction })).not.toBeInTheDocument();

    expect(screen.queryByRole("button", { name: copy.keyboardGuide.toggleGuide })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: copy.productShell.replay }));
    const shortcuts = screen.getByRole("button", { name: copy.keyboardGuide.toggleGuide });
    expect(shortcuts).toHaveClass("keyboard-trigger--icon");
    expect(shortcuts).not.toHaveTextContent(copy.ownerSettings.entry);
    await user.click(shortcuts);
    expect(screen.getByRole("dialog", { name: copy.ownerSettings.title })).toBeVisible();
    expect(screen.queryByLabelText(copy.ownerSettings.settingsCode)).not.toBeInTheDocument();
    expect(screen.getByText(copy.keyboardGuide.help)).toBeVisible();
  });

  it("keeps the API reference in the quiet footer", () => {
    render(<ReplayShell />);

    expect(screen.getByRole("link", { name: copy.footer.api })).toHaveAttribute("href", "/docs");
  });

  it("marks the Replay tabs for two-row phone layout", () => {
    render(<ReplayShell />);

    expect(screen.getByRole("tablist")).toHaveClass("tab-list--wrap-on-phone");
  });

  it("shows Replay provenance once without repeating badges through the review", () => {
    const { container } = render(<ReplayShell />);
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent(copy.provenance.banner);
    expect(container.querySelectorAll(".replay-provenance-badge")).toHaveLength(0);
  });

  it("keeps the decision and disabled order boundary in the decision record tab", async () => {
    const user = userEvent.setup();
    render(<ReplayShell initialScenario="THESIS_INTACT" />);

    await user.click(screen.getByRole("tab", { name: copy.navigation.record }));
    const panel = screen.getByRole("tabpanel", { name: copy.navigation.record });
    expect(panel).toHaveTextContent(copy.positionReview.targetAtEntry);
    expect(panel).toHaveTextContent(copy.positionReview.currentExposure);
    expect(panel).toHaveTextContent(copy.positionReview.expectedExposure);
    expect(panel).toHaveTextContent(copy.positionReview.noOrder);
    expect(panel).toHaveTextContent(copy.certificate.notApplicable);
    expect(panel).toHaveTextContent(copy.certificate.limitations);
  });

  it("fails closed when the canonical Replay response cannot be loaded", async () => {
    const unavailable = async () => {
      throw new Error("offline");
    };

    render(<ReplayShell replayLoader={unavailable} />);

    expect(
      await screen.findByRole("heading", { name: copy.states.unknown.heading }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: copy.decisionTrail.label })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(copy.provenance.banner);
  });

  it.each([
    ["COLD", copy.states.cold.heading],
    ["NO_POSITION", copy.states.noPosition.heading],
    ["STALE", copy.states.stale.heading],
    ["UNKNOWN", copy.states.unknown.heading],
    ["ASSIGNMENT", copy.states.assignment.heading],
    ["BLOCKED", copy.states.blocked.heading],
  ] satisfies Array<[Exclude<OperationalState, "READY">, string]>)(
    "explains the %s state without losing Replay provenance",
    (operationalState, heading) => {
      render(<ReplayShell operationalState={operationalState} />);

      expect(screen.getByRole("status")).toHaveTextContent(copy.provenance.banner);
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
      expect(
        screen.queryByRole("heading", { name: copy.certificate.title }),
      ).not.toBeInTheDocument();
    },
  );

  it("keeps What changed before Decision in the mobile DOM order", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));

    render(<ReplayShell />);

    expect(screen.getByRole("main")).toBeInTheDocument();
    const decision = screen.getByRole("region", { name: copy.alternatives.roll });
    const changed = screen.getByRole("region", { name: copy.scenarios.THETA_TAKEOVER.answer });
    expect(
      changed.compareDocumentPosition(decision) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByRole("combobox", { name: copy.positionReview.scenarioLabel })).toHaveValue(
      "THETA_TAKEOVER",
    );
    expect(document.querySelector("[data-layout='responsive']")).toBeInTheDocument();
  });

  it("exposes semantic controls, landmarks, table headings, and keyboard tabs", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    expect(
      screen.getByRole("heading", { level: 1, name: copy.positionReview.title }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.getByRole("contentinfo")).toHaveTextContent(copy.legal.privacyLink);
    expect(document.querySelector(".content-hint")).toHaveTextContent(copy.footer.keyboard);

    const evidence = screen.getByRole("tab", { name: copy.navigation.overview });
    evidence.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: copy.navigation.comparison })).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName(copy.navigation.comparison);
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("renders exactly one detail panel while switching tabs", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName(copy.navigation.overview);
    await user.click(screen.getByRole("tab", { name: copy.navigation.run }));
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName(copy.navigation.run);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("uses industry comparison labels and keeps the thesis-drift definition in its section", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    const changed = screen.getByRole("region", { name: copy.scenarios.THETA_TAKEOVER.answer });
    expect(changed).not.toHaveTextContent(copy.positionReview.thesisDriftDefinition);
    await user.click(screen.getByRole("tab", { name: copy.navigation.comparison }));
    const evidence = screen.getByRole("tabpanel", { name: copy.navigation.comparison });
    expect(evidence).toHaveTextContent(copy.positionReview.thesisDriftDefinition);
    expect(
      screen.getByRole("columnheader", { name: copy.positionReview.evidencePlan }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: copy.positionReview.evidenceCurrent }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: copy.positionReview.evidenceStatus }),
    ).toBeInTheDocument();
    expect(evidence).toHaveTextContent(copy.thesis.delta);
    expect(evidence).toHaveTextContent(copy.thesis.vega);
    expect(evidence).toHaveTextContent(copy.positionReview.driftScore);
  });

  it("opens storage-free Settings with its shortcut reference", async () => {
    const user = userEvent.setup();
    window.localStorage.clear();
    window.sessionStorage.clear();
    render(<ReplayShell />);

    const settings = screen.getByRole("button", { name: copy.ownerSettings.entry });
    expect(settings).toHaveAttribute("aria-keyshortcuts", "?");
    await user.click(settings);

    expect(
      await screen.findByRole("dialog", { name: copy.ownerSettings.title }),
    ).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("heading", { name: copy.keyboardGuide.title })).toBeInTheDocument();
    expect(await screen.findByLabelText(copy.ownerSettings.settingsCode)).toHaveFocus();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("opens Privacy and Important information from the footer", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    await user.click(screen.getByRole("button", { name: copy.legal.privacyLink }));
    expect(screen.getByRole("dialog", { name: copy.legal.privacyTitle })).toHaveTextContent(
      copy.legal.privacyReplayBody,
    );
    await user.click(screen.getByRole("button", { name: copy.legal.close }));
    await user.click(screen.getByRole("button", { name: copy.legal.importantLink }));
    expect(screen.getByRole("dialog", { name: copy.legal.importantTitle })).toHaveTextContent(
      copy.legal.importantAdviceBody,
    );
  });

  it("opens the paper-trading explanation from the header status", async () => {
    const user = userEvent.setup();
    render(<ReplayShell />);

    const trigger = screen.getByRole("button", { name: copy.provenance.paperOnly });
    await user.click(trigger);

    const information = screen.getByRole("dialog", { name: copy.legal.importantTitle });
    expect(information).toHaveTextContent(copy.legal.importantPaperBody);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it.each([
    ["THESIS_INTACT", copy.positionReview.ifHeld],
    ["THETA_TAKEOVER", copy.positionReview.afterRoll],
    ["CATALYST_BROKEN", copy.positionReview.afterClose],
    ["STALE_QUOTE", copy.positionReview.noActionHeading],
  ] as const)("labels the %s outcome column as %s", (scenario, label) => {
    render(<ReplayShell initialScenario={scenario} />);
    expect(screen.getByRole("region", { name: copy.exposure.title })).toHaveTextContent(label);
  });
});
