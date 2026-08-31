import { z } from "zod";
import {
  CompetitionPerformanceProofResponseSchema,
  type CompetitionPerformanceProofResponse,
} from "../contracts/v1";
import { copy } from "../content/copy";
import type { GreekExposure, ReplayAction, ReplayFixture, ScenarioId } from "./types";

const scenarioSchema = z.enum(["THESIS_INTACT", "THETA_TAKEOVER", "CATALYST_BROKEN", "STALE_QUOTE"]);
const actionSchema = z.enum(["HOLD", "CLOSE", "ROLL", "NO_ACTION"]);
const decimalSchema = z.string().regex(/^[+-]?\d+(?:\.\d+)?$/).transform(Number);
const hashSchema = z.string().regex(/^[a-f0-9]{64}$/);
const thesisCodeSchema = z.string().min(1);
const greekExposureSchema = z.object({
  delta: decimalSchema,
  gamma: decimalSchema,
  theta_per_day: decimalSchema,
  vega_per_iv_point: decimalSchema,
}).passthrough();
const dateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const timestampSchema = z.string().datetime({ offset: true }).refine(
  (value) => value.endsWith("Z"),
  "Replay timestamps must be UTC",
);
const presentationSchema = z.object({
  opening: z.object({
    underlying: z.string().min(1),
    reference_spot: decimalSchema,
    spread_kind: z.literal("BULL_CALL_SPREAD"),
    long_strike: decimalSchema,
    short_strike: decimalSchema,
    expiration_date: dateSchema,
    quantity: z.number().int().positive(),
    contract_multiplier: z.literal(100),
    entry_net_debit_per_share_usd: decimalSchema,
    maximum_loss: decimalSchema,
    approved_risk_cap: decimalSchema,
    delta_low: decimalSchema,
    delta_high: decimalSchema,
    vega_low: decimalSchema,
    vega_high: decimalSchema,
    maximum_daily_theta: decimalSchema,
    minimum_dte: z.number().int().nonnegative(),
    maximum_dte: z.number().int().nonnegative(),
    selection_state: z.literal("PRESELECTED_SAMPLE"),
  }).passthrough(),
  market: z.object({
    assessed_at: timestampSchema,
    review_by: timestampSchema.nullable(),
    urgency: z.enum(["ROUTINE", "SOON", "IMMEDIATE", "WAITING"]),
    quote_status: z.enum(["FRESH", "STALE"]),
    quote_age_seconds: z.number().int().nonnegative(),
    dte: z.number().int().nonnegative(),
    mark: decimalSchema.nullable(),
    bid: decimalSchema.nullable(),
    ask: decimalSchema.nullable(),
    liquidation_value: decimalSchema.nullable(),
    open_pnl: decimalSchema.nullable(),
    implied_volatility: decimalSchema.nullable(),
    iv_change_points: decimalSchema.nullable(),
  }).passthrough(),
  evidence: z.object({
    status: z.enum(["CLASSIFIED", "NOT_RUN"]),
    classifications: z.array(z.object({
      source_id: z.string().min(1),
      headline: z.string().min(1),
      observed_at: timestampSchema,
      event_code: z.string().min(1),
      relation: z.enum(["SUPPORTS", "CONTRADICTS", "NEUTRAL"]),
      materiality: z.number().int().min(1).max(3),
      relevance: decimalSchema,
      confidence: decimalSchema,
      source_tier: z.enum(["PRIMARY", "ORIGINAL_REPORTING", "SECONDARY"]),
      invalidates: z.boolean(),
    }).passthrough()),
  }).passthrough(),
  integration: z.object({
    fixture_validation: z.literal("COMPLETE"),
    deterministic_policy: z.literal("COMPLETE"),
    trading_api: z.literal("NOT_RUN"),
    mcp: z.literal("NOT_RUN"),
    model: z.literal("NOT_RUN"),
    cli: z.literal("NOT_RUN"),
    order_entry: z.literal("DISABLED"),
  }).passthrough(),
  roll: z.object({
    expiration_date: dateSchema,
    long_strike: decimalSchema,
    short_strike: decimalSchema,
    quantity: z.number().int().positive(),
    contract_multiplier: z.literal(100),
    estimated_net_debit_per_share_usd: decimalSchema.refine((value) => value >= 0, {
      message: "Expected a non-negative roll debit",
    }),
    resulting_maximum_loss: decimalSchema,
  }).passthrough().nullable(),
}).passthrough().superRefine((presentation, context) => {
  const { opening, market, evidence, roll } = presentation;
  const issue = (path: (string | number)[], message: string) => {
    context.addIssue({ code: "custom", path, message });
  };
  const close = (left: number, right: number) => Math.abs(left - right) < 1e-6;
  const openingWidth = opening.short_strike - opening.long_strike;
  if (
    opening.reference_spot <= 0
    || opening.long_strike <= 0
    || openingWidth <= 0
    || opening.entry_net_debit_per_share_usd <= 0
    || !close(
      opening.maximum_loss,
      opening.entry_net_debit_per_share_usd * opening.quantity * opening.contract_multiplier,
    )
    || opening.maximum_loss > opening.approved_risk_cap
    || opening.delta_low > opening.delta_high
    || opening.vega_low > opening.vega_high
    || opening.maximum_daily_theta <= 0
    || opening.minimum_dte > opening.maximum_dte
  ) {
    issue(["opening"], "Invalid Replay opening record");
  }

  const assessed = new Date(market.assessed_at);
  const assessedDay = Date.UTC(
    assessed.getUTCFullYear(),
    assessed.getUTCMonth(),
    assessed.getUTCDate(),
  );
  const expiryDay = Date.parse(`${opening.expiration_date}T00:00:00Z`);
  if (market.dte !== Math.round((expiryDay - assessedDay) / 86_400_000)) {
    issue(["market", "dte"], "Replay DTE does not match assessment date");
  }
  if (market.review_by !== null && Date.parse(market.review_by) <= assessed.getTime()) {
    issue(["market", "review_by"], "Replay review time must follow assessment");
  }
  if (evidence.classifications.some((item) => Date.parse(item.observed_at) > assessed.getTime())) {
    issue(["evidence", "classifications"], "Replay evidence cannot follow assessment");
  }
  if ((evidence.status === "CLASSIFIED") !== (evidence.classifications.length > 0)) {
    issue(["evidence"], "Invalid Replay evidence status");
  }

  const marketValues = [
    market.mark,
    market.bid,
    market.ask,
    market.liquidation_value,
    market.open_pnl,
    market.implied_volatility,
    market.iv_change_points,
  ];
  if (market.quote_status === "STALE") {
    if (marketValues.some((value) => value !== null) || market.urgency !== "WAITING") {
      issue(["market"], "Stale Replay market must not expose current values");
    }
  } else {
    const { bid, mark, ask, liquidation_value: value, open_pnl: pnl } = market;
    if (marketValues.some((item) => item === null)) {
      issue(["market"], "Fresh Replay market is incomplete");
    } else if (
      bid !== null
      && mark !== null
      && ask !== null
      && value !== null
      && pnl !== null
      && (
        bid > mark
        || mark > ask
        || !close(value, bid * opening.quantity * opening.contract_multiplier)
        || !close(pnl, value - opening.maximum_loss)
      )
    ) {
      issue(["market"], "Replay position arithmetic is inconsistent");
    }
  }

  if (roll !== null) {
    if (
      Date.parse(`${roll.expiration_date}T00:00:00Z`) <= expiryDay
      || roll.short_strike - roll.long_strike !== openingWidth
      || roll.quantity !== opening.quantity
      || !close(
        roll.resulting_maximum_loss,
        opening.maximum_loss
          + roll.estimated_net_debit_per_share_usd
          * roll.quantity
          * roll.contract_multiplier,
      )
      || roll.resulting_maximum_loss > opening.approved_risk_cap
    ) {
      issue(["roll"], "Invalid Replay roll record");
    }
  }
});

const replayResponseSchema = z.object({
  schema_version: z.literal("v1"),
  scenario: scenarioSchema,
  provenance_label: z.literal("REPLAY / FIXTURE DATA"),
  input_hash: hashSchema,
  assessment_hash: hashSchema,
  assessment: z.object({
    action: actionSchema,
    quality: z.enum(["COMPLETE", "MISSING", "STALE", "UNKNOWN"]),
    thesis_status: z.enum(["INTACT", "WEAKENING", "BROKEN", "UNKNOWN"]),
    rationale_code: z.string().min(1),
    execution_decision: z.enum([
      "NO_ACTION",
      "HOLD_CERTIFIED",
      "CLOSE_APPROVED",
      "CLOSE_RISK_ONLY",
      "ROLL_APPROVED",
    ]),
    actual_exposure: greekExposureSchema,
    components: z.object({
      evidence_drift: decimalSchema,
      exposure_mismatch: decimalSchema,
      time_pressure: decimalSchema,
      volatility_mismatch: decimalSchema,
      risk_utilization: decimalSchema,
    }).passthrough().nullable(),
    alternatives: z.array(z.object({
      action: actionSchema,
      eligible: z.boolean(),
    }).passthrough()).length(3),
    policy_hash: z.string().min(1),
  }).passthrough(),
  certificate: z.object({
    lineage_hash: hashSchema,
    account_role: z.literal("REPLAY"),
    thesis: z.object({
      thesis: z.object({
        underlying: z.string().min(1),
        thesis_code: thesisCodeSchema,
        invalidation_codes: z.array(z.literal("PRIMARY_CONTRADICTION")).min(1),
        intended_exposure: greekExposureSchema,
      }).passthrough(),
    }).passthrough(),
    expected_after_exposure: greekExposureSchema.nullable(),
    actual_after_exposure: z.null(),
    attempts: z.array(z.unknown()).length(0),
    execution_state: z.literal("NOT_REQUESTED"),
  }).passthrough(),
  presentation: presentationSchema,
  execution_enabled: z.literal(false),
}).strict();

type ReplayApiResponse = z.infer<typeof replayResponseSchema>;
type ApiGreekExposure = z.infer<typeof greekExposureSchema>;

function rounded(value: number): number {
  return Math.round(Math.max(0, value));
}

function presentExposure(exposure: ApiGreekExposure): GreekExposure {
  return {
    delta: exposure.delta,
    gamma: exposure.gamma,
    thetaPerDay: exposure.theta_per_day,
    vega: exposure.vega_per_iv_point,
  };
}

const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  const day = parsed.getUTCDate();
  const month = monthNames[parsed.getUTCMonth()];
  const hours = String(parsed.getUTCHours()).padStart(2, "0");
  const minutes = String(parsed.getUTCMinutes()).padStart(2, "0");
  return `${day} ${month} · ${hours}:${minutes} UTC`;
}

function formatDate(value: string): string {
  const [year, month, day] = value.split("-").map(Number);
  void year;
  return `${day} ${monthNames[month - 1]}`;
}

function formatMoney(value: number): string {
  return `$${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(2)}`;
}

function formatSignedMoney(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${formatMoney(Math.abs(value))}`;
}

function withLeadingNumber(template: string, value: number): string {
  return template.replace(/^\d+(?:\.\d+)?/, String(value));
}

function matchState(value: number, low: number, high: number): "ALIGNED" | "BROKEN" {
  return low <= value && value <= high ? "ALIGNED" : "BROKEN";
}

export function mergeReplayResponse(raw: unknown, fallback: ReplayFixture): ReplayFixture {
  const response: ReplayApiResponse = replayResponseSchema.parse(raw);
  if (response.scenario !== fallback.scenario) {
    throw new Error("REPLAY_SCENARIO_MISMATCH");
  }
  const selected = response.assessment.alternatives.filter(({ eligible }) => eligible);
  const blocked = response.assessment.action === "NO_ACTION";
  if (
    (blocked && selected.length !== 0)
    || (!blocked && (selected.length !== 1 || selected[0].action !== response.assessment.action))
  ) {
    throw new Error("REPLAY_ALTERNATIVE_MISMATCH");
  }
  if (
    blocked
    && (
      response.assessment.quality === "COMPLETE"
      || response.assessment.execution_decision !== "NO_ACTION"
      || response.assessment.components !== null
      || response.certificate.expected_after_exposure !== null
    )
  ) {
    throw new Error("REPLAY_BLOCKED_STATE_MISMATCH");
  }

  const states = new Map<ReplayAction, "SELECTED" | "REJECTED">(
    response.assessment.alternatives.map(({ action, eligible }) => [
      action,
      eligible ? "SELECTED" : "REJECTED",
    ]),
  );
  const exposure = response.assessment.actual_exposure;
  const intended = response.certificate.thesis.thesis.intended_exposure;
  const thesisCode = response.certificate.thesis.thesis.thesis_code;
  if (thesisCode !== "POST_EVENT_CONTINUATION_V1") {
    throw new Error("REPLAY_THESIS_CODE_MISMATCH");
  }
  const expectedAfter = response.certificate.expected_after_exposure;
  const components = response.assessment.components;
  const presentation = response.presentation;
  const opening = presentation.opening;
  const market = presentation.market;
  if (opening.underlying !== response.certificate.thesis.thesis.underlying) {
    throw new Error("REPLAY_PRESENTATION_MISMATCH");
  }
  const staleMarket = market.quote_status === "STALE";
  if ((response.assessment.quality === "COMPLETE") !== !staleMarket) {
    throw new Error("REPLAY_QUOTE_QUALITY_MISMATCH");
  }
  const evidenceCards = presentation.evidence.classifications.map((item) => ({
    sourceId: item.source_id,
    headline: item.headline,
    observedAt: formatTimestamp(item.observed_at),
    eventCode: item.event_code,
    relation: item.relation,
    materiality: item.materiality,
    relevance: item.relevance,
    confidence: item.confidence,
    sourceTier: item.source_tier,
  }));
  const evidenceState = presentation.evidence.status === "NOT_RUN"
    ? "UNKNOWN"
    : response.assessment.thesis_status === "BROKEN"
      ? "BROKEN"
      : response.assessment.thesis_status === "WEAKENING"
        ? "WEAKENED"
        : response.assessment.thesis_status === "UNKNOWN"
          ? "UNKNOWN"
          : "ALIGNED";
  const classification = evidenceState === "BROKEN"
    ? copy.provenanceDetail.contradictedClassification
    : evidenceState === "WEAKENED"
      ? copy.provenanceDetail.weakenedClassification
      : evidenceState === "UNKNOWN"
        ? copy.provenanceDetail.unknownClassification
        : copy.provenanceDetail.supportingClassification;
  const observedAt = evidenceCards[0]?.observedAt ?? formatTimestamp(
    new Date(new Date(market.assessed_at).getTime() - market.quote_age_seconds * 1000).toISOString(),
  );
  const roll = presentation.roll;

  return {
    ...fallback,
    action: response.assessment.action,
    invalidationCode: response.certificate.thesis.thesis.invalidation_codes[0],
    blockedState: response.assessment.quality === "STALE" ? "STALE" : undefined,
    intended: presentExposure(intended),
    position: {
      symbol: opening.underlying,
      strikes: `${formatMoney(opening.long_strike)} / ${formatMoney(opening.short_strike)}`,
      expiry: formatDate(opening.expiration_date),
      quantity: withLeadingNumber(fallback.position.quantity, opening.quantity),
    },
    measured: {
      ...presentExposure(exposure),
      dte: market.dte,
      maxLoss: opening.maximum_loss,
      dataAgeSeconds: market.quote_age_seconds,
    },
    expectedAfter: expectedAfter ? presentExposure(expectedAfter) : null,
    provenance: {
      ...fallback.provenance,
      observedAt,
      classification,
      support: evidenceState === "BROKEN" ? "CONTRADICTED" : evidenceState === "UNKNOWN" ? "UNKNOWN" : "SUPPORTED",
    },
    evidenceStatus: presentation.evidence.status,
    evidenceCards,
    market: {
      referenceSpot: formatMoney(opening.reference_spot),
      mark: market.mark === null ? fallback.market.mark : formatMoney(market.mark),
      bidAsk: market.bid === null || market.ask === null
        ? fallback.market.bidAsk
        : `${formatMoney(market.bid)} / ${formatMoney(market.ask)}`,
      entryPrice: `${formatMoney(opening.entry_net_debit_per_share_usd)} ${fallback.market.entryPrice.replace(/^\$?\d+(?:\.\d+)?\s*/, "")}`,
      liquidationValue: market.liquidation_value === null
        ? fallback.market.liquidationValue
        : formatMoney(market.liquidation_value),
      openPnl: market.open_pnl === null
        ? fallback.market.openPnl
        : formatSignedMoney(market.open_pnl),
      iv: market.implied_volatility === null
        ? fallback.market.iv
        : `${(market.implied_volatility * 100).toFixed(1)}%`,
      ivChange: market.iv_change_points === null
        ? fallback.market.ivChange
        : `${market.iv_change_points.toFixed(1)} pts`,
      riskCap: formatMoney(opening.approved_risk_cap),
      orderState: fallback.market.orderState,
    },
    reviewTiming: {
      assessedAt: formatTimestamp(market.assessed_at),
      quoteAgeSeconds: market.quote_age_seconds,
      reviewBy: market.review_by ? formatTimestamp(market.review_by) : fallback.reviewTiming.reviewBy,
      urgency: market.urgency,
    },
    rollProposal: roll ? {
      expiry: formatDate(roll.expiration_date),
      strikes: `${formatMoney(roll.long_strike)} / ${formatMoney(roll.short_strike)}`,
      quantity: withLeadingNumber(fallback.position.quantity, roll.quantity),
      estimatedCost: formatMoney(roll.estimated_net_debit_per_share_usd),
      resultingMaxLoss: formatMoney(roll.resulting_maximum_loss),
    } : undefined,
    comparison: [
      { key: "direction", measuredValue: `${exposure.delta > 0 ? "+" : ""}${exposure.delta}`, state: staleMarket ? "UNKNOWN" : matchState(exposure.delta, opening.delta_low, opening.delta_high) },
      { key: "volatility", measuredValue: `${exposure.vega_per_iv_point}`, state: staleMarket ? "UNKNOWN" : matchState(exposure.vega_per_iv_point, opening.vega_low, opening.vega_high) },
      { key: "horizon", measuredValue: `${market.dte}`, state: staleMarket ? "UNKNOWN" : matchState(market.dte, opening.minimum_dte, opening.maximum_dte) },
      { key: "risk", measuredValue: formatMoney(opening.maximum_loss), state: staleMarket ? "UNKNOWN" : opening.maximum_loss <= opening.approved_risk_cap ? "ALIGNED" : "BROKEN" },
      { key: "evidence", measuredValue: `${components ? rounded(components.evidence_drift) : 0}`, state: staleMarket ? "UNKNOWN" : evidenceState },
    ],
    drift: components ? [
      { key: "exposure", points: rounded(components.exposure_mismatch), quality: "FRESH" },
      { key: "volatility", points: rounded(components.volatility_mismatch), quality: "FRESH" },
      { key: "time", points: rounded(components.time_pressure), quality: "FRESH" },
      { key: "evidence", points: rounded(components.evidence_drift), quality: "FRESH" },
      { key: "risk", points: rounded(components.risk_utilization), quality: "FRESH" },
    ] : fallback.drift,
    alternatives: fallback.alternatives.map((alternative) => ({
      ...alternative,
      state: blocked ? "UNAVAILABLE" : states.get(alternative.action) ?? "UNAVAILABLE",
    })),
    lineage: {
      ...fallback.lineage,
      policyVersion: response.assessment.policy_hash,
      assessmentHash: response.assessment_hash,
      inputHash: response.input_hash,
    },
  };
}

export async function loadReplayFixture(
  scenario: ScenarioId,
  fallback: ReplayFixture,
  fetcher: typeof fetch = fetch,
): Promise<ReplayFixture> {
  const response = await fetcher(`/api/replays/${scenario}`, { method: "POST" });
  if (!response.ok) {
    throw new Error(`REPLAY_HTTP_${response.status}`);
  }
  return mergeReplayResponse(await response.json(), fallback);
}

export async function loadCompetitionProof(
  fetcher: typeof fetch = fetch,
): Promise<CompetitionPerformanceProofResponse> {
  const response = await fetcher("/api/proof");
  if (!response.ok) {
    throw new Error(`PROOF_HTTP_${response.status}`);
  }
  return CompetitionPerformanceProofResponseSchema.parse(await response.json());
}
