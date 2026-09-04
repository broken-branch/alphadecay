// @ts-expect-error Node types are not part of the browser application build.
import { readFileSync } from "node:fs";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StrategyDraftRequestError } from "./api";
import type { StrategyDraftClient } from "./api";
import { ConnectedStrategyIntake } from "./ConnectedStrategyIntake";
import type { StrategyDraftResponse } from "./contracts";
import { copy as publicCopy } from "../content/copy";
import type { ReviewedExperimentDefinition } from "../experiments";
import { StrategyCurationRequestError } from "../strategy-protocol";
import type { StrategyCurationClient, StrategyCurationResponse } from "../strategy-protocol";
import { curatedProtocolFixture } from "../strategy-protocol/test-fixture";
import type { StrategyDraftRequest, StrategyIntakeFields } from "./types";

const copy = publicCopy.strategyIntake;

const initialValue: StrategyIntakeFields = {
  market: "SPY",
  thesis: "SPY may rise as earnings participation improves across sectors.",
  direction: "BULLISH",
  horizon: "WEEKS",
  evidence: "Earnings revisions broaden for several weeks.",
  invalidation: "SPY closes below its 50-day moving average.",
  maximumRiskUsd: "240",
  notes: "",
};

function responseFor(request: StrategyDraftRequest): StrategyDraftResponse {
  return {
    schema_version: "v1",
    status: "DRAFT_REVIEW_REQUIRED",
    curation_status: "NOT_CURATED",
    automation_state: "OFF",
    execution_eligible: false,
    intake: request,
    assumptions: ["USER_BRIEF_UNVERIFIED", "OPTIONS_ONLY", "PAPER_ONLY", "DEFINED_RISK_ONLY"],
    questions: [],
    required_before_promotion: [
      "MODEL_CURATION_REQUIRED",
      "EVIDENCE_REVIEW_REQUIRED",
      "RISK_REVIEW_REQUIRED",
      "OWNER_REVIEW_REQUIRED",
    ],
    structure_constraints: {
      options_required: true,
      defined_risk_required: true,
      naked_short_options_allowed: false,
      direction: "BULLISH",
      candidate_families: ["BULL_CALL_DEBIT_SPREAD"],
    },
    evidence_plan: {
      submitted_evidence: request.evidence,
      required_checks: [
        "VERIFY_THESIS_CLAIMS",
        "CHECK_MARKET_DATA_RECENCY",
        "CHECK_OPTION_LIQUIDITY",
        "CHECK_INVALIDATION_STATE",
      ],
    },
    risk_rules: {
      budget: request.risk_budget,
      loss_must_be_bounded: true,
      size_must_fit_budget: true,
    },
    exit_rules: {
      invalidation: request.invalidation,
      required_before_promotion: ["PROFIT_EXIT_REQUIRED", "LOSS_EXIT_REQUIRED", "TIME_EXIT_REQUIRED"],
    },
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConnectedStrategyIntake", () => {
  it("does not call the server until the owner chooses Review draft", () => {
    const client: StrategyDraftClient = { create: vi.fn() };
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "csrf-token" }}
        initialValue={initialValue}
        client={client}
      />,
    );

    expect(client.create).not.toHaveBeenCalled();
  });

  it("requires an authenticated owner session and CSRF token", async () => {
    const user = userEvent.setup();
    const client: StrategyDraftClient = { create: vi.fn() };
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: false, csrfToken: null }}
        initialValue={initialValue}
        client={client}
      />,
    );

    expect(screen.getByText(copy.form.ownerSignInNote)).toBeVisible();
    await user.click(screen.getByRole("button", { name: copy.form.createDraftOwner }));

    expect(client.create).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(copy.response.sessionRequired);
  });

  it("renders the verified review state, constraints, checks, and promotion work", async () => {
    const user = userEvent.setup();
    const create = vi.fn(async (request: StrategyDraftRequest) => responseFor(request));
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "csrf-token" }}
        initialValue={initialValue}
        client={{ create }}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));

    expect(await screen.findByRole("heading", { name: copy.response.title })).toBeVisible();
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ market_scope: "SPY" }), "csrf-token");
    for (const visibleText of [
      copy.response.statusValue,
      copy.response.curationValue,
      copy.response.automationValue,
      copy.response.executionValue,
      copy.response.bullCall,
      copy.response.boundedLoss,
      copy.response.optionLiquidity,
      copy.response.modelCuration,
      copy.response.ownerReview,
      copy.response.profitExit,
    ]) {
      expect(screen.getByText(visibleText)).toBeVisible();
    }
    expect(screen.queryByText("DRAFT_REVIEW_REQUIRED")).not.toBeInTheDocument();
    expect(screen.queryByText("MODEL_CURATION_REQUIRED")).not.toBeInTheDocument();
  });

  it("curates only after an explicit owner action and opens the editable protocol", async () => {
    const user = userEvent.setup();
    const draftCreate = vi.fn(async (request: StrategyDraftRequest) => responseFor(request));
    const curationCreate = vi.fn(async () => curatedProtocolFixture());
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "csrf-token" }}
        initialValue={initialValue}
        client={{ create: draftCreate }}
        curationClient={{ create: curationCreate }}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));
    expect(curationCreate).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: copy.response.curate }));

    expect(await screen.findByRole("heading", { name: publicCopy.strategyProtocol.header.title })).toBeVisible();
    expect(curationCreate).toHaveBeenCalledWith(
      {
        brief: expect.objectContaining({ market_scope: "SPY" }),
        protocol_fields: {
          entry_rule: null,
          no_trade_rule: null,
          profit_exit_rule: null,
          loss_exit_rule: null,
          time_exit_rule: null,
          invalidation_rules: ["SPY closes below its 50-day moving average."],
        },
      },
      "csrf-token",
    );
    expect(screen.getByText(publicCopy.strategyProtocol.state.noOrder)).toBeVisible();
  });

  it("rechecks edited rules and marks only a fully ready protocol reviewed", async () => {
    const user = userEvent.setup();
    const first = curatedProtocolFixture();
    const ready: StrategyCurationResponse = {
      ...first,
      classifications: {
        ...first.classifications,
        evidence: "READY",
        exit: "READY",
      },
      blocking_questions: [],
    };
    const curationCreate = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(ready);
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "csrf-token" }}
        initialValue={initialValue}
        client={{ create: async (request) => responseFor(request) }}
        curationClient={{ create: curationCreate } as StrategyCurationClient}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));
    await user.click(await screen.findByRole("button", { name: copy.response.curate }));
    await user.click(await screen.findByRole("button", { name: publicCopy.strategyProtocol.form.review }));

    expect(await screen.findByRole("button", {
      name: publicCopy.strategyProtocol.form.complete,
    })).toBeDisabled();
    expect(curationCreate).toHaveBeenCalledTimes(2);
    expect(curationCreate.mock.calls[1][0].protocol_fields.entry_rule).toBe(
      first.protocol_fields.entry_rule,
    );
  });

  it("saves the exact reviewed source only after the owner chooses save", async () => {
    const user = userEvent.setup();
    const first = curatedProtocolFixture();
    const ready: StrategyCurationResponse = {
      ...first,
      classifications: { ...first.classifications, evidence: "READY", exit: "READY" },
      blocking_questions: [],
    };
    const saved: ReviewedExperimentDefinition = {
      schema_version: "v1",
      experiment_id: "10000000-0000-4000-8000-000000000001",
      version: 1,
      definition_hash: "a".repeat(64),
      lifecycle_state: "REVIEWED",
      automation_state: "OFF",
      execution_eligible: false,
      paper_trading_only: true,
      original_thesis: ready.intake,
      reviewed_protocol: ready.protocol_fields,
      curation: ready,
      created_at: "2026-09-01T20:00:00Z",
    };
    const createExperiment = vi.fn(async () => saved);
    const onExperimentSaved = vi.fn();
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "csrf-token" }}
        initialValue={initialValue}
        client={{ create: async (request) => responseFor(request) }}
        curationClient={{ create: vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(ready) }}
        experimentClient={{ create: createExperiment }}
        onExperimentSaved={onExperimentSaved}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));
    await user.click(await screen.findByRole("button", { name: copy.response.curate }));
    await user.click(await screen.findByRole("button", { name: publicCopy.strategyProtocol.form.review }));
    expect(createExperiment).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", {
      name: publicCopy.strategyProtocol.form.save,
    }));

    expect(createExperiment).toHaveBeenCalledWith({
      original_thesis: ready.intake,
      reviewed_protocol: ready.protocol_fields,
      curation: ready,
    }, "csrf-token");
    expect(onExperimentSaved).toHaveBeenCalledWith(saved);
    expect(screen.getByRole("status")).toHaveTextContent(publicCopy.strategyProtocol.form.saved);
  });

  it("keeps the reviewed draft when curation authentication expires", async () => {
    const user = userEvent.setup();
    const onSessionRejected = vi.fn();
    const curationClient: StrategyCurationClient = {
      create: vi.fn(async () => {
        throw new StrategyCurationRequestError(403);
      }),
    };
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "expired-token" }}
        initialValue={initialValue}
        client={{ create: async (request) => responseFor(request) }}
        curationClient={curationClient}
        onSessionRejected={onSessionRejected}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));
    await user.click(await screen.findByRole("button", { name: copy.response.curate }));

    expect(await screen.findByRole("alert")).toHaveTextContent(copy.response.curationSessionExpired);
    expect(screen.getByRole("heading", { name: copy.response.title })).toBeVisible();
    expect(onSessionRejected).toHaveBeenCalledOnce();
  });

  it("turns owner authentication failures into one useful message", async () => {
    const user = userEvent.setup();
    const onSessionRejected = vi.fn();
    const client: StrategyDraftClient = {
      create: vi.fn(async () => {
        throw new StrategyDraftRequestError(403);
      }),
    };
    render(
      <ConnectedStrategyIntake
        session={{ authenticated: true, csrfToken: "expired-token" }}
        initialValue={initialValue}
        client={client}
        onSessionRejected={onSessionRejected}
      />,
    );

    await user.click(screen.getByRole("button", { name: copy.form.createDraft }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(copy.response.sessionExpired));
    expect(onSessionRejected).toHaveBeenCalledOnce();
  });

  it("contains no storage, order, autonomy, or arming path", () => {
    const source = [
      "api.ts",
      "ConnectedStrategyIntake.tsx",
      "StrategyIntake.tsx",
    ].map((name) => readFileSync(`frontend/src/strategy-intake/${name}`, "utf8")).concat(
      readFileSync("frontend/src/strategy-protocol/api.ts", "utf8"),
    ).join("\n");
    for (const forbidden of [
      "localStorage",
      "sessionStorage",
      "indexedDB",
      "document.cookie",
      "/api/owner/autonomy",
      "/api/internal/agent/run",
      "/v2/orders",
      "submitOrder",
      "armAutomation",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });
});
