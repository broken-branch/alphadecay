import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { CompetitionPerformanceProofResponse, HealthResponse } from "../contracts/v1";
import { CompetitionRecordView } from "../competition-record/CompetitionRecord";
import type { CompetitionRecordResponse } from "../competition-record/api";
import { copy } from "../content/copy";
import { InformationDialog, OwnerSettings } from "../owner/OwnerSettings";
import brandLockupUrl from "../assets/branding/alphadecay-lockup.svg";
import brandLockupLightUrl from "../assets/branding/alphadecay-lockup-light.svg";
import brandMarkUrl from "../assets/branding/alphadecay-mark.svg";
import brandMarkLightUrl from "../assets/branding/alphadecay-mark-light.svg";
import { replayFixtures } from "./fixtures";
import { actionLabels, alternativeStateLabels, matchLabels, qualityLabels } from "./labels";
import { StateNotice } from "./StateNotice";
import type {
  ComparisonRow,
  GreekExposure,
  OperationalState,
  ReplayAction,
  ReplayFixture,
  ReviewUrgency,
  ScenarioId,
} from "./types";

const scenarioIds: ScenarioId[] = [
  "THESIS_INTACT",
  "THETA_TAKEOVER",
  "CATALYST_BROKEN",
  "STALE_QUOTE",
];
const tabIds = ["evidence", "choices", "activity", "record"] as const;
type TabId = (typeof tabIds)[number];
type Theme = "dark" | "light";
type WorkspaceView = "competition" | "demo" | "setup";

const comparisonLabels: Record<ComparisonRow["key"], string> = {
  direction: copy.thesis.direction,
  volatility: copy.thesis.volatility,
  horizon: copy.thesis.horizon,
  risk: copy.thesis.risk,
  evidence: copy.thesis.evidence,
};
const thesisPromises: Record<ComparisonRow["key"], string> = {
  direction: copy.thesis.bullish,
  volatility: copy.thesis.normalVolatility,
  horizon: copy.thesis.fullHorizon,
  risk: copy.thesis.definedRisk,
  evidence: copy.thesis.noContradiction,
};
const driftLabels = {
  exposure: copy.drift.exposure,
  volatility: copy.drift.volatility,
  time: copy.drift.time,
  evidence: copy.drift.evidence,
  risk: copy.drift.risk,
};
const stageCopy = [
  copy.run.observe,
  copy.run.validate,
  copy.run.research,
  copy.run.classify,
  copy.run.measure,
  copy.run.compare,
  copy.run.enforce,
  copy.run.certify,
];
const expectedAfterCopy: Record<ReplayAction, string> = {
  HOLD: copy.certificate.expectedHold,
  CLOSE: copy.certificate.expectedClose,
  ROLL: copy.certificate.expectedRoll,
  NO_ACTION: copy.certificate.expectedBlocked,
};
const tabLabels: Record<TabId, string> = {
  evidence: copy.navigation.overview,
  choices: copy.navigation.comparison,
  activity: copy.navigation.run,
  record: copy.navigation.record,
};
const actionContextLabels: Record<ReplayAction, string> = {
  HOLD: copy.positionReview.ifHeld,
  CLOSE: copy.positionReview.afterClose,
  ROLL: copy.positionReview.afterRoll,
  NO_ACTION: copy.positionReview.noActionHeading,
};
const currentMetricLabels: Record<ComparisonRow["key"], string> = {
  direction: copy.thesis.delta,
  volatility: copy.thesis.vega,
  horizon: copy.positionReview.dteShort,
  risk: copy.thesis.maxLoss,
  evidence: copy.positionReview.driftScore,
};
const urgencyLabels: Record<ReviewUrgency, string> = {
  ROUTINE: copy.positionReview.urgencyRoutine,
  SOON: copy.positionReview.urgencySoon,
  IMMEDIATE: copy.positionReview.urgencyImmediate,
  WAITING: copy.positionReview.urgencyWaiting,
};
const sourceTierLabels: Record<ReplayFixture["evidenceCards"][number]["sourceTier"], string> = {
  PRIMARY: copy.provenanceDetail.primaryTierSample,
  ORIGINAL_REPORTING: copy.provenanceDetail.originalReportingTierSample,
  SECONDARY: copy.provenanceDetail.secondaryTierSample,
};
const invalidationLabels: Record<ReplayFixture["invalidationCode"], string> = {
  PRIMARY_CONTRADICTION: copy.thesis.invalidationValue,
};

type ReplayShellProps = {
  initialScenario?: ScenarioId;
  operationalState?: OperationalState;
  replayLoader?: (scenario: ScenarioId, fallback: ReplayFixture) => Promise<ReplayFixture>;
  proofLoader?: () => Promise<CompetitionPerformanceProofResponse>;
  archiveLoader?: () => Promise<CompetitionRecordResponse>;
  runtimeLoader?: () => Promise<HealthResponse>;
};
type PerformancePoint = NonNullable<CompetitionPerformanceProofResponse["point"]>;
type BaselineStatus = NonNullable<CompetitionPerformanceProofResponse["baseline_status"]>;

function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const light = theme === "light";
  return (
    <button
      className="theme-toggle"
      type="button"
      role="switch"
      aria-checked={light}
      aria-label={copy.theme.label}
      title={light ? copy.theme.light : copy.theme.dark}
      onClick={onToggle}
    >
      {light ? (
        <svg className="theme-toggle__icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42" />
        </svg>
      ) : (
        <svg className="theme-toggle__icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M20.5 15.2A8.5 8.5 0 0 1 8.8 3.5 8.5 8.5 0 1 0 20.5 15.2Z" />
        </svg>
      )}
    </button>
  );
}

function isProtectedShortcutTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest(
      'input, select, textarea, button, a, summary, audio, video, dialog, [role="dialog"], [role="button"], [role="link"], [role="textbox"], [role="combobox"], [contenteditable], [tabindex]:not([tabindex="-1"])',
    ),
  );
}

function formatCurrency(value: number): string {
  return `${value < 0 ? "-" : ""}${copy.performance.currencySymbol}${Math.abs(value)}`;
}
function formatMoney(value: string | null): string {
  if (value === null) return copy.performance.notAvailable;
  const hasSign = value.startsWith("+") || value.startsWith("-");
  const sign = hasSign ? value[0] : "";
  const unsigned = hasSign ? value.slice(1) : value;
  const [whole, fraction = ""] = unsigned.split(".");
  return `${sign}${copy.performance.currencySymbol}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction.padEnd(2, "0")}`;
}
function formatPercent(value: string | null): string {
  return value === null
    ? copy.performance.notAvailable
    : `${value}${copy.performance.percentSymbol}`;
}
function formatUtc(value: string | null): string {
  return value === null
    ? copy.performance.notMeasured
    : new Date(value).toISOString().replace(".000Z", "Z");
}
function formatSigned(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}
function formatAge(seconds: number): string {
  return `${seconds} ${
    seconds === 1 ? copy.positionReview.second : copy.positionReview.seconds
  }`;
}
function formatObservedAge(label: string, seconds: number, includeOld = false): string {
  const parts = [label, copy.positionReview.separator, formatAge(seconds)];
  if (includeOld) parts.push(copy.positionReview.oldSuffix);
  return parts.join(" ");
}
function captureStatusLabel(status: PerformancePoint["status"]): string {
  return {
    COMPLETE: copy.performance.captureComplete,
    MISSING: copy.performance.captureMissing,
    UNKNOWN: copy.performance.captureUnknown,
  }[status];
}
function baselineStatusLabel(status: BaselineStatus): string {
  return {
    BASELINE_CLEAN: copy.performance.baselineClean,
    BASELINE_CONTAMINATED: copy.performance.baselineContaminated,
    BASELINE_UNKNOWN: copy.performance.baselineUnknownValue,
    BASELINE_NOT_CAPTURED: copy.performance.baselineNotCapturedValue,
  }[status];
}
function failureLabel(code: NonNullable<PerformancePoint["failure_code"]>): string {
  return {
    CAPTURE_NOT_STARTED: copy.performance.failureCaptureNotStarted,
    PROVIDER_UNAVAILABLE: copy.performance.failureProviderUnavailable,
    ACCOUNT_STATE_INCOMPLETE: copy.performance.failureAccountIncomplete,
    BASELINE_UNAVAILABLE: copy.performance.failureBaselineUnavailable,
    SCHEMA_INVALID: copy.performance.failureSchemaInvalid,
  }[code];
}
function publishedProofCopy(baseline: BaselineStatus, point: PerformancePoint) {
  if (point.status === "MISSING") {
    return { heading: copy.performance.missing, detail: copy.performance.missingDetail };
  }
  if (point.status === "UNKNOWN") {
    return { heading: copy.performance.unknown, detail: copy.performance.unknownDetail };
  }
  if (baseline === "BASELINE_CONTAMINATED") {
    return { heading: copy.performance.contaminated, detail: copy.performance.contaminatedDetail };
  }
  if (baseline === "BASELINE_UNKNOWN") {
    return {
      heading: copy.performance.baselineUnknown,
      detail: copy.performance.baselineUnknownDetail,
    };
  }
  if (baseline === "BASELINE_NOT_CAPTURED") {
    return {
      heading: copy.performance.baselineNotCaptured,
      detail: copy.performance.baselineNotCapturedDetail,
    };
  }
  return { heading: copy.performance.complete, detail: copy.performance.completeDetail };
}
function ProofMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function PerformanceProof({
  proof,
  compact = false,
}: {
  proof: CompetitionPerformanceProofResponse | null | undefined;
  compact?: boolean;
}) {
  let heading = copy.performance.loading;
  let detail: string | null = null;
  const published =
    proof?.publication_status === "PUBLISHED" && proof.point && proof.baseline_status
      ? { proof, point: proof.point, baseline: proof.baseline_status }
      : null;
  if (proof === null) {
    heading = copy.performance.unavailable;
    detail = copy.performance.unavailableDetail;
  } else if (proof?.publication_status === "NOT_PUBLISHED") {
    heading = copy.performance.notPublished;
    detail = copy.performance.notPublishedDetail;
  } else if (published) {
    ({ heading, detail } = publishedProofCopy(published.baseline, published.point));
  }

  return (
    <section className="performance-proof" aria-label={copy.performance.label}>
      <p className="eyebrow">{copy.performance.sourceLabel}</p>
      <h3 aria-live="polite" aria-atomic="true">
        {heading}
      </h3>
      {detail ? <p className="muted">{detail}</p> : null}
      {published && !compact ? (
        <>
          <dl className="proof-status">
            <ProofMetric
              label={copy.performance.captureStatus}
              value={captureStatusLabel(published.point.status)}
            />
            <ProofMetric
              label={copy.performance.baselineStatus}
              value={baselineStatusLabel(published.baseline)}
            />
          </dl>
          <dl className="proof-grid">
            <ProofMetric
              label={copy.performance.startingEquity}
              value={copy.performance.startingEquityValue}
            />
            <ProofMetric
              label={copy.performance.currentEquity}
              value={formatMoney(published.point.current_equity_usd)}
            />
            <ProofMetric
              label={copy.performance.equityChange}
              value={formatMoney(published.point.account_equity_change_usd)}
            />
            <ProofMetric
              label={copy.performance.equityReturn}
              value={formatPercent(published.point.account_equity_return_pct)}
            />
            <ProofMetric
              label={copy.performance.scheduledFor}
              value={formatUtc(published.point.scheduled_for)}
            />
            <ProofMetric
              label={copy.performance.attemptedAt}
              value={formatUtc(published.point.attempted_at)}
            />
            <ProofMetric
              label={copy.performance.measuredAt}
              value={formatUtc(published.point.measured_at)}
            />
            <ProofMetric
              label={copy.performance.publishedAt}
              value={formatUtc(published.proof.published_at)}
            />
            <ProofMetric
              label={copy.performance.lifecycleCashflow}
              value={formatMoney(published.point.reconciled_lifecycle_cashflow_usd)}
            />
            <ProofMetric
              label={copy.performance.liquidationPnl}
              value={formatMoney(published.point.open_position_liquidation_pnl_usd)}
            />
            {published.point.failure_code ? (
              <ProofMetric
                label={copy.performance.failureReason}
                value={failureLabel(published.point.failure_code)}
              />
            ) : null}
            <ProofMetric
              label={copy.performance.simulator}
              value={copy.performance.simulatorValue}
            />
            <ProofMetric
              label={copy.performance.publicationHash}
              value={published.proof.publication_hash ?? copy.performance.notAvailable}
            />
            <ProofMetric
              label={copy.performance.predecessor}
              value={
                published.proof.predecessor_hash
                  ? `${copy.performance.linkedPublication}: ${published.proof.predecessor_hash}`
                  : copy.performance.firstPublication
              }
            />
            <ProofMetric
              label={copy.performance.linkedCertificates}
              value={published.proof.linked_certificate_ids.length}
            />
          </dl>
          <p className="proof-limitation">{copy.performance.simulatorDetail}</p>
        </>
      ) : null}
    </section>
  );
}

function WorkspaceNavigation({
  view,
  proof,
  archive,
  onChange,
}: {
  view: WorkspaceView | null;
  proof: CompetitionPerformanceProofResponse | null | undefined;
  archive: CompetitionRecordResponse | null | undefined;
  onChange: (view: WorkspaceView) => void;
}) {
  const published = archive?.publication_status === "PUBLISHED" || proof?.publication_status === "PUBLISHED";
  const stateLabel =
    view === null
      ? copy.competitionRecord.loading
      : view === "demo"
      ? copy.gateway.sample
      : view === "setup"
        ? copy.gateway.paperSetup
        : published
          ? copy.gateway.actualRecord
          : copy.gateway.noPublishedRecord;
  return (
    <div className="workspace-strip">
      <nav className="workspace-nav" aria-label={copy.gateway.label}>
        {(
          [
            ["competition", copy.gateway.competition],
            ["demo", copy.gateway.demo],
            ["setup", copy.gateway.setup],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-current={view === id ? "page" : undefined}
            className={id === "setup" ? "workspace-nav__secondary" : undefined}
            onClick={() => onChange(id)}
          >
            {label}
          </button>
        ))}
      </nav>
      <strong className={`workspace-state workspace-state--${view ?? "loading"}`}>{stateLabel}</strong>
    </div>
  );
}

function SetupView({
  onOpenOwnerSettings,
  ownerControlsEnabled,
}: {
  onOpenOwnerSettings: () => void;
  ownerControlsEnabled: boolean;
}) {
  return (
    <div className="setup-view">
      <div className="review-heading">
        <p className="eyebrow">{copy.gateway.paperSetup}</p>
        <h1>{copy.gateway.setupTitle}</h1>
        <p>{copy.gateway.setupIntro}</p>
      </div>
      <div className="setup-paths">
        <section>
          <h2>{ownerControlsEnabled ? copy.gateway.ownerTitle : copy.gateway.publicTitle}</h2>
          <p>{ownerControlsEnabled ? copy.gateway.ownerBody : copy.gateway.publicBody}</p>
          {ownerControlsEnabled ? (
            <button type="button" className="primary-button" onClick={onOpenOwnerSettings}>
              {copy.gateway.ownerAction}
            </button>
          ) : null}
        </section>
        <section>
          <h2>{copy.gateway.selfHostTitle}</h2>
          <p>{copy.gateway.selfHostBody}</p>
          <a
            className="quiet-button setup-link"
            href="https://github.com/broken-branch/alphadecay#readme"
          >
            {copy.gateway.selfHostAction}
          </a>
        </section>
      </div>
    </div>
  );
}

function ExposureValues({ exposure, dte }: { exposure: GreekExposure; dte?: number }) {
  return (
    <dl className="exposure-values">
      <div>
        <dt>{copy.thesis.delta}</dt>
        <dd>{formatSigned(exposure.delta)}</dd>
      </div>
      <div>
        <dt>{copy.thesis.theta}</dt>
        <dd>{formatCurrency(exposure.thetaPerDay)}</dd>
      </div>
      <div>
        <dt>{copy.thesis.vega}</dt>
        <dd>{formatCurrency(exposure.vega)}</dd>
      </div>
      {dte === undefined ? null : (
        <div>
          <dt>{copy.thesis.dte}</dt>
          <dd aria-label={`${dte} ${copy.positionReview.dteLong}`}>
            {dte} {copy.positionReview.dteShort}
          </dd>
        </div>
      )}
    </dl>
  );
}

function PositionContext({
  fixture,
  selected,
  onSelect,
}: {
  fixture: ReplayFixture;
  selected: ScenarioId;
  onSelect: (id: ScenarioId) => void;
}) {
  return (
    <section className="position-context" aria-label={copy.positionReview.selectedPosition}>
      <div className="position-context__identity">
        <span className="symbol">{fixture.position.symbol}</span>
        <strong>{copy.positionReview.spreadType}</strong>
      </div>
      <div className="position-context__details">
        <span>{fixture.position.strikes}</span>
        <span>{fixture.position.expiry}</span>
        <span aria-label={`${fixture.measured.dte} ${copy.positionReview.dteLong}`}>
          {fixture.measured.dte} {copy.positionReview.dteShort}
        </span>
        <span>{fixture.position.quantity}</span>
        {fixture.blockedState === "STALE" ? (
          <span className="position-context__stale semantic-adverse">
            {formatObservedAge(copy.positionReview.quoteAge, fixture.measured.dataAgeSeconds)}
          </span>
        ) : null}
      </div>
      <label className="scenario-select">
        <span>{copy.positionReview.scenarioLabel}</span>
        <select value={selected} onChange={(event) => onSelect(event.target.value as ScenarioId)}>
          {scenarioIds.map((id) => (
            <option key={id} value={id}>
              {copy.scenarios[id].name}
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}

function DecisionSummary({ fixture }: { fixture: ReplayFixture }) {
  const scenario = copy.scenarios[fixture.scenario];
  const timing = fixture.reviewTiming;
  return (
    <section id="decision" className="decision-summary" aria-labelledby="decision-title">
      <p className="eyebrow">{copy.positionReview.decision}</p>
      <h2 id="decision-title">{actionLabels[fixture.action]}</h2>
      <p className="decision-summary__reason">{scenario.decision}</p>
      <dl className="decision-timing">
        <div>
          <dt>{copy.positionReview.sampleAssessedAt}</dt>
          <dd>{timing.assessedAt}</dd>
        </div>
        <div>
          <dt>{copy.positionReview.quoteAge}</dt>
          <dd>{formatAge(timing.quoteAgeSeconds)}</dd>
        </div>
        <div>
          <dt>{copy.positionReview.reviewBy}</dt>
          <dd>{timing.reviewBy}</dd>
        </div>
        <div>
          <dt>{copy.positionReview.urgency}</dt>
          <dd className={timing.urgency === "IMMEDIATE" ? "semantic-adverse" : undefined}>
            {urgencyLabels[timing.urgency]}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function OpeningThesis({ fixture }: { fixture: ReplayFixture }) {
  const direction = fixture.intended.delta > 0 ? copy.thesis.bullish : copy.thesis.unknown;
  return (
    <section className="opening-thesis" aria-labelledby="opening-thesis-title">
      <p className="eyebrow" id="opening-thesis-title">
        {copy.thesis.openingTitle}
      </p>
      <dl>
        <div>
          <dt>{copy.thesis.savedThesis}</dt>
          <dd>{fixture.openingThesis}</dd>
        </div>
        <div>
          <dt>{copy.thesis.intendedDirection}</dt>
          <dd className="inline-measure">
            <span className="inline-measure__label">{copy.thesis.direction}</span>
            <span>{direction}</span>
            <span className="inline-measure__label">{copy.thesis.delta}</span>
            <span>{formatSigned(fixture.intended.delta)}</span>
          </dd>
        </div>
        <div>
          <dt>{copy.thesis.timeCondition}</dt>
          <dd>{copy.thesis.timeConditionValue}</dd>
        </div>
        <div>
          <dt>{copy.thesis.invalidation}</dt>
          <dd>{invalidationLabels[fixture.invalidationCode]}</dd>
        </div>
      </dl>
    </section>
  );
}

function AcquisitionSummary({ fixture }: { fixture: ReplayFixture }) {
  const state = {
    SELECTED: copy.acquisition.selected,
    REJECTED: copy.acquisition.rejected,
    NO_OP: copy.acquisition.noOp,
  }[fixture.acquisition.state];
  return (
    <section className="acquisition-summary" aria-labelledby="acquisition-title">
      <div>
        <h3 id="acquisition-title">{copy.acquisition.title}</h3>
      </div>
      <dl className="compact-facts">
        <div>
          <dt>{copy.acquisition.event}</dt>
          <dd>{fixture.acquisition.event}</dd>
        </div>
        <div>
          <dt>{copy.acquisition.trigger}</dt>
          <dd>{fixture.acquisition.trigger}</dd>
        </div>
        <div>
          <dt>{copy.acquisition.direction}</dt>
          <dd>{fixture.acquisition.direction}</dd>
        </div>
        <div>
          <dt>{copy.acquisition.structure}</dt>
          <dd>{fixture.acquisition.structure}</dd>
        </div>
        <div>
          <dt>{copy.acquisition.candidateCheck}</dt>
          <dd>{fixture.acquisition.candidateCheck}</dd>
        </div>
        <div>
          <dt>{copy.acquisition.state}</dt>
          <dd className="semantic-positive">{state}</dd>
        </div>
      </dl>
    </section>
  );
}

function AutonomousCycle({ fixture }: { fixture: ReplayFixture }) {
  return (
    <section className="autonomous-cycle" aria-labelledby="autonomous-cycle-title">
      <h3 id="autonomous-cycle-title">{copy.autonomy.title}</h3>
      <dl className="compact-facts">
        <div>
          <dt>{copy.autonomy.paper}</dt>
          <dd>{fixture.autonomy.armed ? copy.autonomy.armed : copy.autonomy.disarmed}</dd>
        </div>
        <div>
          <dt>{copy.autonomy.scheduledTrigger}</dt>
          <dd>{fixture.autonomy.scheduledTrigger}</dd>
        </div>
        <div>
          <dt>{copy.autonomy.emptyBookRoute}</dt>
          <dd>{fixture.autonomy.emptyBookRoute}</dd>
        </div>
        <div>
          <dt>{copy.autonomy.managedPositionRoute}</dt>
          <dd>{fixture.autonomy.managedPositionRoute}</dd>
        </div>
        <div>
          <dt>{copy.autonomy.policy}</dt>
          <dd>{fixture.autonomy.policy}</dd>
        </div>
        <div>
          <dt>{copy.autonomy.reconcile}</dt>
          <dd>{fixture.autonomy.reconcile}</dd>
        </div>
      </dl>
    </section>
  );
}

function MarketContext({ fixture }: { fixture: ReplayFixture }) {
  const items = [
    [copy.market.referenceSpot, fixture.market.referenceSpot],
    [copy.market.mark, fixture.market.mark],
    [copy.market.bidAsk, fixture.market.bidAsk],
    [copy.market.entryPrice, fixture.market.entryPrice],
    [copy.market.liquidationValue, fixture.market.liquidationValue],
    [copy.market.openPnl, fixture.market.openPnl],
    [copy.market.iv, fixture.market.iv],
    [copy.market.ivChange, fixture.market.ivChange],
    [copy.market.dte, `${fixture.measured.dte}`],
    [copy.market.maxLoss, `$${fixture.measured.maxLoss}`],
    [copy.market.riskCap, fixture.market.riskCap],
    [copy.market.orderState, fixture.market.orderState],
  ];
  return (
    <section className="market-context" aria-labelledby="market-context-title">
      <h3 id="market-context-title">{copy.market.title}</h3>
      <dl className="market-strip">
        {items.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ChangedSummary({ fixture }: { fixture: ReplayFixture }) {
  const scenario = copy.scenarios[fixture.scenario];
  const horizon = fixture.comparison.find((row) => row.key === "horizon");
  const adverse = horizon?.state === "BROKEN" || horizon?.state === "WEAKENED";
  return (
    <section className="changed-summary" aria-labelledby="changed-title">
      <p className="eyebrow">{copy.positionReview.whatChanged}</p>
      <h2 id="changed-title" className={adverse ? "semantic-adverse" : undefined}>
        {scenario.answer}
      </h2>
      <p>{scenario.summary}</p>
    </section>
  );
}

function ExposureComparison({ fixture }: { fixture: ReplayFixture }) {
  const expected = fixture.expectedAfter;
  const stale = fixture.blockedState === "STALE";
  const currentHorizonBad =
    fixture.comparison.find((row) => row.key === "horizon")?.state === "BROKEN";
  const afterHeading =
    fixture.action === "ROLL"
      ? copy.positionReview.timeRestored
      : expectedAfterCopy[fixture.action];
  return (
    <section className="exposure-comparison" aria-label={copy.exposure.title}>
      <article>
        <p className="eyebrow">{copy.positionReview.tradePlan}</p>
        <h3>{copy.positionReview.planHorizon}</h3>
        <ExposureValues exposure={fixture.intended} />
      </article>
      <article className={stale ? "exposure-comparison__stale" : undefined}>
        <p className="eyebrow">{copy.positionReview.positionNow}</p>
        <h3 className={currentHorizonBad ? "semantic-adverse" : undefined}>
          {currentHorizonBad
            ? copy.positionReview.timeTooShort
            : copy.scenarios[fixture.scenario].answer}
        </h3>
        {stale ? (
          <p className="observation-age semantic-adverse">
            {formatObservedAge(
              copy.positionReview.lastObserved,
              fixture.measured.dataAgeSeconds,
              true,
            )}
          </p>
        ) : null}
        <ExposureValues exposure={fixture.measured} dte={fixture.measured.dte} />
      </article>
      <article>
        <p className="eyebrow">{actionContextLabels[fixture.action]}</p>
        <h3 className={fixture.action === "ROLL" ? "semantic-positive" : undefined}>
          {afterHeading}
        </h3>
        {expected ? (
          <>
            <ExposureValues exposure={expected} />
            {fixture.rollProposal ? (
              <dl className="replacement-terms" aria-label={copy.positionReview.replacementTerms}>
                <div className="replacement-terms__heading">
                  <dt>{copy.positionReview.replacementTerms}</dt>
                  <dd>{copy.positionReview.sampleEstimate}</dd>
                </div>
                <div>
                  <dt>{copy.positionReview.replacementExpiry}</dt>
                  <dd>{fixture.rollProposal.expiry}</dd>
                </div>
                <div>
                  <dt>{copy.positionReview.replacementStrikes}</dt>
                  <dd>{fixture.rollProposal.strikes}</dd>
                </div>
                <div>
                  <dt>{copy.positionReview.replacementQuantity}</dt>
                  <dd>{fixture.rollProposal.quantity}</dd>
                </div>
                <div>
                  <dt>{copy.positionReview.estimatedRollCost}</dt>
                  <dd>
                    {fixture.rollProposal.estimatedCost} {copy.positionReview.debitPerSpread}
                  </dd>
                </div>
                <div>
                  <dt>{copy.positionReview.resultingMaxLoss}</dt>
                  <dd>{fixture.rollProposal.resultingMaxLoss}</dd>
                </div>
              </dl>
            ) : null}
          </>
        ) : (
          <p className="muted">{copy.positionReview.notCalculated}</p>
        )}
      </article>
      <div className="broker-check">
        <strong>{copy.positionReview.brokerPosition}</strong>
        <span>{copy.exposure.noBrokerOutcome}</span>
      </div>
    </section>
  );
}

function EvidencePanel({ fixture }: { fixture: ReplayFixture }) {
  const stale = fixture.blockedState === "STALE";
  return (
    <div className="detail-stack">
      <section className="sample-provenance" aria-labelledby="sample-provenance-title">
        <h3 id="sample-provenance-title">{copy.provenanceDetail.title}</h3>
        <dl className="compact-facts">
          <div>
            <dt>{copy.provenanceDetail.source}</dt>
            <dd>{fixture.provenance.source}</dd>
          </div>
          <div>
            <dt>{copy.provenanceDetail.observedAt}</dt>
            <dd>{fixture.provenance.observedAt}</dd>
          </div>
          <div>
            <dt>{copy.provenanceDetail.classification}</dt>
            <dd>{fixture.provenance.classification}</dd>
          </div>
          <div>
            <dt>{copy.provenanceDetail.support}</dt>
            <dd>
              {({
                SUPPORTED: copy.provenanceDetail.supported,
                CONTRADICTED: copy.provenanceDetail.contradicted,
                UNKNOWN: copy.provenanceDetail.unknown,
              })[fixture.provenance.support]}
            </dd>
          </div>
        </dl>
        {fixture.evidenceCards.length ? (
          <div className="evidence-cards">
            <p className="evidence-cards__note">{copy.provenanceDetail.fixtureEvidenceNote}</p>
            {fixture.evidenceCards.map((card) => (
              <dl className="evidence-card" key={card.sourceId}>
                <div>
                  <dt>{copy.provenanceDetail.sourceId}</dt>
                  <dd>{card.sourceId}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.headline}</dt>
                  <dd>{card.headline}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.observedAt}</dt>
                  <dd>{card.observedAt}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.eventCode}</dt>
                  <dd>{card.eventCode}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.relation}</dt>
                  <dd>{card.relation}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.materiality}</dt>
                  <dd>{card.materiality}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.relevance}</dt>
                  <dd>{card.relevance.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.confidence}</dt>
                  <dd>{card.confidence.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>{copy.provenanceDetail.sourceTier}</dt>
                  <dd>{sourceTierLabels[card.sourceTier]}</dd>
                </div>
              </dl>
            ))}
          </div>
        ) : fixture.evidenceStatus === "NOT_RUN" ? (
          <p className="muted">{copy.provenanceDetail.noClassification}</p>
        ) : null}
      </section>
      <section className="sample-observation" aria-labelledby="sample-observation-title">
        <h3 id="sample-observation-title">{copy.thesis.sampleObservation}</h3>
        <dl>
          <div>
            <dt>{copy.thesis.measuredExposure}</dt>
            <dd className="inline-measure">
              <span className="inline-measure__label">{copy.thesis.delta}</span>
              <span>{formatSigned(fixture.measured.delta)}</span>
              <span className="inline-measure__label">{copy.thesis.theta}</span>
              <span>{formatCurrency(fixture.measured.thetaPerDay)}</span>
              <span className="inline-measure__label">{copy.thesis.vega}</span>
              <span>{formatCurrency(fixture.measured.vega)}</span>
            </dd>
          </div>
          <div>
            <dt>{copy.thesis.whyItMatters}</dt>
            <dd>{copy.scenarios[fixture.scenario].summary}</dd>
          </div>
        </dl>
      </section>
      <section aria-labelledby="comparison-title">
        <h3 id="comparison-title">{copy.thesis.comparisonTitle}</h3>
        <p
          className="definition"
          aria-label={`${copy.positionReview.thesisDrift}. ${copy.positionReview.thesisDriftDefinition}`}
        >
          <strong>{copy.positionReview.thesisDrift}</strong>
          <span>{copy.positionReview.thesisDriftDefinition}</span>
        </p>
        <table className="comparison-table">
          <thead>
            <tr>
              <th scope="col">{copy.positionReview.evidencePlan}</th>
              <th scope="col">
                {stale
                  ? copy.positionReview.evidenceLastObserved
                  : copy.positionReview.evidenceCurrent}
              </th>
              <th scope="col">{copy.positionReview.evidenceStatus}</th>
            </tr>
          </thead>
          <tbody>
            {fixture.comparison.map((row) => (
              <tr key={row.key}>
                <th scope="row">
                  <span>{comparisonLabels[row.key]}</span>
                  <small>{thesisPromises[row.key]}</small>
                </th>
                <td>
                  <span>{currentMetricLabels[row.key]}</span>
                  <strong
                    aria-label={
                      row.key === "horizon"
                        ? `${row.measuredValue} ${copy.positionReview.dteLong}`
                        : undefined
                    }
                  >
                    {row.measuredValue}
                  </strong>
                </td>
                <td
                  className={
                    row.state === "BROKEN" || row.state === "WEAKENED"
                      ? "semantic-adverse"
                      : row.state === "ALIGNED"
                        ? "semantic-positive"
                        : undefined
                  }
                >
                  {row.state === "BROKEN" && row.key !== "evidence"
                    ? copy.thesis.outsidePlan
                    : matchLabels[row.state]}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section aria-labelledby="drift-title">
        <h3 id="drift-title">{copy.drift.title}</h3>
        <p className="muted drift-scale">{copy.drift.scale}</p>
        <dl className="drift-list">
          {fixture.drift.map((item) => (
            <div key={item.key}>
              <dt>{driftLabels[item.key]}</dt>
              <dd>{qualityLabels[item.quality]}</dd>
              <dd>
                {item.points}
                {copy.drift.scoreScale}
              </dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}

function ChoicesPanel({ fixture }: { fixture: ReplayFixture }) {
  return (
    <section aria-label={copy.decisionTrail.alternatives}>
      <h3>{copy.alternatives.title}</h3>
      <p className="muted">
        {fixture.blockedState ? copy.alternatives.blockedIntro : copy.alternatives.intro}
      </p>
      <dl className="choice-list">
        {fixture.alternatives.map((choice) => (
          <div key={choice.action}>
            <dt>
              {actionLabels[choice.action]} <span>{alternativeStateLabels[choice.state]}</span>
            </dt>
            <dd>{copy.alternatives[choice.reasonKey]}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ActivityPanel({ fixture }: { fixture: ReplayFixture }) {
  return (
    <section aria-labelledby="run-title">
      <h3 id="run-title">{copy.run.title}</h3>
      <p className="muted">{copy.run.intro}</p>
      <div className="technology-proof">
        <h4>{copy.run.technologyTitle}</h4>
        <ul>
          <li>{copy.run.tradingApi}</li>
          <li>{copy.run.mcp}</li>
          <li>{copy.run.model}</li>
          <li>{copy.run.cli}</li>
        </ul>
      </div>
      <AutonomousCycle fixture={fixture} />
      <ol className="activity-list">
        {stageCopy.map((stage, index) => (
          <li key={stage}>
            <span>{stage}</span>
            <strong>
              {index === 2 || index === 3 ? copy.run.notRun : copy.run.complete}
            </strong>
          </li>
        ))}
      </ol>
      <p className="sr-only" aria-live="polite">
        {copy.run.liveRegion}
      </p>
    </section>
  );
}

function RecordPanel({ fixture }: { fixture: ReplayFixture }) {
  return (
    <section aria-labelledby="record-title">
      <h3 id="record-title">{copy.positionReview.decisionRecord}</h3>
      <dl className="record-grid">
        <div>
          <dt>{copy.certificate.action}</dt>
          <dd>{actionLabels[fixture.action]}</dd>
        </div>
        <div>
          <dt>{copy.positionReview.orderStatus}</dt>
          <dd>{copy.positionReview.noOrder}</dd>
        </div>
        <div>
          <dt>{copy.positionReview.targetAtEntry}</dt>
          <dd>
            <ExposureValues exposure={fixture.intended} />
          </dd>
        </div>
        <div>
          <dt>{copy.positionReview.currentExposure}</dt>
          <dd>
            <ExposureValues exposure={fixture.measured} dte={fixture.measured.dte} />
          </dd>
        </div>
        <div>
          <dt>{copy.positionReview.expectedExposure}</dt>
          <dd>
            {fixture.expectedAfter ? (
              <ExposureValues exposure={fixture.expectedAfter} />
            ) : (
              copy.positionReview.notCalculated
            )}
          </dd>
        </div>
        <div>
          <dt>{copy.positionReview.brokerPosition}</dt>
          <dd>{copy.certificate.notApplicable}</dd>
        </div>
      </dl>
      <details>
        <summary>{copy.certificate.howChecked}</summary>
        <p>{copy.certificate.details}</p>
        <dl className="lineage-grid">
          <div>
            <dt>{copy.certificate.policyVersion}</dt>
            <dd>{fixture.lineage.policyVersion}</dd>
          </div>
          <div>
            <dt>{copy.certificate.fixtureVersion}</dt>
            <dd>{fixture.lineage.fixtureVersion}</dd>
          </div>
          <div>
            <dt>{copy.certificate.assessmentHash}</dt>
            <dd>{fixture.lineage.assessmentHash}</dd>
          </div>
          <div>
            <dt>{copy.certificate.inputHash}</dt>
            <dd>{fixture.lineage.inputHash}</dd>
          </div>
        </dl>
      </details>
      <p className="proof-limitation">{copy.certificate.limitations}</p>
    </section>
  );
}

function DetailTabs({
  fixture,
  activeTab,
  onActiveTabChange,
  keyboardNavigationVersion,
}: {
  fixture: ReplayFixture;
  activeTab: TabId;
  onActiveTabChange: (tab: TabId) => void;
  keyboardNavigationVersion: number;
}) {
  const tabRefs = useRef<Record<TabId, HTMLButtonElement | null>>({
    evidence: null,
    choices: null,
    activity: null,
    record: null,
  });
  const panelRefs = useRef<Record<TabId, HTMLDivElement | null>>({
    evidence: null,
    choices: null,
    activity: null,
    record: null,
  });
  useEffect(() => {
    const tab = tabRefs.current[activeTab];
    const tabList = tab?.parentElement;
    if (!tab || !tabList) return;
    const tabBounds = tab.getBoundingClientRect();
    const listBounds = tabList.getBoundingClientRect();
    if (tabBounds.left < listBounds.left) {
      tabList.scrollLeft += Math.floor(tabBounds.left - listBounds.left);
    } else if (tabBounds.right > listBounds.right) {
      tabList.scrollLeft += Math.ceil(tabBounds.right - listBounds.right);
    }
  }, [activeTab]);
  useEffect(() => {
    if (keyboardNavigationVersion === 0) return;
    const panel = panelRefs.current[activeTab];
    if (!panel) return;
    panel.focus({ preventScroll: true });
    panel.scrollIntoView?.({ block: "start" });
  }, [keyboardNavigationVersion]);
  const selectTab = (tab: TabId) => {
    onActiveTabChange(tab);
    tabRefs.current[tab]?.focus();
  };
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, tab: TabId) => {
    const index = tabIds.indexOf(tab);
    let next: TabId | null = null;
    if (event.key === "ArrowRight") next = tabIds[(index + 1) % tabIds.length];
    if (event.key === "ArrowLeft") next = tabIds[(index - 1 + tabIds.length) % tabIds.length];
    if (event.key === "Home") next = tabIds[0];
    if (event.key === "End") next = tabIds[tabIds.length - 1];
    if (next) {
      event.preventDefault();
      selectTab(next);
    }
  };
  return (
    <section className="details demo-sections" aria-label={copy.navigation.label}>
      <div className="tab-list" role="tablist" aria-label={copy.navigation.label}>
        {tabIds.map((tab) => (
          <button
            key={tab}
            ref={(node) => {
              tabRefs.current[tab] = node;
            }}
            id={`tab-${tab}`}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            aria-controls={`panel-${tab}`}
            tabIndex={activeTab === tab ? 0 : -1}
            onClick={() => onActiveTabChange(tab)}
            onKeyDown={(event) => handleKeyDown(event, tab)}
          >
            {tabLabels[tab]}
          </button>
        ))}
      </div>
      <div
        ref={(node) => {
          panelRefs.current.evidence = node;
        }}
        id="panel-evidence"
        role="tabpanel"
        aria-labelledby="tab-evidence"
        tabIndex={-1}
        hidden={activeTab !== "evidence"}
      >
        <div className="review-summary">
          <ChangedSummary fixture={fixture} />
          <DecisionSummary fixture={fixture} />
        </div>
        <ExposureComparison fixture={fixture} />
      </div>
      <div
        ref={(node) => {
          panelRefs.current.choices = node;
        }}
        id="panel-choices"
        role="tabpanel"
        aria-labelledby="tab-choices"
        tabIndex={-1}
        hidden={activeTab !== "choices"}
      >
        <OpeningThesis fixture={fixture} />
        <EvidencePanel fixture={fixture} />
      </div>
      <div
        ref={(node) => {
          panelRefs.current.activity = node;
        }}
        id="panel-activity"
        role="tabpanel"
        aria-labelledby="tab-activity"
        tabIndex={-1}
        hidden={activeTab !== "activity"}
      >
        <AcquisitionSummary fixture={fixture} />
        <MarketContext fixture={fixture} />
        <ActivityPanel fixture={fixture} />
      </div>
      <div
        ref={(node) => {
          panelRefs.current.record = node;
        }}
        id="panel-record"
        role="tabpanel"
        aria-labelledby="tab-record"
        tabIndex={-1}
        hidden={activeTab !== "record"}
      >
        <ChoicesPanel fixture={fixture} />
        <RecordPanel fixture={fixture} />
      </div>
    </section>
  );
}

export function ReplayShell({
  initialScenario = "THETA_TAKEOVER",
  operationalState = "READY",
  replayLoader,
  proofLoader,
  archiveLoader,
  runtimeLoader,
}: ReplayShellProps) {
  const [selectedScenario, setSelectedScenario] = useState(initialScenario);
  const [loadedFixture, setLoadedFixture] = useState<ReplayFixture | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [theme, setTheme] = useState<Theme>("dark");
  const [view, setView] = useState<WorkspaceView | null>(archiveLoader ? null : "demo");
  const [proof, setProof] = useState<CompetitionPerformanceProofResponse | null | undefined>(
    proofLoader ? undefined : null,
  );
  const [archive, setArchive] = useState<CompetitionRecordResponse | null | undefined>(
    archiveLoader ? undefined : null,
  );
  const [runtimeMode, setRuntimeMode] = useState<HealthResponse["runtime_mode"] | undefined>(
    runtimeLoader ? undefined : "CONNECTED",
  );
  const [activeTab, setActiveTab] = useState<TabId>("evidence");
  const [keyboardTabNavigationVersion, setKeyboardTabNavigationVersion] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [informationOpen, setInformationOpen] = useState<"privacy" | "important" | null>(null);
  const settingsButtonRef = useRef<HTMLButtonElement | null>(null);
  const informationTriggerRef = useRef<HTMLButtonElement | null>(null);
  const viewChosenRef = useRef(false);
  const initialViewResolvedRef = useRef(false);
  const fallback = replayFixtures[selectedScenario];

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    if (!runtimeLoader) return;
    let active = true;
    runtimeLoader().then(
      (result) => {
        if (active) setRuntimeMode(result.runtime_mode);
      },
      () => {
        if (active) setRuntimeMode("REPLAY_ONLY");
      },
    );
    return () => {
      active = false;
    };
  }, [runtimeLoader]);

  useEffect(() => {
    if (runtimeMode === undefined) return;
    if (runtimeMode === "REPLAY_ONLY") {
      setProof({
        schema_version: "v1",
        publication_status: "NOT_PUBLISHED",
        baseline_status: null,
        published_at: null,
        point: null,
        linked_certificate_ids: [],
        publication_hash: null,
        predecessor_hash: null,
      });
      return;
    }
    if (!proofLoader) {
      setProof(null);
      return;
    }
    let active = true;
    setProof(undefined);
    proofLoader().then(
      (result) => {
        if (!active) return;
        setProof(result);
        if (
          !archiveLoader
          && !viewChosenRef.current
          && !initialViewResolvedRef.current
        ) {
          initialViewResolvedRef.current = true;
          setView(result.publication_status === "PUBLISHED" ? "competition" : "demo");
        }
      },
      () => {
        if (!active) return;
        setProof(null);
        if (!archiveLoader && !viewChosenRef.current && !initialViewResolvedRef.current) {
          initialViewResolvedRef.current = true;
          setView("demo");
        }
      },
    );
    return () => {
      active = false;
    };
  }, [archiveLoader, proofLoader, runtimeMode]);

  useEffect(() => {
    if (runtimeMode === undefined) return;
    if (runtimeMode === "REPLAY_ONLY") {
      setArchive({ schema_version: "v1", publication_status: "NOT_PUBLISHED", records: [] });
      if (!viewChosenRef.current && !initialViewResolvedRef.current) {
        initialViewResolvedRef.current = true;
        setView("demo");
      }
      return;
    }
    if (!archiveLoader) {
      setArchive(null);
      return;
    }
    let active = true;
    setArchive(undefined);
    archiveLoader().then(
      (result) => {
        if (!active) return;
        setArchive(result);
        if (!viewChosenRef.current && !initialViewResolvedRef.current) {
          initialViewResolvedRef.current = true;
          setView(result.publication_status === "PUBLISHED" ? "competition" : "demo");
        }
      },
      () => {
        if (!active) return;
        setArchive(null);
        if (!viewChosenRef.current && !initialViewResolvedRef.current) {
          initialViewResolvedRef.current = true;
          setView("demo");
        }
      },
    );
    return () => {
      active = false;
    };
  }, [archiveLoader, runtimeMode]);

  const toggleTheme = () => {
    const nextTheme = theme === "dark" ? "light" : "dark";
    setTheme(nextTheme);
    document.documentElement.dataset.theme = nextTheme;
  };
  const selectView = (nextView: WorkspaceView) => {
    viewChosenRef.current = true;
    initialViewResolvedRef.current = true;
    setView(nextView);
  };
  useEffect(() => {
    if (!replayLoader) return;
    let active = true;
    setLoadFailed(false);
    replayLoader(selectedScenario, fallback).then(
      (result) => {
        if (active) setLoadedFixture(result);
      },
      () => {
        if (active) setLoadFailed(true);
      },
    );
    return () => {
      active = false;
    };
  }, [fallback, replayLoader, selectedScenario]);

  const fixture = loadedFixture?.scenario === selectedScenario ? loadedFixture : fallback;
  const currentNotice =
    operationalState !== "READY"
      ? operationalState
      : loadFailed
        ? "UNKNOWN"
        : replayLoader && loadedFixture?.scenario !== selectedScenario
          ? "COLD"
          : null;
  const shortcutsOnly = runtimeMode !== "CONNECTED";

  useEffect(() => {
    const handleReviewShortcut = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat) return;

      const isSettingsToggle = event.key === "?" && !event.altKey && !event.ctrlKey && !event.metaKey;
      if (isSettingsToggle) {
        if (isProtectedShortcutTarget(event.target)) return;
        event.preventDefault();
        setSettingsOpen((open) => !open);
        return;
      }

    };

    document.addEventListener("keydown", handleReviewShortcut);
    return () => document.removeEventListener("keydown", handleReviewShortcut);
  }, []);

  return (
    <div
      className={`app-shell${view === "demo" ? " app-shell--demo" : ""}`}
      data-layout="responsive"
    >
      <header className="site-header">
        <a className="brand" href="#position-review">
          <picture className="brand__art brand__art--dark" aria-hidden="true">
            <source media="(max-width: 760px)" srcSet={brandMarkUrl} data-brand-variant="mobile-mark" />
            <img src={brandLockupUrl} alt={copy.brand.name} data-brand-variant="desktop-lockup" />
          </picture>
          <picture className="brand__art brand__art--light" aria-hidden="true">
            <source media="(max-width: 760px)" srcSet={brandMarkLightUrl} data-brand-variant="mobile-mark" />
            <img src={brandLockupLightUrl} alt={copy.brand.name} data-brand-variant="desktop-lockup" />
          </picture>
          <span className="sr-only">{copy.brand.name}</span>
        </a>
        <div className="header-actions">
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <button
            ref={settingsButtonRef}
            className={`keyboard-trigger${shortcutsOnly ? " keyboard-trigger--icon" : ""}`}
            type="button"
            aria-haspopup="dialog"
            aria-expanded={settingsOpen}
            aria-keyshortcuts="?"
            aria-label={shortcutsOnly ? copy.keyboardGuide.toggleGuide : undefined}
            title={shortcutsOnly ? copy.keyboardGuide.toggleGuide : undefined}
            onClick={() => setSettingsOpen(true)}
          >
            {shortcutsOnly ? (
              <svg
                className="keyboard-trigger__icon"
                viewBox="0 0 24 16"
                aria-hidden="true"
              >
                <rect x="1" y="1" width="22" height="14" rx="2" />
                <path d="M4 5h2M9 5h2M14 5h2M19 5h1M4 9h2M9 9h2M14 9h2M19 9h1M6 12h12" />
              </svg>
            ) : copy.ownerSettings.entry}
          </button>
          <button
            type="button"
            className="header-environment"
            aria-haspopup="dialog"
            aria-expanded={informationOpen === "important"}
            onClick={(event) => {
              informationTriggerRef.current = event.currentTarget;
              setInformationOpen("important");
            }}
          >
            {copy.provenance.paperOnly}
          </button>
        </div>
      </header>
      <WorkspaceNavigation view={view} proof={proof} archive={archive} onChange={selectView} />
      <main id="position-review" className={view === "demo" ? "demo-main" : undefined}>
        {view === null ? (
          <div className="review-heading initial-view-loading" aria-live="polite">
            <h1>{copy.competitionRecord.loading}</h1>
          </div>
        ) : null}
        {view === "competition" ? (
          <>
            <div className="review-heading">
              <p className="eyebrow">
                {archive?.publication_status === "PUBLISHED"
                  ? copy.gateway.actualRecord
                  : copy.gateway.noPublishedRecord}
              </p>
              <h1>{copy.gateway.competitionTitle}</h1>
              <p>{copy.gateway.competitionIntro}</p>
            </div>
            {archiveLoader ? <CompetitionRecordView archive={archive} /> : null}
          </>
        ) : null}
        {view === "demo" ? (
          <div className="review-heading">
            <div>
              <p className="eyebrow">{copy.provenance.publicAccess}</p>
              <h1>{copy.positionReview.title}</h1>
            </div>
            <details className="view-help">
              <summary aria-label={copy.positionReview.helpLabel}>
                {copy.positionReview.helpSymbol}
              </summary>
              <p>{copy.positionReview.intro}</p>
            </details>
          </div>
        ) : null}
        {view === "competition" && proofLoader && (!archiveLoader || proof?.publication_status === "PUBLISHED") ? (
          <PerformanceProof proof={proof} />
        ) : null}
        {view === "demo" ? (
          <>
            {replayLoader && loadedFixture?.scenario !== selectedScenario ? null : (
              <PositionContext
                fixture={fixture}
                selected={selectedScenario}
                onSelect={setSelectedScenario}
              />
            )}
            <div className="provenance" role="status">
              <strong>{copy.provenance.banner}</strong>
            </div>
            {currentNotice ? (
              <div className="operational-state">
                <StateNotice state={currentNotice} />
              </div>
            ) : (
              <>
                {fixture.blockedState ? <StateNotice state={fixture.blockedState} /> : null}
                <DetailTabs
                  fixture={fixture}
                  activeTab={activeTab}
                  onActiveTabChange={setActiveTab}
                  keyboardNavigationVersion={keyboardTabNavigationVersion}
                />
              </>
            )}
          </>
        ) : null}
        {view === "setup" ? (
          <SetupView
            onOpenOwnerSettings={() => setSettingsOpen(true)}
            ownerControlsEnabled={runtimeMode === "CONNECTED"}
          />
        ) : null}
      </main>
      {view === "demo" ? (
        <div className="content-hint">
          <p>{copy.footer.keyboard}</p>
        </div>
      ) : null}
      <footer>
        <nav className="footer-links">
          <a href="/docs">{copy.footer.api}</a>
          <button
            type="button"
            onClick={(event) => {
              informationTriggerRef.current = event.currentTarget;
              setInformationOpen("privacy");
            }}
          >
            {copy.legal.privacyLink}
          </button>
          <button
            type="button"
            onClick={(event) => {
              informationTriggerRef.current = event.currentTarget;
              setInformationOpen("important");
            }}
          >
            {copy.legal.importantLink}
          </button>
        </nav>
      </footer>
      <OwnerSettings
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        triggerRef={settingsButtonRef}
        ownerControlsEnabled={runtimeMode === "CONNECTED"}
      />
      <InformationDialog
        kind={informationOpen}
        onClose={() => setInformationOpen(null)}
        triggerRef={informationTriggerRef}
      />
    </div>
  );
}
