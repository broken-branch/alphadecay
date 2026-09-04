import { useId, useState } from "react";
import type { ChangeEvent, FormEvent, ReactNode } from "react";
import { copy as publicCopy } from "../content/copy";
import { emptyStrategyIntake, mergeImportedStrategy, parseStrategyText } from "./parser";
import type {
  StrategyBriefSource,
  StrategyDraftRequest,
  StrategyHorizon,
  StrategyIntakeErrors,
  StrategyIntakeFields,
} from "./types";

const copy = publicCopy.strategyIntake;
import "./strategy-intake.css";

const MAX_IMPORT_BYTES = 20_000;
const acceptedExtensions = [".txt", ".md"];

type StrategyIntakeProps = {
  initialValue?: Partial<StrategyIntakeFields>;
  onDraftReady?: (draft: StrategyDraftRequest) => void;
  onDraftChange?: () => void;
  busy?: boolean;
  reviewResult?: ReactNode;
  ownerAuthenticated?: boolean;
};

const directionOptions = [
  ["BULLISH", copy.form.bullish],
  ["BEARISH", copy.form.bearish],
  ["NEUTRAL", copy.form.neutral],
  ["UNSURE", copy.form.unsure],
] as const;

const horizonOptions: ReadonlyArray<[StrategyHorizon, string]> = [
  ["INTRADAY", copy.form.intraday],
  ["DAYS", copy.form.days],
  ["WEEKS", copy.form.weeks],
  ["MONTHS", copy.form.months],
  ["UNSURE", copy.form.horizonUnsure],
];

function normalizedFields(fields: StrategyIntakeFields): StrategyIntakeFields {
  return Object.fromEntries(
    Object.entries(fields).map(([key, value]) => [key, typeof value === "string" ? value.trim() : value]),
  ) as StrategyIntakeFields;
}

function validate(fields: StrategyIntakeFields): StrategyIntakeErrors {
  const errors: StrategyIntakeErrors = {};
  for (const key of ["market", "thesis", "horizon", "evidence", "invalidation"] as const) {
    if (!fields[key]) errors[key] = copy.form.required;
  }
  if (fields.thesis && fields.thesis.length < 20) errors.thesis = copy.form.thesisTooShort;
  for (const key of ["evidence", "invalidation"] as const) {
    const items = listItems(fields[key]);
    if (items.length > 12) errors[key] = copy.form.tooManyItems;
    else if (items.some((item) => item.length > 1_000)) errors[key] = copy.form.itemTooLong;
  }
  const risk = Number(fields.maximumRiskUsd);
  if (!Number.isFinite(risk) || risk <= 0) errors.maximumRiskUsd = copy.form.invalidRisk;
  return errors;
}

function listItems(value: string): string[] {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function toDraft(fields: StrategyIntakeFields, source: StrategyBriefSource): StrategyDraftRequest {
  const horizon = horizonOptions.find(([key]) => key === fields.horizon)?.[1] ?? fields.horizon;
  return {
    source: source.kind === "PASTED_TEXT" ? { ...source, content: fields.thesis } : source,
    market_scope: fields.market,
    direction: fields.direction,
    horizon,
    evidence: listItems(fields.evidence),
    invalidation: listItems(fields.invalidation),
    risk_budget: { max_loss_dollars: Number(fields.maximumRiskUsd).toFixed(2) },
    ...(fields.notes ? { notes: fields.notes } : {}),
  };
}

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () =>
      typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("FILE_READ_FAILED")),
    );
    reader.addEventListener("error", () => reject(reader.error ?? new Error("FILE_READ_FAILED")));
    reader.readAsText(file);
  });
}

function FieldMessage({ id, message }: { id: string; message?: string }) {
  return message ? <span className="strategy-field__error" id={id}>{message}</span> : null;
}

function DraftPreview({ fields, ready }: { fields: StrategyIntakeFields; ready: boolean }) {
  const value = (text: string) => text || copy.preview.unset;
  const risk = fields.maximumRiskUsd
    ? `${copy.preview.currency}${fields.maximumRiskUsd}`
    : copy.preview.unset;
  const direction = directionOptions.find(([key]) => key === fields.direction)?.[1] ?? copy.preview.unset;
  const horizon = horizonOptions.find(([key]) => key === fields.horizon)?.[1] ?? copy.preview.unset;
  return (
    <aside className="strategy-preview" aria-labelledby="strategy-preview-title" aria-live="polite">
      <div className="strategy-preview__heading">
        <div>
          <p className="strategy-intake__eyebrow">{copy.preview.eyebrow}</p>
          <h2 id="strategy-preview-title">{copy.preview.title}</h2>
        </div>
        <span className="strategy-preview__status">{copy.preview.status}</span>
      </div>
      <div className="strategy-preview__safety" aria-label={`${copy.preview.notArmed}. ${copy.preview.noOrder}.`}>
        <span>{copy.preview.notArmed}</span>
        <span>{copy.preview.noOrder}</span>
      </div>
      <p className="strategy-preview__thesis">{value(fields.thesis)}</p>
      <dl className="strategy-preview__facts">
        <div><dt>{copy.preview.market}</dt><dd>{value(fields.market)}</dd></div>
        <div><dt>{copy.preview.direction}</dt><dd>{direction}</dd></div>
        <div><dt>{copy.preview.horizon}</dt><dd>{horizon}</dd></div>
        <div><dt>{copy.preview.risk}</dt><dd>{risk}</dd></div>
      </dl>
      <div className="strategy-preview__conditions">
        <section>
          <h3>{copy.preview.evidence}</h3>
          <p>{value(fields.evidence)}</p>
        </section>
        <section>
          <h3>{copy.preview.invalidation}</h3>
          <p>{value(fields.invalidation)}</p>
        </section>
        {fields.notes ? <section><h3>{copy.preview.notes}</h3><p>{fields.notes}</p></section> : null}
      </div>
      <div className="strategy-preview__next">
        <h3>{ready ? copy.preview.readyTitle : copy.preview.nextTitle}</h3>
        <p>{ready ? copy.preview.readyBody : copy.preview.nextBody}</p>
      </div>
    </aside>
  );
}

export function StrategyIntake({
  initialValue,
  onDraftReady,
  onDraftChange,
  busy = false,
  reviewResult,
  ownerAuthenticated = true,
}: StrategyIntakeProps) {
  const fieldBaseId = useId();
  const [fields, setFields] = useState<StrategyIntakeFields>(() => ({
    ...emptyStrategyIntake(),
    ...initialValue,
  }));
  const [errors, setErrors] = useState<StrategyIntakeErrors>({});
  const [importMessage, setImportMessage] = useState<string | null>(null);
  const [draftReady, setDraftReady] = useState(false);
  const [source, setSource] = useState<StrategyBriefSource>({ kind: "PASTED_TEXT", content: "" });

  const update = <Key extends keyof StrategyIntakeFields>(key: Key, value: StrategyIntakeFields[Key]) => {
    setFields((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: undefined }));
    setDraftReady(false);
    onDraftChange?.();
  };

  const fieldId = (name: keyof StrategyIntakeFields) => `${fieldBaseId}-${name}`;
  const errorId = (name: keyof StrategyIntakeFields) => `${fieldId(name)}-error`;

  const importFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const lowerName = file.name.toLowerCase();
    if (![...acceptedExtensions, ".markdown"].some((extension) => lowerName.endsWith(extension))) {
      setImportMessage(copy.import.unsupported);
      return;
    }
    if (file.size > MAX_IMPORT_BYTES) {
      setImportMessage(copy.import.tooLarge);
      return;
    }
    try {
      const content = await readFile(file);
      const parsed = parseStrategyText(content);
      setFields((current) => mergeImportedStrategy(current, parsed));
      setSource({
        kind: lowerName.endsWith(".txt") ? "TEXT_FILE" : "MARKDOWN_FILE",
        content,
        filename: file.name,
      });
      setErrors({});
      setDraftReady(false);
      setImportMessage(copy.import.loaded);
      onDraftChange?.();
    } catch {
      setImportMessage(copy.import.failed);
    }
  };

  const reviewDraft = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const normalized = normalizedFields(fields);
    const nextErrors = validate(normalized);
    setFields(normalized);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) {
      setDraftReady(false);
      const firstInvalid = ([
        "market",
        "thesis",
        "horizon",
        "evidence",
        "invalidation",
        "maximumRiskUsd",
      ] as const).find((name) => nextErrors[name]);
      if (firstInvalid) document.getElementById(fieldId(firstInvalid))?.focus();
      return;
    }
    const draft = toDraft(normalized, source);
    setDraftReady(true);
    onDraftReady?.(draft);
  };

  return (
    <section className="strategy-intake">
      <header className="strategy-intake__intro">
        <p className="strategy-intake__eyebrow">{copy.intro.eyebrow}</p>
        <h1>{copy.intro.title}</h1>
        <p>{copy.intro.body}</p>
        <small>{copy.intro.privacy}</small>
      </header>

      <div className="strategy-intake__workspace">
        <form className="strategy-form" onSubmit={reviewDraft} noValidate>
          <div className="strategy-form__heading">
            <h2>{copy.form.title}</h2>
            <p>{copy.form.requiredNote}</p>
          </div>

          <label className="strategy-field" htmlFor={fieldId("market")}>
            <span>{copy.form.market} <em>{copy.form.required}</em></span>
            <input
              id={fieldId("market")}
              maxLength={120}
              value={fields.market}
              placeholder={copy.form.marketPlaceholder}
              aria-describedby={`${fieldId("market")}-hint${errors.market ? ` ${errorId("market")}` : ""}`}
              aria-invalid={Boolean(errors.market)}
              onChange={(event) => update("market", event.target.value)}
            />
            <small id={`${fieldId("market")}-hint`}>{copy.form.marketHint}</small>
            <FieldMessage id={errorId("market")} message={errors.market} />
          </label>

          <label className="strategy-field" htmlFor={fieldId("thesis")}>
            <span>{copy.form.thesis} <em>{copy.form.required}</em></span>
            <textarea
              id={fieldId("thesis")}
              rows={5}
              maxLength={20_000}
              value={fields.thesis}
              placeholder={copy.form.thesisPlaceholder}
              aria-describedby={`${fieldId("thesis")}-hint${errors.thesis ? ` ${errorId("thesis")}` : ""}`}
              aria-invalid={Boolean(errors.thesis)}
              onChange={(event) => update("thesis", event.target.value)}
            />
            <small id={`${fieldId("thesis")}-hint`}>{copy.form.thesisHint}</small>
            <FieldMessage id={errorId("thesis")} message={errors.thesis} />
          </label>

          <fieldset className="strategy-choice">
            <legend>{copy.form.direction}</legend>
            <div>
              {directionOptions.map(([value, label]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name={`${fieldBaseId}-direction`}
                    value={value}
                    checked={fields.direction === value}
                    onChange={() => update("direction", value)}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <label className="strategy-field" htmlFor={fieldId("horizon")}>
            <span>{copy.form.horizon} <em>{copy.form.required}</em></span>
            <select
              id={fieldId("horizon")}
              value={fields.horizon}
              aria-describedby={errors.horizon ? errorId("horizon") : undefined}
              aria-invalid={Boolean(errors.horizon)}
              onChange={(event) => update("horizon", event.target.value as StrategyIntakeFields["horizon"])}
            >
              <option value="">{copy.form.horizonPlaceholder}</option>
              {horizonOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
            <FieldMessage id={errorId("horizon")} message={errors.horizon} />
          </label>

          <div className="strategy-form__paired">
            <label className="strategy-field" htmlFor={fieldId("evidence")}>
              <span>{copy.form.evidence} <em>{copy.form.required}</em></span>
              <textarea
                id={fieldId("evidence")}
                rows={4}
                maxLength={12_011}
                value={fields.evidence}
                placeholder={copy.form.evidencePlaceholder}
                aria-describedby={`${fieldId("evidence")}-hint${errors.evidence ? ` ${errorId("evidence")}` : ""}`}
                aria-invalid={Boolean(errors.evidence)}
                onChange={(event) => update("evidence", event.target.value)}
              />
              <small id={`${fieldId("evidence")}-hint`}>{copy.form.evidenceHint}</small>
              <FieldMessage id={errorId("evidence")} message={errors.evidence} />
            </label>
            <label className="strategy-field" htmlFor={fieldId("invalidation")}>
              <span>{copy.form.invalidation} <em>{copy.form.required}</em></span>
              <textarea
                id={fieldId("invalidation")}
                rows={4}
                maxLength={12_011}
                value={fields.invalidation}
                placeholder={copy.form.invalidationPlaceholder}
                aria-describedby={`${fieldId("invalidation")}-hint${errors.invalidation ? ` ${errorId("invalidation")}` : ""}`}
                aria-invalid={Boolean(errors.invalidation)}
                onChange={(event) => update("invalidation", event.target.value)}
              />
              <small id={`${fieldId("invalidation")}-hint`}>{copy.form.invalidationHint}</small>
              <FieldMessage id={errorId("invalidation")} message={errors.invalidation} />
            </label>
          </div>

          <label className="strategy-field strategy-field--risk" htmlFor={fieldId("maximumRiskUsd")}>
            <span>{copy.form.risk} <em>{copy.form.required}</em></span>
            <span className="strategy-field__money">
              <span aria-hidden="true">{copy.form.riskPrefix}</span>
              <input
                id={fieldId("maximumRiskUsd")}
                type="number"
                min="0.01"
                step="0.01"
                inputMode="decimal"
                value={fields.maximumRiskUsd}
                placeholder={copy.form.riskPlaceholder}
                aria-describedby={`${fieldId("maximumRiskUsd")}-hint${errors.maximumRiskUsd ? ` ${errorId("maximumRiskUsd")}` : ""}`}
                aria-invalid={Boolean(errors.maximumRiskUsd)}
                onChange={(event) => update("maximumRiskUsd", event.target.value)}
              />
            </span>
            <small id={`${fieldId("maximumRiskUsd")}-hint`}>{copy.form.riskHint}</small>
            <FieldMessage id={errorId("maximumRiskUsd")} message={errors.maximumRiskUsd} />
          </label>

          <label className="strategy-field" htmlFor={fieldId("notes")}>
            <span>{copy.form.notes}</span>
            <textarea
              id={fieldId("notes")}
              rows={3}
              maxLength={4_000}
              value={fields.notes}
              placeholder={copy.form.notesPlaceholder}
              aria-describedby={`${fieldId("notes")}-hint`}
              onChange={(event) => update("notes", event.target.value)}
            />
            <small id={`${fieldId("notes")}-hint`}>{copy.form.notesHint}</small>
          </label>

          <section className="strategy-import" aria-labelledby={`${fieldBaseId}-import-title`}>
            <div>
              <h3 id={`${fieldBaseId}-import-title`}>{copy.import.title}</h3>
              <p>{copy.import.body}</p>
            </div>
            <label className="strategy-import__button">
              <span>{copy.import.action}</span>
              <input type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" onChange={importFile} />
            </label>
            <small>{copy.import.accepted}</small>
            {importMessage ? <p className="strategy-import__message" role="status">{importMessage}</p> : null}
          </section>

          <details className="strategy-template">
            <summary>{copy.template.title}</summary>
            <p>{copy.template.body}</p>
            <pre>{[
              copy.template.market,
              copy.template.thesis,
              copy.template.direction,
              copy.template.horizon,
              copy.template.evidence,
              copy.template.invalidation,
              copy.template.risk,
              copy.template.notes,
            ].join("\n")}</pre>
          </details>

          {Object.keys(errors).length ? <p className="strategy-form__error" role="alert">{copy.form.fixFields}</p> : null}
          <button className="strategy-form__review" type="submit" disabled={busy} aria-busy={busy}>
            {busy
              ? copy.response.loading
              : ownerAuthenticated
                ? copy.form.createDraft
                : copy.form.createDraftOwner}
          </button>
          {!ownerAuthenticated ? (
            <p className="strategy-form__owner-note">{copy.form.ownerSignInNote}</p>
          ) : null}
        </form>

        <div className="strategy-intake__aside">
          <DraftPreview fields={fields} ready={draftReady} />
          {reviewResult}
        </div>
      </div>
    </section>
  );
}
