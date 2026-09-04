import { useRef, useState } from "react";
import { StrategyDraftRequestError, strategyDraftClient } from "./api";
import type { StrategyDraftClient } from "./api";
import type {
  CandidateStructureFamily,
  EvidenceCheck,
  ExitRequirement,
  PromotionRequirement,
  ProtocolAssumption,
  ProtocolQuestion,
  StrategyDraftResponse,
} from "./contracts";
import { copy as publicCopy } from "../content/copy";
import {
  ReviewedExperimentRequestError,
  reviewedExperimentClient,
} from "../experiments";
import type {
  ReviewedExperimentClient,
  ReviewedExperimentDefinition,
} from "../experiments";
import {
  CuratedProtocolReview,
  StrategyCurationRequestError,
  strategyCurationClient,
} from "../strategy-protocol";
import type {
  StrategyCurationClient,
  StrategyCurationBrief,
  StrategyCurationResponse,
  StrategyProtocolFields,
} from "../strategy-protocol";
import { StrategyIntake } from "./StrategyIntake";
import type { StrategyDraftRequest, StrategyIntakeFields } from "./types";

const copy = publicCopy.strategyIntake;

type OwnerStrategySession = {
  authenticated: boolean;
  csrfToken: string | null;
};

type ConnectedStrategyIntakeProps = {
  session: OwnerStrategySession;
  onSessionRejected?: () => void;
  initialValue?: Partial<StrategyIntakeFields>;
  client?: StrategyDraftClient;
  curationClient?: StrategyCurationClient;
  experimentClient?: Pick<ReviewedExperimentClient, "create">;
  onExperimentSaved?: (experiment: ReviewedExperimentDefinition) => void;
};

const familyLabels: Record<CandidateStructureFamily, string> = {
  BULL_CALL_DEBIT_SPREAD: copy.response.bullCall,
  BEAR_PUT_DEBIT_SPREAD: copy.response.bearPut,
  IRON_CONDOR: copy.response.ironCondor,
};
const evidenceCheckLabels: Record<EvidenceCheck, string> = {
  VERIFY_THESIS_CLAIMS: copy.response.verifyClaims,
  CHECK_MARKET_DATA_RECENCY: copy.response.freshMarketData,
  CHECK_OPTION_LIQUIDITY: copy.response.optionLiquidity,
  CHECK_INVALIDATION_STATE: copy.response.invalidationState,
};
const questionLabels: Record<ProtocolQuestion, string> = {
  MARKET_SCOPE_REQUIRED: copy.response.marketQuestion,
  DIRECTION_REQUIRED: copy.response.directionQuestion,
  HORIZON_REQUIRED: copy.response.horizonQuestion,
  EVIDENCE_REQUIRED: copy.response.evidenceQuestion,
  INVALIDATION_REQUIRED: copy.response.invalidationQuestion,
  RISK_BUDGET_REQUIRED: copy.response.riskQuestion,
  DIRECTION_REVIEW_REQUIRED: copy.response.directionReviewQuestion,
};
const promotionLabels: Record<PromotionRequirement, string> = {
  MODEL_CURATION_REQUIRED: copy.response.modelCuration,
  EVIDENCE_REVIEW_REQUIRED: copy.response.evidenceReview,
  RISK_REVIEW_REQUIRED: copy.response.riskReview,
  OWNER_REVIEW_REQUIRED: copy.response.ownerReview,
};
const exitLabels: Record<ExitRequirement, string> = {
  PROFIT_EXIT_REQUIRED: copy.response.profitExit,
  LOSS_EXIT_REQUIRED: copy.response.lossExit,
  TIME_EXIT_REQUIRED: copy.response.timeExit,
};
const assumptionLabels: Record<ProtocolAssumption, string> = {
  USER_BRIEF_UNVERIFIED: copy.response.unverifiedBrief,
  OPTIONS_ONLY: copy.response.optionsOnly,
  PAPER_ONLY: copy.response.paperOnly,
  DEFINED_RISK_ONLY: copy.response.definedRiskOnly,
};

function RuleList({ items }: { items: string[] }) {
  return (
    <ul className="strategy-response__list">
      {items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
    </ul>
  );
}

function StrategyDraftReview({
  draft,
  onCurate,
  curationBusy,
  curationError,
}: {
  draft: StrategyDraftResponse;
  onCurate: () => void;
  curationBusy: boolean;
  curationError: string | null;
}) {
  const families = draft.structure_constraints.candidate_families.map((family) => familyLabels[family]);
  const budget = draft.risk_rules.budget;
  return (
    <section className="strategy-response" aria-labelledby="strategy-response-title">
      <header className="strategy-response__heading">
        <p className="strategy-intake__eyebrow">{copy.response.eyebrow}</p>
        <h2 id="strategy-response-title">{copy.response.title}</h2>
        <p>{copy.response.intro}</p>
      </header>
      <dl className="strategy-response__state">
        <div><dt>{copy.response.statusLabel}</dt><dd>{copy.response.statusValue}</dd></div>
        <div><dt>{copy.response.curationLabel}</dt><dd>{copy.response.curationValue}</dd></div>
        <div><dt>{copy.response.automationLabel}</dt><dd>{copy.response.automationValue}</dd></div>
        <div><dt>{copy.response.executionLabel}</dt><dd>{copy.response.executionValue}</dd></div>
      </dl>
      <section>
        <h3>{copy.response.structureTitle}</h3>
        <RuleList items={[
          copy.response.optionsOnly,
          copy.response.definedRisk,
          copy.response.noNaked,
        ]} />
        <dl className="strategy-response__single">
          <dt>{copy.response.candidateLabel}</dt>
          <dd>{families.length ? families.join(", ") : copy.response.candidateNone}</dd>
        </dl>
      </section>
      <section>
        <h3>{copy.response.riskTitle}</h3>
        <RuleList items={[copy.response.boundedLoss, copy.response.budgetFit]} />
        {budget ? (
          <dl className="strategy-response__single">
            {budget.max_loss_dollars ? <><dt>{copy.response.riskLimit}</dt><dd>{copy.response.currency}{budget.max_loss_dollars}</dd></> : null}
            {budget.max_account_percent ? <><dt>{copy.response.accountRiskLimit}</dt><dd>{budget.max_account_percent}{copy.response.percentSuffix}</dd></> : null}
          </dl>
        ) : null}
      </section>
      <section>
        <h3>{copy.response.evidenceTitle}</h3>
        <RuleList items={draft.evidence_plan.required_checks.map((check) => evidenceCheckLabels[check])} />
        <div className="strategy-response__user-text">
          <strong>{copy.response.submittedEvidence}</strong>
          <RuleList items={draft.evidence_plan.submitted_evidence} />
        </div>
      </section>
      <section>
        <h3>{copy.response.questionsTitle}</h3>
        {draft.questions.length
          ? <RuleList items={draft.questions.map((question) => questionLabels[question])} />
          : <p>{copy.response.questionsNone}</p>}
      </section>
      <section>
        <h3>{copy.response.promotionTitle}</h3>
        <RuleList items={draft.required_before_promotion.map((item) => promotionLabels[item])} />
      </section>
      <section>
        <h3>{copy.response.exitTitle}</h3>
        <RuleList items={draft.exit_rules.required_before_promotion.map((item) => exitLabels[item])} />
        {draft.exit_rules.invalidation.length ? (
          <div className="strategy-response__user-text">
            <strong>{copy.response.invalidationLabel}</strong>
            <RuleList items={draft.exit_rules.invalidation} />
          </div>
        ) : null}
      </section>
      <details className="strategy-response__assumptions">
        <summary>{copy.response.assumptionsTitle}</summary>
        <RuleList items={draft.assumptions.map((item) => assumptionLabels[item])} />
      </details>
      <div className="strategy-response__curation">
        <p>{copy.response.curateBoundary}</p>
        {curationError ? <p className="strategy-response__curation-error" role="alert">{curationError}</p> : null}
        <button type="button" disabled={curationBusy} aria-busy={curationBusy} onClick={onCurate}>
          {curationBusy ? copy.response.curating : copy.response.curate}
        </button>
      </div>
    </section>
  );
}

function errorCopy(error: unknown): string {
  if (!(error instanceof StrategyDraftRequestError)) return copy.response.unavailable;
  if (error.status === 401 || error.status === 403) return copy.response.sessionExpired;
  if (error.status === 422) return copy.response.inputRejected;
  return copy.response.unavailable;
}

function curationErrorCopy(error: unknown): string {
  if (!(error instanceof StrategyCurationRequestError)) return copy.response.curationUnavailable;
  if (error.status === 401 || error.status === 403) return copy.response.curationSessionExpired;
  if (error.status === 422) return copy.response.curationInputRejected;
  return copy.response.curationUnavailable;
}

function emptyProtocolFields(request: StrategyDraftRequest): StrategyProtocolFields {
  return {
    entry_rule: null,
    no_trade_rule: null,
    profit_exit_rule: null,
    loss_exit_rule: null,
    time_exit_rule: null,
    invalidation_rules: request.invalidation,
  };
}

function curationBrief(request: StrategyDraftRequest): StrategyCurationBrief {
  return {
    source: {
      ...request.source,
      filename: request.source.filename ?? null,
    },
    market_scope: request.market_scope,
    direction: request.direction,
    horizon: request.horizon,
    evidence: request.evidence,
    invalidation: request.invalidation,
    risk_budget: {
      max_loss_dollars: request.risk_budget.max_loss_dollars ?? null,
      max_account_percent: null,
    },
    notes: request.notes ?? null,
  };
}

function reviewIsComplete(protocol: StrategyCurationResponse): boolean {
  const readiness = protocol.classifications;
  return protocol.blocking_questions.length === 0
    && readiness.structure !== "REVIEW_REQUIRED"
    && readiness.clarity === "READY"
    && readiness.evidence === "READY"
    && readiness.risk === "READY"
    && readiness.exit === "READY";
}

export function ConnectedStrategyIntake({
  session,
  onSessionRejected,
  initialValue,
  client = strategyDraftClient,
  curationClient = strategyCurationClient,
  experimentClient = reviewedExperimentClient,
  onExperimentSaved,
}: ConnectedStrategyIntakeProps) {
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<StrategyDraftResponse | null>(null);
  const [submittedBrief, setSubmittedBrief] = useState<StrategyDraftRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [curation, setCuration] = useState<StrategyCurationResponse | null>(null);
  const [curationBusy, setCurationBusy] = useState(false);
  const [curationError, setCurationError] = useState<string | null>(null);
  const [curationVersion, setCurationVersion] = useState(0);
  const [reviewComplete, setReviewComplete] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const requestVersion = useRef(0);

  const createDraft = async (request: StrategyDraftRequest) => {
    if (!session.authenticated || !session.csrfToken) {
      setDraft(null);
      setError(copy.response.sessionRequired);
      return;
    }
    setBusy(true);
    setDraft(null);
    setError(null);
    const version = ++requestVersion.current;
    try {
      const response = await client.create(request, session.csrfToken);
      if (requestVersion.current === version) {
        setDraft(response);
        setSubmittedBrief(request);
      }
    } catch (requestError) {
      if (requestVersion.current === version) {
        setError(errorCopy(requestError));
        if (
          requestError instanceof StrategyDraftRequestError &&
          (requestError.status === 401 || requestError.status === 403)
        ) {
          onSessionRejected?.();
        }
      }
    } finally {
      if (requestVersion.current === version) setBusy(false);
    }
  };

  const clearReview = () => {
    requestVersion.current += 1;
    setBusy(false);
    setDraft(null);
    setSubmittedBrief(null);
    setError(null);
    setCuration(null);
    setCurationBusy(false);
    setCurationError(null);
    setReviewComplete(false);
    setSaveBusy(false);
    setSaveError(null);
    setSaved(false);
  };

  const curate = async (fields?: StrategyProtocolFields) => {
    if (!session.authenticated || !session.csrfToken || !submittedBrief) {
      setCurationError(copy.response.sessionRequired);
      return;
    }
    setCurationBusy(true);
    setCurationError(null);
    const version = ++requestVersion.current;
    try {
      const response = await curationClient.create(
        {
          brief: curationBrief(submittedBrief),
          protocol_fields: fields ?? emptyProtocolFields(submittedBrief),
        },
        session.csrfToken,
      );
      if (requestVersion.current === version) {
        setCuration(response);
        setCurationVersion((current) => current + 1);
        setReviewComplete(reviewIsComplete(response));
        setSaved(false);
      }
    } catch (requestError) {
      if (requestVersion.current === version) {
        setCurationError(curationErrorCopy(requestError));
        if (
          requestError instanceof StrategyCurationRequestError
          && (requestError.status === 401 || requestError.status === 403)
        ) {
          onSessionRejected?.();
        }
      }
    } finally {
      if (requestVersion.current === version) setCurationBusy(false);
    }
  };

  const editBrief = () => {
    requestVersion.current += 1;
    setCuration(null);
    setCurationBusy(false);
    setCurationError(null);
    setReviewComplete(false);
    setSaveError(null);
    setSaved(false);
  };

  const saveExperiment = async () => {
    if (!session.authenticated || !session.csrfToken || !curation || !reviewComplete) {
      setSaveError(publicCopy.strategyProtocol.form.saveSessionRequired);
      return;
    }
    setSaveBusy(true);
    setSaveError(null);
    try {
      const experiment = await experimentClient.create(
        {
          original_thesis: curation.intake,
          reviewed_protocol: curation.protocol_fields,
          curation,
        },
        session.csrfToken,
      );
      setSaved(true);
      onExperimentSaved?.(experiment);
    } catch (requestError) {
      if (
        requestError instanceof ReviewedExperimentRequestError
        && (requestError.status === 401 || requestError.status === 403)
      ) {
        setSaveError(publicCopy.strategyProtocol.form.saveSessionExpired);
        onSessionRejected?.();
      } else if (requestError instanceof ReviewedExperimentRequestError && requestError.status === 422) {
        setSaveError(publicCopy.strategyProtocol.form.saveInputRejected);
      } else {
        setSaveError(publicCopy.strategyProtocol.form.saveUnavailable);
      }
    } finally {
      setSaveBusy(false);
    }
  };

  const reviewResult = error ? (
    <section className="strategy-response strategy-response--error" role="alert">
      <p>{error}</p>
    </section>
  ) : draft ? (
    <StrategyDraftReview
      draft={draft}
      onCurate={() => void curate()}
      curationBusy={curationBusy}
      curationError={curationError}
    />
  ) : null;

  return (
    <>
      <div className="connected-intake-draft" hidden={curation !== null}>
        <StrategyIntake
          initialValue={initialValue}
          onDraftReady={createDraft}
          onDraftChange={clearReview}
          busy={busy || curationBusy}
          reviewResult={reviewResult}
          ownerAuthenticated={session.authenticated}
        />
      </div>
      {curation ? (
        <div className="connected-protocol-review">
          {curationError ? (
            <p className="connected-protocol-review__error" role="alert">{curationError}</p>
          ) : null}
          <CuratedProtocolReview
            key={curationVersion}
            protocol={curation}
            onReview={(fields) => void curate(fields)}
            onBack={editBrief}
            busy={curationBusy}
            reviewComplete={reviewComplete}
          />
          {reviewComplete ? (
            <section className="connected-protocol-review__save" aria-labelledby="save-experiment-title">
              <div>
                <h2 id="save-experiment-title">{publicCopy.strategyProtocol.form.saveTitle}</h2>
                <p>{publicCopy.strategyProtocol.form.saveBoundary}</p>
              </div>
              {saveError ? <p role="alert">{saveError}</p> : null}
              {saved ? <p role="status">{publicCopy.strategyProtocol.form.saved}</p> : null}
              <button
                type="button"
                disabled={saveBusy || saved}
                aria-busy={saveBusy}
                onClick={() => void saveExperiment()}
              >
                {saved
                  ? publicCopy.strategyProtocol.form.savedButton
                  : saveBusy
                    ? publicCopy.strategyProtocol.form.saving
                    : publicCopy.strategyProtocol.form.save}
              </button>
            </section>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
