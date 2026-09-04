import { useEffect, useState, type ReactNode } from "react";
import { copy as publicCopy } from "../content/copy";
import type {
  ExperimentEvidence,
  ExperimentStatus,
  ExperimentValuePoint,
  ExperimentWorkspaceProps,
} from "./types";
import type {
  ExperimentMetricUnavailableReason,
  ExperimentPerformanceProjection,
} from "./experiment-performance-contracts";
import {
  experimentPerformanceClient,
  ExperimentPerformanceRequestError,
} from "./experiment-performance-api";
import "./experiment-workspace.css";

const copy = publicCopy.experiment;

type PositionEvent = NonNullable<ExperimentWorkspaceProps["position"]>["payload"]["events"][number];

const statusLabels: Record<ExperimentStatus, string> = {
  DRAFT: copy.status.draft,
  WATCHING: copy.status.watching,
  REJECTED: copy.status.rejected,
  OPEN: copy.status.open,
  CLOSED: copy.status.closed,
  BLOCKED: copy.status.blocked,
};

const evidenceLabels: Record<ExperimentEvidence["state"], string> = {
  SUPPORTS: copy.evidence.supporting,
  CONTRADICTS: copy.evidence.contradicting,
  NEUTRAL: copy.evidence.neutral,
  UNKNOWN: copy.evidence.unknown,
};

function formatMoney(value: string | number | null, signed = false): string {
  if (value === null) return copy.trade.notRecorded;
  const amount = Number(value);
  return new Intl.NumberFormat("en-US", {
    currency: "USD",
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
    signDisplay: signed && amount !== 0 ? "always" : "never",
    style: "currency",
  }).format(amount);
}

function formatDate(value: string | null): string {
  if (value === null) return copy.trade.notRecorded;
  return new Intl.DateTimeFormat("en-US", {
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    month: "short",
    timeZone: "UTC",
    timeZoneName: "short",
    year: "numeric",
  }).format(new Date(value));
}

function formatDecimal(value: string): string {
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 3 });
}

function formatSpread(position: NonNullable<ExperimentWorkspaceProps["position"]>): string {
  const spread = position.payload.current_spread ?? position.payload.opening_spread;
  return [
    formatDecimal(spread.long_strike),
    "/",
    formatDecimal(spread.short_strike),
    spread.option_type.toLowerCase(),
    spread.structure.toLowerCase(),
  ].join(" ");
}

function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: "positive" | "adverse" }) {
  return (
    <div className="experiment-metric">
      <dt>{label}</dt>
      <dd className={tone ? `experiment-tone--${tone}` : undefined}>{value}</dd>
    </div>
  );
}

function unavailableReason(reason: ExperimentMetricUnavailableReason): string {
  return {
    NO_OPENED_TRADES: copy.performance.noOpenedTrades,
    NO_CLOSED_TRADES: copy.performance.noClosedTrades,
  }[reason];
}

function projectedValue(
  metric:
    | { value: string | number; unavailable_reason: null }
    | { value: null; unavailable_reason: ExperimentMetricUnavailableReason },
  formatter: (value: string | number) => string,
): ReactNode {
  if (metric.value === null) {
    return <span className="experiment-metric__unavailable">{unavailableReason(metric.unavailable_reason)}</span>;
  }
  return formatter(metric.value);
}

const terminalStateLabels: Record<ExperimentPerformanceProjection["terminal_state"], string> = {
  NO_POSITION: copy.performance.terminalNoPosition,
  OPEN: copy.performance.terminalOpen,
  CLOSED: copy.performance.terminalClosed,
};

function PerformanceProjection({
  loading,
  projection,
  source,
}: {
  loading: boolean;
  projection: ExperimentPerformanceProjection | null;
  source: ExperimentWorkspaceProps["definition"]["source"];
}) {
  if (source === "REPLAY") {
    return <p className="experiment-performance__unavailable">{copy.performance.replayOnly}</p>;
  }
  if (loading) {
    return <p className="experiment-performance__unavailable">{copy.performance.projectionLoading}</p>;
  }
  if (!projection) {
    return <p className="experiment-performance__unavailable">{copy.performance.projectionUnavailable}</p>;
  }
  const money = (value: string | number) => formatMoney(value, true);
  const count = (value: string | number) => Number(value).toLocaleString("en-US");
  return (
    <div className="experiment-performance__projection">
      <p className="experiment-performance__certified">{copy.performance.projectionCertified}</p>
      <section aria-labelledby="experiment-performance-activity">
        <h3 id="experiment-performance-activity">{copy.performance.activity}</h3>
        <dl>
          <Metric label={copy.performance.decisionCount} value={count(projection.decision_count)} />
          <Metric label={copy.performance.openedTradeCount} value={count(projection.opened_trade_count)} />
          <Metric label={copy.performance.closedTradeCount} value={count(projection.closed_trade_count)} />
          <Metric label={copy.performance.terminalState} value={terminalStateLabels[projection.terminal_state]} />
        </dl>
      </section>
      <section aria-labelledby="experiment-performance-cash-flow">
        <h3 id="experiment-performance-cash-flow">{copy.performance.certifiedCashFlow}</h3>
        <dl>
          <Metric
            label={copy.performance.definedRiskAtEntry}
            value={projectedValue(
              projection.total_defined_maximum_risk_at_entry,
              (value) => formatMoney(value),
            )}
          />
          <Metric
            label={copy.performance.entryCashFlow}
            value={projectedValue(projection.entry_cash_flow, money)}
          />
          <Metric
            label={copy.performance.managementCashFlow}
            value={projectedValue(projection.management_cash_flow, money)}
          />
          <Metric
            label={copy.performance.exitCashFlow}
            value={projectedValue(projection.exit_cash_flow, money)}
          />
        </dl>
      </section>
      <section aria-labelledby="experiment-performance-closed-results">
        <h3 id="experiment-performance-closed-results">{copy.performance.closedResults}</h3>
        <dl>
          <Metric
            label={copy.performance.paperPnl}
            value={projectedValue(projection.realized_strategy_pnl, money)}
          />
          <Metric label={copy.performance.wins} value={projectedValue(projection.win_count, count)} />
          <Metric label={copy.performance.losses} value={projectedValue(projection.loss_count, count)} />
          <Metric
            label={copy.performance.breakevens}
            value={projectedValue(projection.breakeven_count, count)}
          />
        </dl>
      </section>
    </div>
  );
}

function DecisionSpine({
  definition,
  performanceProjection,
  position,
}: Pick<ExperimentWorkspaceProps, "definition" | "performanceProjection" | "position">) {
  const terminalState = performanceProjection
    ? terminalStateLabels[performanceProjection.terminal_state]
    : copy.trade.notRecorded;
  return (
    <ol className="experiment-decision-spine" aria-label={copy.workspace.decisionSpine}>
      <li><span>{copy.workspace.thesis}</span><strong>{definition.thesis}</strong></li>
      <li><span>{copy.workspace.frozenProtocol}</span><strong>{definition.structure ?? copy.trade.notRecorded}</strong></li>
      <li><span>{copy.workspace.entryDecision}</span><strong>{position ? copy.timeline.entry : copy.trade.notEntered}</strong></li>
      <li><span>{copy.workspace.lifecycle}</span><strong>{position?.payload.state ?? copy.trade.notRecorded}</strong></li>
      <li><span>{copy.workspace.outcome}</span><strong>{terminalState}</strong></li>
    </ol>
  );
}

function performanceState({
  definition,
  performanceProjection,
  position,
  proof,
}: Pick<
  ExperimentWorkspaceProps,
  "definition" | "performanceProjection" | "position" | "proof"
>) {
  if (definition.source === "REPLAY") {
    return { detail: copy.performance.replayOnly, point: null };
  }
  if (performanceProjection) {
    return { detail: copy.performance.projectionCertified, point: null };
  }
  if (definition.status === "REJECTED") {
    return { detail: copy.performance.noTradeOutcome, point: null };
  }
  if (position?.payload.state === "OPEN") {
    return { detail: copy.performance.openPosition, point: null };
  }
  if (position) {
    const point = proof?.publication_status === "PUBLISHED" && proof.point?.status === "COMPLETE"
      ? proof.point
      : null;
    return { detail: copy.performance.lifecyclePublished, point };
  }
  if (proof === null) return { detail: copy.performance.unavailable, point: null };
  if (!proof || proof.publication_status === "NOT_PUBLISHED") {
    return { detail: copy.performance.decisionPending, point: null };
  }
  if (!proof.point || proof.point.status !== "COMPLETE") {
    return { detail: copy.performance.incomplete, point: null };
  }
  if (proof.baseline_status !== "BASELINE_CLEAN") {
    return { detail: copy.performance.baselineUnclean, point: null };
  }
  return { detail: copy.performance.published, point: proof.point };
}

function pathCoordinates(
  points: readonly ExperimentValuePoint[],
  width: number,
  height: number,
  low: number,
  high: number,
): string {
  if (points.length === 0) return "";
  const range = Math.max(high - low, Math.abs(high) * 0.0025, 1);
  return points.map((point, index) => {
    const x = points.length === 1 ? width / 2 : (index / (points.length - 1)) * width;
    const y = height - ((point.value - low) / range) * height;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}

function ValuePath({
  valuePath,
  benchmark,
  position,
}: Pick<ExperimentWorkspaceProps, "valuePath" | "benchmark" | "position">) {
  const series = valuePath ?? [];
  const benchmarkSeries = benchmark?.path ?? [];
  if (series.length === 0) {
    return (
      <div className="experiment-empty">
        <p>{copy.performance.noPath}</p>
        <p>{copy.performance.noComparison}</p>
      </div>
    );
  }
  const width = 800;
  const height = 224;
  const allValues = [...series, ...benchmarkSeries].map((point) => point.value);
  const low = Math.min(...allValues);
  const high = Math.max(...allValues);
  const strategyCoordinates = pathCoordinates(series, width, height, low, high);
  const benchmarkCoordinates = pathCoordinates(benchmarkSeries, width, height, low, high);
  const start = Date.parse(series[0].at);
  const finish = Date.parse(series.at(-1)?.at ?? series[0].at);
  const range = Math.max(finish - start, 1);
  const valueRange = Math.max(high - low, Math.abs(high) * 0.0025, 1);
  const executionEvents = position?.payload.events.filter((event) => event.event_kind === "EXECUTION") ?? [];
  return (
    <figure className="experiment-chart">
      <svg role="img" aria-label={copy.performance.pathDescription} viewBox={`0 0 ${width} ${height + 36}`}>
        <line className="experiment-chart__axis" x1="0" x2={width} y1={height} y2={height} />
        <polyline
          className="experiment-chart__line"
          fill="none"
          points={strategyCoordinates}
        />
        {benchmarkSeries.length ? (
          <polyline
            className="experiment-chart__benchmark"
            fill="none"
            points={benchmarkCoordinates}
          />
        ) : null}
        {executionEvents.map((event) => {
          const eventTime = Date.parse(event.occurred_at);
          const point = series.reduce((nearest, candidate) => (
            Math.abs(Date.parse(candidate.at) - eventTime) < Math.abs(Date.parse(nearest.at) - eventTime)
              ? candidate
              : nearest
          ), series[0]);
          const x = Math.max(0, Math.min(width, ((eventTime - start) / range) * width));
          const y = height - ((point.value - low) / valueRange) * height;
          return (
            <circle key={`${event.occurred_at}-${event.action}`} className="experiment-chart__point" cx={x} cy={y} r="5">
              <title>{`${eventTitle(event)} · ${formatDate(event.occurred_at)} · ${formatMoney(point.value)}`}</title>
            </circle>
          );
        })}
        <text x="0" y={height + 30}>{copy.chart.start}</text>
        <text textAnchor="end" x={width} y={height + 30}>{copy.chart.finish}</text>
      </svg>
      <figcaption>
        <span><i className="experiment-chart__key experiment-chart__key--strategy" />{copy.performance.strategySeries}</span>
        {benchmarkSeries.length ? (
          <span><i className="experiment-chart__key experiment-chart__key--benchmark" />{benchmark?.label}</span>
        ) : <span>{copy.performance.noComparison}</span>}
      </figcaption>
    </figure>
  );
}

function PayoffProfile({ payoff }: Pick<ExperimentWorkspaceProps, "payoff">) {
  if (!payoff || payoff.points.length < 2) return null;
  const width = 800;
  const height = 180;
  const prices = payoff.points.map((point) => point.underlyingPrice);
  const values = payoff.points.map((point) => point.pnlUsd);
  const lowPrice = Math.min(...prices);
  const highPrice = Math.max(...prices);
  const lowValue = Math.min(...values, 0);
  const highValue = Math.max(...values, 0);
  const priceRange = Math.max(highPrice - lowPrice, 1);
  const valueRange = Math.max(highValue - lowValue, 1);
  const coordinates = payoff.points.map((point) => {
    const x = ((point.underlyingPrice - lowPrice) / priceRange) * width;
    const y = height - ((point.pnlUsd - lowValue) / valueRange) * height;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  const zeroY = height - ((0 - lowValue) / valueRange) * height;
  return (
    <figure className="experiment-payoff">
      <div>
        <p className="experiment-kicker">{copy.trade.payoff}</p>
        <dl>
          <Metric label={copy.trade.breakeven} value={payoff.breakevenUsd === null ? copy.trade.notRecorded : formatMoney(payoff.breakevenUsd)} />
          <Metric label={copy.trade.maximumProfit} value={payoff.maximumProfitUsd === null ? copy.trade.notRecorded : formatMoney(payoff.maximumProfitUsd)} />
        </dl>
      </div>
      <svg role="img" aria-label={copy.trade.payoffDescription} viewBox={`0 0 ${width} ${height}`}>
        <line className="experiment-chart__axis" x1="0" x2={width} y1={zeroY} y2={zeroY} />
        <polyline className="experiment-payoff__line" fill="none" points={coordinates} />
      </svg>
    </figure>
  );
}

function eventTitle(event: PositionEvent): string {
  if (event.event_kind === "EXECUTION") {
    return {
      ENTRY: copy.timeline.entry,
      ROLL: copy.timeline.roll,
      CLOSE: copy.timeline.close,
    }[event.action];
  }
  return {
    HOLD: copy.timeline.hold,
    CLOSE: copy.timeline.closeReview,
    ROLL: copy.timeline.rollReview,
    NO_ACTION: copy.timeline.noAction,
  }[event.action];
}

function eventReason(event: PositionEvent): string {
  return {
    POSITION_OPENED: copy.reason.positionOpened,
    POSITION_ROLLED: copy.reason.positionRolled,
    POSITION_CLOSED: copy.reason.positionClosed,
    POSITION_REVIEWED: copy.reason.positionReviewed,
    RISK_REDUCTION: copy.reason.riskReduction,
    THESIS_CHANGED: copy.reason.thesisChanged,
    POSITION_ADJUSTMENT: copy.reason.positionAdjustment,
    DATA_INCOMPLETE: copy.reason.dataIncomplete,
  }[event.reason_category];
}

function DecisionTimeline({ position, createdAt, rejectionReason, status }: Pick<
  ExperimentWorkspaceProps,
  "position" | "createdAt" | "rejectionReason"
> & { status: ExperimentStatus }) {
  const events = position?.payload.events ?? [];
  const hasOpeningEvent = Boolean(createdAt);
  const hasRejectionEvent = status === "REJECTED" && Boolean(rejectionReason);
  if (!hasOpeningEvent && !hasRejectionEvent && events.length === 0) {
    return <p className="experiment-empty">{copy.timeline.noEvents}</p>;
  }
  return (
    <ol className="experiment-timeline">
      {createdAt ? (
        <li>
          <time dateTime={createdAt}>{formatDate(createdAt)}</time>
          <strong>{copy.timeline.started}</strong>
        </li>
      ) : null}
      {events.map((event, index) => (
        <li key={`${event.occurred_at}-${event.action}-${index}`}>
          <time dateTime={event.occurred_at}>{formatDate(event.occurred_at)}</time>
          <strong>{eventTitle(event)}</strong>
          <p><span>{copy.timeline.why}</span>{eventReason(event)}</p>
          {event.event_kind === "EXECUTION" ? (
            <p><span>{copy.timeline.cashflow}</span>{formatMoney(event.cashflow_usd, true)}</p>
          ) : null}
        </li>
      ))}
      {hasRejectionEvent ? (
        <li>
          <strong>{copy.timeline.rejected}</strong>
          <p><span>{copy.timeline.why}</span>{rejectionReason}</p>
        </li>
      ) : null}
    </ol>
  );
}

function EvidenceList({ evidence }: { evidence: readonly ExperimentEvidence[] }) {
  if (evidence.length === 0) return <p className="experiment-empty">{copy.evidence.noEvidence}</p>;
  return (
    <ul className="experiment-evidence">
      {evidence.map((item) => (
        <li key={item.id}>
          <div>
            <strong>{item.title}</strong>
            <span className={`experiment-evidence__state experiment-evidence__state--${item.state.toLowerCase()}`}>
              {evidenceLabels[item.state]}
            </span>
          </div>
          <p>{item.detail}</p>
          {item.observedAt || item.sourceLabel ? (
            <dl>
              {item.observedAt ? <Metric label={copy.evidence.observed} value={formatDate(item.observedAt)} /> : null}
              {item.sourceLabel ? <Metric label={copy.evidence.source} value={item.sourceLabel} /> : null}
            </dl>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function ExperimentWorkspace({
  benchmark = null,
  createdAt = null,
  definition,
  evidence = [],
  performanceConnection,
  performanceProjection = null,
  position = null,
  proof,
  payoff = null,
  rejectionReason = null,
  valuePath = [],
}: ExperimentWorkspaceProps) {
  const [loadedPerformance, setLoadedPerformance] = useState<ExperimentPerformanceProjection | null>(null);
  const [performanceLoading, setPerformanceLoading] = useState(false);
  const performanceConnected = Boolean(performanceConnection);
  const connectedPerformance = performanceConnected ? loadedPerformance : performanceProjection;
  const performanceAuthenticated = performanceConnection?.authenticated ?? false;
  const performanceCsrfToken = performanceConnection?.csrfToken ?? null;
  const performanceClient = performanceConnection?.client ?? experimentPerformanceClient;
  const onPerformanceSessionRejected = performanceConnection?.onSessionRejected;

  useEffect(() => {
    if (!performanceConnected || definition.source === "REPLAY") return;
    let active = true;
    setLoadedPerformance(null);
    setPerformanceLoading(true);
    const load = async () => {
      try {
        const projection = performanceAuthenticated
          ? await performanceClient.readOwner(definition.id, performanceCsrfToken ?? "")
          : await performanceClient.readPublished(definition.id);
        if (!active) return;
        setLoadedPerformance(projection);
        setPerformanceLoading(false);
      } catch (error) {
        if (!active) return;
        setLoadedPerformance(null);
        setPerformanceLoading(false);
        if (
          error instanceof ExperimentPerformanceRequestError
          && (error.status === 401 || error.status === 403)
        ) onPerformanceSessionRejected?.();
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [
    definition.id,
    definition.source,
    performanceAuthenticated,
    performanceClient,
    performanceConnected,
    performanceCsrfToken,
    onPerformanceSessionRejected,
  ]);

  const performance = performanceLoading
    ? { detail: copy.performance.projectionLoading, point: null }
    : performanceState({
        definition,
        performanceProjection: connectedPerformance,
        position,
        proof,
      });
  const status = position?.payload.state ?? definition.status;
  const spread = position ? formatSpread(position) : definition.structure ?? copy.trade.notRecorded;
  const openingEvent = position?.payload.events.find(
    (event) => event.event_kind === "EXECUTION" && event.action === "ENTRY",
  );
  const closeEvent = [...(position?.payload.events ?? [])].reverse().find(
    (event) => event.event_kind === "EXECUTION" && event.action === "CLOSE",
  );
  const currentExposure = position?.payload.current_exposure;
  const record = position;

  return (
    <article className="experiment-workspace" aria-labelledby="experiment-title">
      <header className="experiment-hero">
        <div className="experiment-hero__identity">
          <p className="experiment-kicker">
            {definition.source === "PAPER" ? copy.workspace.paper : copy.workspace.replay}
          </p>
          <h1 id="experiment-title">{definition.name}</h1>
          <p className="experiment-hero__thesis">{definition.thesis}</p>
        </div>
        <div className="experiment-hero__result" aria-live="polite">
          <span>{copy.workspace.status}</span>
          <strong>{statusLabels[status]}</strong>
          <p>{performance.detail}</p>
        </div>
      </header>

      <section className="experiment-performance" aria-labelledby="experiment-performance-title">
        <div className="experiment-section-heading">
          <p className="experiment-kicker">{copy.workspace.performance}</p>
          <h2 id="experiment-performance-title">{copy.workspace.decisionSpine}</h2>
        </div>
        <DecisionSpine definition={definition} performanceProjection={connectedPerformance} position={position} />
        <PerformanceProjection
          loading={performanceLoading}
          projection={connectedPerformance}
          source={definition.source}
        />
        <ValuePath valuePath={valuePath} benchmark={benchmark} position={position} />
      </section>

      <section className="experiment-trade" aria-labelledby="experiment-trade-title">
        <div className="experiment-section-heading">
          <p className="experiment-kicker">{copy.workspace.riskAndTrade}</p>
          <h2 id="experiment-trade-title">{spread}</h2>
        </div>
        <dl className="experiment-trade__facts">
          <Metric label={copy.trade.underlying} value={position?.payload.underlying ?? definition.underlying} />
          <Metric label={copy.trade.quantity} value={position?.payload.opening_spread.quantity ?? copy.trade.notRecorded} />
          <Metric label={copy.trade.maximumRisk} value={definition.maximumRiskUsd === null ? copy.trade.notRecorded : formatMoney(definition.maximumRiskUsd)} />
          <Metric label={copy.trade.entry} value={openingEvent?.event_kind === "EXECUTION" ? formatMoney(openingEvent.cashflow_usd, true) : copy.trade.notEntered} />
          <Metric label={copy.trade.opened} value={position ? formatDate(position.payload.opened_at) : copy.trade.notEntered} />
          <Metric label={copy.trade.exit} value={closeEvent?.event_kind === "EXECUTION" ? formatMoney(closeEvent.cashflow_usd, true) : position?.payload.state === "OPEN" ? copy.trade.stillOpen : copy.trade.notRecorded} />
        </dl>
        <PayoffProfile payoff={payoff} />
        <details className="experiment-disclosure">
          <summary>{copy.workspace.optionsDetail}</summary>
          {position ? (
            <dl className="experiment-options-detail">
              <Metric
                label={copy.trade.direction}
                value={{
                  BULLISH: publicCopy.competitionRecord.bullish,
                  BEARISH: publicCopy.competitionRecord.bearish,
                  NEUTRAL: publicCopy.competitionRecord.neutral,
                }[position.payload.thesis.direction]}
              />
              <Metric label={copy.trade.target} value={formatDate(position.payload.thesis.target_at)} />
              {currentExposure ? (
                <>
                  <Metric label={copy.trade.delta} value={currentExposure.delta ?? copy.trade.notRecorded} />
                  <Metric label={copy.trade.gamma} value={currentExposure.gamma ?? copy.trade.notRecorded} />
                  <Metric label={copy.trade.theta} value={currentExposure.theta_per_day ?? copy.trade.notRecorded} />
                  <Metric label={copy.trade.vega} value={currentExposure.vega_per_iv_point ?? copy.trade.notRecorded} />
                </>
              ) : <Metric label={copy.trade.currentExposure} value={copy.trade.noExposure} />}
            </dl>
          ) : <p className="experiment-empty">{copy.trade.noExposure}</p>}
        </details>
      </section>

      <div className="experiment-rationale-grid">
        <section aria-labelledby="experiment-why-title">
          <p className="experiment-kicker">{copy.workspace.why}</p>
          <h2 id="experiment-why-title">{copy.workspace.thesis}</h2>
          <ul className="experiment-plain-list">
            {definition.whyChosen.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </section>
        <section aria-labelledby="experiment-invalidation-title">
          <p className="experiment-kicker">{copy.workspace.invalidation}</p>
          <h2 id="experiment-invalidation-title">{copy.workspace.invalidation}</h2>
          <ul className="experiment-plain-list">
            {definition.invalidation.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </section>
      </div>

      <section className="experiment-section" aria-labelledby="experiment-evidence-title">
        <div className="experiment-section-heading">
          <p className="experiment-kicker">{copy.workspace.evidence}</p>
          <h2 id="experiment-evidence-title">{copy.workspace.evidence}</h2>
        </div>
        <EvidenceList evidence={evidence} />
      </section>

      <section className="experiment-section" aria-labelledby="experiment-timeline-title">
        <div className="experiment-section-heading">
          <p className="experiment-kicker">{copy.workspace.timeline}</p>
          <h2 id="experiment-timeline-title">{statusLabels[status]}</h2>
        </div>
        <DecisionTimeline
          createdAt={createdAt}
          position={position}
          rejectionReason={rejectionReason}
          status={status}
        />
      </section>

      <details className="experiment-disclosure experiment-disclosure--technical">
        <summary>{copy.workspace.technical}</summary>
        <p>{copy.technical.explanation}</p>
        <dl className="experiment-technical-grid">
          <Metric label={copy.technical.recordId} value={record?.public_record_id ?? copy.technical.notAvailable} />
          <Metric label={copy.technical.publication} value={record?.publication_hash ?? copy.technical.notAvailable} />
          <Metric label={copy.technical.predecessor} value={record?.predecessor_hash ?? copy.technical.notAvailable} />
          <Metric label={copy.technical.policy} value={definition.policyVersion ?? copy.technical.notAvailable} />
        </dl>
      </details>
    </article>
  );
}
