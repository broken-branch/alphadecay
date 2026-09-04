import { copy as publicCopy } from "../content/copy";
import type { ExperimentHistoryProjection, ExperimentHistoryRecord } from "./experiment-history";
import "./experiment-history.css";

const copy = publicCopy.experiment.history;

export type ExperimentHistoryProps = {
  history: ExperimentHistoryProjection;
  selectedRecordKey: string | null;
  onSelectRecord?: (selectionKey: string) => void;
  indexOnly?: boolean;
};

function formatDate(value: string): string {
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

function EmptyState({ sourceState }: Pick<ExperimentHistoryProjection, "sourceState">) {
  const message = {
    PENDING: copy.loading,
    UNAVAILABLE: copy.unavailable,
    NOT_PUBLISHED: copy.notPublished,
    PUBLISHED: copy.notPublished,
  }[sourceState];
  return <p className="experiment-history__empty">{message}</p>;
}

function RecordDetail({ record }: { record: ExperimentHistoryRecord }) {
  return (
    <article className="experiment-history__detail" aria-labelledby={`experiment-record-${record.selectionKey}`}>
      <header>
        <p className="experiment-history__eyebrow">{copy.record}</p>
        <h3 id={`experiment-record-${record.selectionKey}`}>{record.label}</h3>
        <p className="experiment-history__state">{record.state}</p>
      </header>

      <section aria-labelledby={`experiment-thesis-${record.selectionKey}`}>
        <h4 id={`experiment-thesis-${record.selectionKey}`}>{copy.recordedThesis}</h4>
        {record.thesis ? (
          <dl className="experiment-history__facts">
            <div><dt>{copy.direction}</dt><dd>{record.thesis.direction}</dd></div>
            <div><dt>{copy.volatilityView}</dt><dd>{record.thesis.volatilityView}</dd></div>
            <div><dt>{copy.targetReview}</dt><dd>{formatDate(record.thesis.targetAt)}</dd></div>
          </dl>
        ) : <p className="experiment-history__muted">{copy.thesisUnavailable}</p>}
      </section>

      <div className="experiment-history__availability">
        <section aria-labelledby={`experiment-risk-${record.selectionKey}`}>
          <h4 id={`experiment-risk-${record.selectionKey}`}>{copy.risk}</h4>
          <dl><dt>{copy.maximumRisk}</dt><dd>{copy.riskUnavailable}</dd></dl>
        </section>
        <section aria-labelledby={`experiment-result-${record.selectionKey}`}>
          <h4 id={`experiment-result-${record.selectionKey}`}>{copy.result}</h4>
          <p>{record.kind === "NO_TRADE" ? copy.noTradeResult : copy.resultUnavailable}</p>
        </section>
      </div>

      <section className="experiment-history__decision" aria-labelledby={`experiment-decision-${record.selectionKey}`}>
        <h4 id={`experiment-decision-${record.selectionKey}`}>{copy.latestDecision}</h4>
        <dl className="experiment-history__facts">
          <div><dt>{copy.decision}</dt><dd>{record.latestDecision.action}</dd></div>
          <div><dt>{copy.reason}</dt><dd>{record.latestDecision.reason}</dd></div>
          <div><dt>{copy.decided}</dt><dd>{formatDate(record.latestDecision.occurredAt)}</dd></div>
        </dl>
      </section>
    </article>
  );
}

export function ExperimentHistory({
  history,
  selectedRecordKey,
  onSelectRecord,
  indexOnly = false,
}: ExperimentHistoryProps) {
  const selected = history.records.find((record) => record.selectionKey === selectedRecordKey) ?? null;

  return (
    <section
      className={`experiment-history${indexOnly ? " experiment-history--index" : ""}`}
      aria-labelledby="experiment-history-title"
    >
      <header className="experiment-history__header">
        <div>
          <p className="experiment-history__eyebrow">{copy.published}</p>
          <h2 id="experiment-history-title">{copy.title}</h2>
          <p>{copy.intro}</p>
        </div>
        {!indexOnly ? <p className="experiment-history__limitation">{copy.limitation}</p> : null}
      </header>

      {history.records.length === 0 ? <EmptyState sourceState={history.sourceState} /> : (
        <div className={`experiment-history__layout${indexOnly ? " experiment-history__layout--index" : ""}`}>
          <ol className="experiment-history__index" aria-label={copy.title}>
            {history.records.map((record) => (
              <li key={record.selectionKey}>
                <button
                  type="button"
                  aria-current={record.selectionKey === selectedRecordKey ? "true" : undefined}
                  onClick={() => onSelectRecord?.(record.selectionKey)}
                >
                  <time dateTime={record.publishedAt}>{formatDate(record.publishedAt)}</time>
                  <strong>{record.label}</strong>
                  <span>{record.state}</span>
                </button>
              </li>
            ))}
          </ol>
          {!indexOnly && selected ? <RecordDetail record={selected} /> : null}
          {!indexOnly && !selected ? (
            <p className="experiment-history__empty experiment-history__empty--detail">{copy.chooseRecord}</p>
          ) : null}
        </div>
      )}
    </section>
  );
}
