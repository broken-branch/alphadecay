import { useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { copy as publicCopy } from "../content/copy";
import type {
  CurationBlockingQuestion,
  CurationConfidence,
  CurationReadiness,
  CuratedDirection,
  CuratedStructure,
  StrategyCurationResponse,
  StrategyProtocolFields,
} from "./contracts";
import "./strategy-protocol.css";

const copy = publicCopy.strategyProtocol;

type TextRuleKey = Exclude<keyof StrategyProtocolFields, "invalidation_rules">;
type EditableRules = Record<TextRuleKey, string> & { invalidation_rules: string };
type RuleKey = keyof EditableRules;
type RuleErrors = Partial<Record<RuleKey, string>>;

export type CuratedProtocolReviewProps = {
  protocol: StrategyCurationResponse;
  onProtocolFieldsChange?: (fields: StrategyProtocolFields) => void;
  onReview: (fields: StrategyProtocolFields) => void;
  onBack?: () => void;
  busy?: boolean;
  reviewComplete?: boolean;
};

const directionLabels: Record<CuratedDirection, string> = {
  BULLISH: copy.direction.bullish,
  BEARISH: copy.direction.bearish,
  NEUTRAL: copy.direction.neutral,
  UNSURE: copy.direction.unsure,
};
const structureLabels: Record<CuratedStructure, string> = {
  BULL_CALL_DEBIT_SPREAD: copy.structure.bullCall,
  BEAR_PUT_DEBIT_SPREAD: copy.structure.bearPut,
  IRON_CONDOR: copy.structure.ironCondor,
  REVIEW_REQUIRED: copy.structure.reviewRequired,
};
const readinessLabels: Record<CurationReadiness, string> = {
  READY: copy.readiness.ready,
  NEEDS_INPUT: copy.readiness.needsInput,
  CONFLICT_REVIEW: copy.readiness.conflictReview,
};
const confidenceLabels: Record<CurationConfidence, string> = {
  LOW: copy.confidence.low,
  MEDIUM: copy.confidence.medium,
  HIGH: copy.confidence.high,
};
const questionLabels: Record<CurationBlockingQuestion, string> = {
  MARKET_SCOPE_REQUIRED: copy.questions.market,
  DIRECTION_REVIEW_REQUIRED: copy.questions.direction,
  HORIZON_REQUIRED: copy.questions.horizon,
  EVIDENCE_REQUIRED: copy.questions.evidence,
  RISK_BUDGET_REQUIRED: copy.questions.risk,
  ENTRY_RULE_REQUIRED: copy.questions.entry,
  NO_TRADE_RULE_REQUIRED: copy.questions.noTrade,
  PROFIT_EXIT_REQUIRED: copy.questions.profit,
  LOSS_EXIT_REQUIRED: copy.questions.loss,
  TIME_EXIT_REQUIRED: copy.questions.time,
  INVALIDATION_REQUIRED: copy.questions.invalidation,
  STRUCTURE_REVIEW_REQUIRED: copy.questions.structure,
};
const ruleOrder: RuleKey[] = [
  "entry_rule",
  "no_trade_rule",
  "profit_exit_rule",
  "loss_exit_rule",
  "time_exit_rule",
  "invalidation_rules",
];
const ruleCopy: Record<RuleKey, { label: string; hint: string }> = {
  entry_rule: copy.rules.entry,
  no_trade_rule: copy.rules.noTrade,
  profit_exit_rule: copy.rules.profit,
  loss_exit_rule: copy.rules.loss,
  time_exit_rule: copy.rules.time,
  invalidation_rules: copy.rules.invalidation,
};

function editableRules(fields: StrategyProtocolFields): EditableRules {
  return {
    entry_rule: fields.entry_rule ?? "",
    no_trade_rule: fields.no_trade_rule ?? "",
    profit_exit_rule: fields.profit_exit_rule ?? "",
    loss_exit_rule: fields.loss_exit_rule ?? "",
    time_exit_rule: fields.time_exit_rule ?? "",
    invalidation_rules: fields.invalidation_rules.join("\n"),
  };
}

function protocolFields(rules: EditableRules): StrategyProtocolFields {
  const nullable = (value: string) => value.trim() ? value : null;
  return {
    entry_rule: nullable(rules.entry_rule),
    no_trade_rule: nullable(rules.no_trade_rule),
    profit_exit_rule: nullable(rules.profit_exit_rule),
    loss_exit_rule: nullable(rules.loss_exit_rule),
    time_exit_rule: nullable(rules.time_exit_rule),
    invalidation_rules: rules.invalidation_rules
      .split("\n")
      .filter((rule) => Boolean(rule.trim())),
  };
}

function Classification({ label, value }: { label: string; value: string | number }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

export function CuratedProtocolReview({
  protocol,
  onProtocolFieldsChange,
  onReview,
  onBack,
  busy = false,
  reviewComplete = false,
}: CuratedProtocolReviewProps) {
  const fieldBaseId = useId();
  const [rules, setRules] = useState<EditableRules>(() => editableRules(protocol.protocol_fields));
  const [errors, setErrors] = useState<RuleErrors>({});
  const fieldRefs = useRef<Partial<Record<RuleKey, HTMLTextAreaElement | null>>>({});
  const classifications = protocol.classifications;

  const updateRule = (key: RuleKey, value: string) => {
    const next = { ...rules, [key]: value };
    setRules(next);
    setErrors((current) => ({ ...current, [key]: undefined }));
    onProtocolFieldsChange?.(protocolFields(next));
  };

  const review = (event: FormEvent) => {
    event.preventDefault();
    const nextErrors: RuleErrors = {};
    for (const key of ruleOrder) {
      if (!rules[key].trim()) nextErrors[key] = copy.rules.required;
    }
    const invalidationRules = protocolFields(rules).invalidation_rules;
    if (invalidationRules.length > 12) nextErrors.invalidation_rules = copy.rules.tooMany;
    if (invalidationRules.some((rule) => rule.length > 2_000)) {
      nextErrors.invalidation_rules = copy.rules.tooLong;
    }
    setErrors(nextErrors);
    const firstInvalid = ruleOrder.find((key) => nextErrors[key]);
    if (firstInvalid) {
      fieldRefs.current[firstInvalid]?.focus();
      return;
    }
    onReview(protocolFields(rules));
  };

  return (
    <section className="protocol-review" aria-labelledby={`${fieldBaseId}-title`}>
      <header className="protocol-review__header">
        {onBack ? (
          <button className="protocol-review__back" type="button" onClick={onBack}>
            {copy.form.back}
          </button>
        ) : null}
        <p className="protocol-review__eyebrow">{copy.header.eyebrow}</p>
        <h1 id={`${fieldBaseId}-title`}>{copy.header.title}</h1>
        <p>{copy.header.intro}</p>
      </header>

      <div className="protocol-review__state" aria-label={copy.state.label}>
        <strong>{reviewComplete ? copy.form.complete : copy.state.reviewRequired}</strong>
        <span>{copy.state.curated}</span>
        <span>{copy.state.automationOff}</span>
        <span>{copy.state.noOrder}</span>
      </div>

      <div className="protocol-review__layout">
        <aside className="protocol-review__classification" aria-labelledby={`${fieldBaseId}-classification`}>
          <div>
            <p className="protocol-review__eyebrow">{copy.classification.eyebrow}</p>
            <h2 id={`${fieldBaseId}-classification`}>{copy.classification.title}</h2>
            <p>{copy.classification.intro}</p>
          </div>
          <dl className="protocol-review__facts">
            <Classification label={copy.classification.direction} value={directionLabels[classifications.direction]} />
            <Classification label={copy.classification.structure} value={structureLabels[classifications.structure]} />
            <Classification label={copy.classification.clarity} value={readinessLabels[classifications.clarity]} />
            <Classification label={copy.classification.evidence} value={readinessLabels[classifications.evidence]} />
            <Classification label={copy.classification.risk} value={readinessLabels[classifications.risk]} />
            <Classification label={copy.classification.exits} value={readinessLabels[classifications.exit]} />
            <Classification label={copy.classification.confidence} value={confidenceLabels[classifications.confidence]} />
            <Classification label={copy.classification.evidenceLinks} value={protocol.supporting_evidence.length} />
          </dl>
          <section className="protocol-review__questions" aria-labelledby={`${fieldBaseId}-questions`}>
            <h3 id={`${fieldBaseId}-questions`}>{copy.questions.title}</h3>
            {protocol.blocking_questions.length ? (
              <ul>
                {protocol.blocking_questions.map((question) => (
                  <li key={question}>{questionLabels[question]}</li>
                ))}
              </ul>
            ) : <p>{copy.questions.none}</p>}
          </section>
          <section className="protocol-review__boundaries" aria-labelledby={`${fieldBaseId}-boundaries`}>
            <h3 id={`${fieldBaseId}-boundaries`}>{copy.boundaries.title}</h3>
            <ul>
              <li>{copy.boundaries.paper}</li>
              <li>{copy.boundaries.options}</li>
              <li>{copy.boundaries.definedRisk}</li>
              <li>{copy.boundaries.notArmed}</li>
            </ul>
          </section>
        </aside>

        <form className="protocol-review__form" onSubmit={review} noValidate>
          <header>
            <p className="protocol-review__eyebrow">{copy.form.eyebrow}</p>
            <h2>{copy.form.title}</h2>
            <p>{copy.form.intro}</p>
          </header>
          {Object.values(errors).some(Boolean) ? (
            <p className="protocol-review__error-summary" role="alert">{copy.form.fixRules}</p>
          ) : null}
          <div className="protocol-review__fields">
            {ruleOrder.map((key) => {
              const fieldId = `${fieldBaseId}-${key}`;
              const errorId = `${fieldId}-error`;
              return (
                <div className="protocol-review__field" key={key}>
                  <label htmlFor={fieldId}>{ruleCopy[key].label}</label>
                  <textarea
                    ref={(node) => { fieldRefs.current[key] = node; }}
                    id={fieldId}
                    rows={key === "invalidation_rules" ? 5 : 3}
                    maxLength={key === "invalidation_rules" ? 24_011 : 2_000}
                    value={rules[key]}
                    aria-invalid={Boolean(errors[key])}
                    aria-describedby={`${fieldId}-hint${errors[key] ? ` ${errorId}` : ""}`}
                    onChange={(event) => updateRule(key, event.target.value)}
                  />
                  <small id={`${fieldId}-hint`}>{ruleCopy[key].hint}</small>
                  {errors[key] ? <small className="protocol-review__field-error" id={errorId}>{errors[key]}</small> : null}
                </div>
              );
            })}
          </div>
          <div className="protocol-review__actions">
            <div>
              <strong>{copy.form.reviewNote}</strong>
              <span>{reviewComplete ? copy.form.completeDetail : copy.form.reviewBoundary}</span>
            </div>
            <button type="submit" disabled={busy || reviewComplete} aria-busy={busy}>
              {reviewComplete ? copy.form.complete : busy ? copy.form.reviewing : copy.form.review}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}
