import { useId, useState } from "react";
import type { FormEvent } from "react";
import { copy as publicCopy } from "../content/copy";
import type { StrategyCurationResponse } from "../strategy-protocol";
import {
  buildReviewedExecutableProtocolRequest,
  emptyProtocolBuilderDraft,
  protocolFacts,
  protocolMetrics,
} from "./builder";
import type {
  ComparisonOperator,
  ProtocolBuilderDraft,
  ProtocolBuilderErrors,
  ReviewedExecutableProtocolRequest,
  RuleDraft,
} from "./types";
import "./protocol-builder.css";

const copy = publicCopy.protocolBuilder;
type ScalarField = Exclude<keyof ProtocolBuilderDraft,
  "entryRule" | "noTradeRule" | "profitExitRule" | "lossExitRule" | "timeExitRule"
  | "invalidationRules">;
type RuleField = "entryRule" | "noTradeRule" | "profitExitRule" | "lossExitRule" | "timeExitRule";

const metricLabels = copy.metric;
const factLabels = copy.factOption;
const operatorLabels: Record<ComparisonOperator, string> = {
  LESS_THAN: copy.lessThan,
  LESS_THAN_OR_EQUAL: copy.lessThanOrEqual,
  EQUAL: copy.equal,
  GREATER_THAN_OR_EQUAL: copy.greaterThanOrEqual,
  GREATER_THAN: copy.greaterThan,
};

export type ProtocolBuilderProps = {
  curation: StrategyCurationResponse;
  initialDraft?: ProtocolBuilderDraft;
  onComplete: (request: ReviewedExecutableProtocolRequest) => void;
  submitting?: boolean;
  submitLabel?: string;
  submittingLabel?: string;
  boundaryText?: string;
};

function issueText(code: string | undefined): string | null {
  if (!code) return null;
  return copy.issue[code as keyof typeof copy.issue] ?? copy.issue.required;
}

function InputField({
  id, label, value, type = "text", error, hint, onChange,
}: {
  id: string;
  label: string;
  value: string;
  type?: "text" | "number" | "date" | "datetime-local";
  error?: string;
  hint?: string;
  onChange: (value: string) => void;
}) {
  const errorText = issueText(error);
  return (
    <div className="protocol-builder__field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type={type}
        value={value}
        aria-invalid={Boolean(errorText)}
        aria-describedby={`${hint ? `${id}-hint` : ""}${errorText ? ` ${id}-error` : ""}`.trim() || undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint ? <small id={`${id}-hint`}>{hint}</small> : null}
      {errorText ? <small id={`${id}-error`} className="protocol-builder__error">{errorText}</small> : null}
    </div>
  );
}

function SelectField({ id, label, value, error, onChange, children }: {
  id: string;
  label: string;
  value: string;
  error?: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  const errorText = issueText(error);
  return (
    <div className="protocol-builder__field">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        aria-invalid={Boolean(errorText)}
        aria-describedby={errorText ? `${id}-error` : undefined}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{copy.choose}</option>
        {children}
      </select>
      {errorText ? <small id={`${id}-error`} className="protocol-builder__error">{errorText}</small> : null}
    </div>
  );
}

function RuleEditor({
  id,
  title,
  sourceText,
  value,
  error,
  onChange,
}: {
  id: string;
  title: string;
  sourceText: string;
  value: RuleDraft;
  error?: string;
  onChange: (value: RuleDraft) => void;
}) {
  const update = <K extends keyof RuleDraft>(key: K, next: RuleDraft[K]) => {
    onChange({ ...value, [key]: next });
  };
  return (
    <fieldset className="protocol-builder__rule" aria-describedby={error ? `${id}-error` : undefined}>
      <legend>{title}</legend>
      <p>{sourceText}</p>
      <div className="protocol-builder__rule-controls">
        <SelectField
          id={`${id}-kind`}
          label={copy.predicateKind}
          value={value.predicateKind}
          onChange={(next) => update("predicateKind", next as RuleDraft["predicateKind"])}
        >
          <option value="NUMERIC">{copy.numeric}</option>
          <option value="FACT">{copy.fact}</option>
        </SelectField>
        {value.predicateKind === "NUMERIC" ? (
          <>
            <SelectField id={`${id}-left`} label={copy.leftMetric} value={value.leftMetric} onChange={(next) => update("leftMetric", next as RuleDraft["leftMetric"])}>
              {protocolMetrics.map((metric) => <option value={metric} key={metric}>{metricLabels[metric]}</option>)}
            </SelectField>
            <SelectField id={`${id}-operator`} label={copy.operator} value={value.operator} onChange={(next) => update("operator", next as RuleDraft["operator"])}>
              {Object.entries(operatorLabels).map(([operator, label]) => <option value={operator} key={operator}>{label}</option>)}
            </SelectField>
            <SelectField id={`${id}-right-kind`} label={copy.rightKind} value={value.rightKind} onChange={(next) => update("rightKind", next as RuleDraft["rightKind"])}>
              <option value="METRIC">{copy.rightMetric}</option>
              <option value="CONSTANT">{copy.rightNumber}</option>
            </SelectField>
            {value.rightKind === "METRIC" ? (
              <SelectField id={`${id}-right-metric`} label={copy.rightMetric} value={value.rightMetric} onChange={(next) => update("rightMetric", next as RuleDraft["rightMetric"])}>
                {protocolMetrics.map((metric) => <option value={metric} key={metric}>{metricLabels[metric]}</option>)}
              </SelectField>
            ) : null}
            {value.rightKind === "CONSTANT" ? (
              <InputField id={`${id}-right-value`} label={copy.rightNumber} value={value.rightValue} type="number" onChange={(next) => update("rightValue", next)} />
            ) : null}
          </>
        ) : null}
        {value.predicateKind === "FACT" ? (
          <>
            <SelectField id={`${id}-fact`} label={copy.factLabel} value={value.fact} onChange={(next) => update("fact", next as RuleDraft["fact"])}>
              {protocolFacts.map((fact) => <option value={fact} key={fact}>{factLabels[fact]}</option>)}
            </SelectField>
            <SelectField id={`${id}-expected`} label={copy.expected} value={value.expected} onChange={(next) => update("expected", next as RuleDraft["expected"])}>
              <option value="true">{copy.true}</option>
              <option value="false">{copy.false}</option>
            </SelectField>
          </>
        ) : null}
      </div>
      {error ? <small id={`${id}-error`} className="protocol-builder__error">{issueText(error)}</small> : null}
    </fieldset>
  );
}

export function ProtocolBuilder({
  curation,
  initialDraft,
  onComplete,
  submitting = false,
  submitLabel = copy.complete,
  submittingLabel = copy.completing,
  boundaryText = copy.boundary,
}: ProtocolBuilderProps) {
  const baseId = useId();
  const [draft, setDraft] = useState(() => initialDraft ?? emptyProtocolBuilderDraft(curation));
  const [errors, setErrors] = useState<ProtocolBuilderErrors>({});
  const clearError = (field: string) => setErrors((current) => {
    const next = { ...current };
    delete next[field];
    return next;
  });
  const setField = (field: ScalarField, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    clearError(field);
  };
  const setRule = (field: RuleField, value: RuleDraft) => {
    setDraft((current) => ({ ...current, [field]: value }));
    clearError(field);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    const result = buildReviewedExecutableProtocolRequest(curation, draft);
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    onComplete(result.value);
  };

  const fields = (items: Array<[ScalarField, string, "text" | "number" | "date" | "datetime-local", string?]>) => (
    <div className="protocol-builder__grid">
      {items.map(([field, label, type, hint]) => (
        <InputField
          key={field}
          id={`${baseId}-${field}`}
          label={label}
          type={type}
          value={draft[field] as string}
          error={errors[field]}
          hint={hint}
          onChange={(value) => setField(field, value)}
        />
      ))}
    </div>
  );

  const ruleItems: Array<[RuleField, string, string | null]> = [
    ["entryRule", copy.entryRule, curation.protocol_fields.entry_rule],
    ["noTradeRule", copy.noTradeRule, curation.protocol_fields.no_trade_rule],
    ["profitExitRule", copy.profitRule, curation.protocol_fields.profit_exit_rule],
    ["lossExitRule", copy.lossRule, curation.protocol_fields.loss_exit_rule],
    ["timeExitRule", copy.timeRule, curation.protocol_fields.time_exit_rule],
  ];
  const qualityHasError = [
    "maximumUnderlyingAgeSeconds",
    "maximumOptionQuoteAgeSeconds",
    "maximumLegQuoteSkewSeconds",
    "maximumRelativeSpreadPercent",
    "minimumLegBidSize",
    "minimumLegAskSize",
  ].some((field) => Boolean(errors[field]));

  return (
    <form className="protocol-builder" onSubmit={submit} noValidate aria-labelledby={`${baseId}-title`}>
      <header className="protocol-builder__header">
        <p className="protocol-builder__eyebrow">{copy.eyebrow}</p>
        <h1 id={`${baseId}-title`}>{copy.title}</h1>
        <p>{copy.intro}</p>
        <strong>{boundaryText}</strong>
        {errors.curation ? <small className="protocol-builder__error">{issueText(errors.curation)}</small> : null}
      </header>
      {Object.keys(errors).length ? <p className="protocol-builder__summary" role="alert">{copy.missingSummary}</p> : null}

      <section><header><h2>{copy.identityTitle}</h2><p>{copy.identityIntro}</p></header>
        {fields([
          ["opportunityKey", copy.opportunityKey, "text"], ["definitionVersion", copy.definitionVersion, "number"],
          ["benchmarkSymbol", copy.benchmark, "text"], ["allowedEventCodes", copy.eventCodes, "text", copy.eventCodesHint],
          ["thesisCode", copy.thesisCode, "text"], ["invalidationCodes", copy.invalidationCodes, "text", copy.invalidationCodesHint],
        ])}
      </section>

      <section><header><h2>{copy.structureTitle}</h2><p>{copy.structureIntro}</p></header>
        <div className="protocol-builder__grid">
          <SelectField id={`${baseId}-direction`} label={copy.direction} value={draft.direction} error={errors.direction} onChange={(value) => setField("direction", value)}>
            <option value="BULLISH">{copy.bullish}</option><option value="BEARISH">{copy.bearish}</option>
          </SelectField>
          <SelectField id={`${baseId}-structure`} label={copy.structure} value={draft.structure} error={errors.direction} onChange={(value) => setField("structure", value)}>
            <option value="BULL_CALL_DEBIT_SPREAD">{copy.bullCall}</option><option value="BEAR_PUT_DEBIT_SPREAD">{copy.bearPut}</option>
          </SelectField>
        </div>
      </section>

      <section><header><h2>{copy.scheduleTitle}</h2><p>{copy.scheduleIntro}</p></header>
        {fields([
          ["dailyStartSession", copy.dailyStart, "date"], ["preEventSession", copy.preEvent, "date"],
          ["eventSession", copy.event, "date"], ["reactionSession", copy.reaction, "date"],
          ["signalSession", copy.signal, "date"], ["evidenceWindowStart", copy.evidenceStart, "datetime-local"],
          ["evidenceWindowEnd", copy.evidenceEnd, "datetime-local"], ["entryWindowStart", copy.entryStart, "datetime-local"],
          ["decisionBoundary", copy.decision, "datetime-local"], ["entryWindowEnd", copy.entryEnd, "datetime-local"],
        ])}
        {errors.sessions || errors.windows ? <p className="protocol-builder__section-error">{issueText(errors.sessions ?? errors.windows)}</p> : null}
      </section>

      <section><header><h2>{copy.selectionTitle}</h2><p>{copy.selectionIntro}</p></header>
        {fields([
          ["minimumExpiry", copy.minimumExpiry, "date"], ["maximumExpiry", copy.maximumExpiry, "date"],
          ["minimumDte", copy.minimumDte, "number"], ["targetDte", copy.targetDte, "number"],
          ["maximumDte", copy.maximumDte, "number"], ["minimumStrike", copy.minimumStrike, "number"],
          ["maximumStrike", copy.maximumStrike, "number"], ["widthDollars", copy.width, "number"],
          ["quantity", copy.quantity, "number"], ["maximumDebitPerShare", copy.maximumDebit, "number"],
          ["maximumLossDollars", copy.maximumLoss, "number"], ["maximumContractsConsidered", copy.contractsConsidered, "number"],
        ])}
        {errors.expiry ? <p className="protocol-builder__section-error">{issueText(errors.expiry)}</p> : null}
      </section>

      <details className="protocol-builder__advanced" open={qualityHasError || undefined}>
        <summary>{copy.qualitySummary}</summary>
        <div><header><h2>{copy.qualityTitle}</h2><p>{copy.qualityIntro}</p></header>
          {fields([
            ["maximumUnderlyingAgeSeconds", copy.underlyingAge, "number"],
            ["maximumOptionQuoteAgeSeconds", copy.quoteAge, "number"],
            ["maximumLegQuoteSkewSeconds", copy.quoteSkew, "number"],
            ["maximumRelativeSpreadPercent", copy.relativeSpread, "number"],
            ["minimumLegBidSize", copy.bidSize, "number"], ["minimumLegAskSize", copy.askSize, "number"],
          ])}
        </div>
      </details>

      <section><header><h2>{copy.rulesTitle}</h2><p>{copy.rulesIntro}</p></header>
        <div className="protocol-builder__rules">
          {ruleItems.map(([field, title, source]) => (
            <RuleEditor key={field} id={`${baseId}-${field}`} title={title} sourceText={source ?? ""} value={draft[field]} error={errors[field]} onChange={(value) => setRule(field, value)} />
          ))}
          {curation.protocol_fields.invalidation_rules.map((source, index) => (
            <RuleEditor
              key={`${index}-${source}`}
              id={`${baseId}-invalidation-${index}`}
              title={`${copy.invalidationRule} ${index + 1}`}
              sourceText={source}
              value={draft.invalidationRules[index]}
              error={errors[`invalidationRule.${index}`]}
              onChange={(value) => {
                setDraft((current) => ({
                  ...current,
                  invalidationRules: current.invalidationRules.map(
                    (item, itemIndex) => itemIndex === index ? value : item,
                  ),
                }));
                clearError(`invalidationRule.${index}`);
              }}
            />
          ))}
        </div>
      </section>

      <footer className="protocol-builder__footer">
        <p>{boundaryText}</p>
        <button type="submit" disabled={submitting} aria-busy={submitting}>
          {submitting ? submittingLabel : submitLabel}
        </button>
      </footer>
    </form>
  );
}
