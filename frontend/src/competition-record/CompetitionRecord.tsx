import { copy } from "../content/copy";
import type { CompetitionRecord, CompetitionRecordResponse, PositionRecord } from "./api";

type CompetitionRecordViewProps = {
  archive: CompetitionRecordResponse | null | undefined;
};

function formatUtc(value: string): string {
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
  const number = Number(value);
  return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function formatMoney(value: string): string {
  const amount = Number(value);
  const absolute = Math.abs(amount).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${amount > 0 ? "+" : amount < 0 ? "−" : ""}$${absolute}`;
}

function spreadLabel(spread: PositionRecord["payload"]["opening_spread"]): string {
  return `${formatDecimal(spread.long_strike)} / ${formatDecimal(spread.short_strike)} ${spread.option_type.toLowerCase()} vertical`;
}

function eventLabel(event: PositionRecord["payload"]["events"][number]): string {
  if (event.event_kind === "EXECUTION") {
    return {
      ENTRY: copy.competitionRecord.entryFilled,
      ROLL: copy.competitionRecord.rollFilled,
      CLOSE: copy.competitionRecord.closeFilled,
    }[event.action];
  }
  return {
    HOLD: copy.competitionRecord.holdReview,
    CLOSE: copy.competitionRecord.closeReview,
    ROLL: copy.competitionRecord.rollReview,
    NO_ACTION: copy.competitionRecord.noActionReview,
  }[event.action];
}

function eventDetail(event: PositionRecord["payload"]["events"][number]): string {
  if (event.event_kind === "EXECUTION") {
    return `${formatMoney(event.cashflow_usd)} · ${copy.competitionRecord.paperFill}`;
  }
  return {
    POSITION_REVIEWED: copy.competitionRecord.positionReviewed,
    RISK_REDUCTION: copy.competitionRecord.riskReduction,
    THESIS_CHANGED: copy.competitionRecord.thesisChanged,
    POSITION_ADJUSTMENT: copy.competitionRecord.positionAdjustment,
    DATA_INCOMPLETE: copy.competitionRecord.dataIncomplete,
  }[event.reason_category];
}

function cashflowClass(event: PositionRecord["payload"]["events"][number]): string | undefined {
  if (event.event_kind !== "EXECUTION") return undefined;
  const value = Number(event.cashflow_usd);
  return value > 0 ? "semantic-positive" : value < 0 ? "semantic-adverse" : undefined;
}

function isPositionRecord(record: CompetitionRecord): record is PositionRecord {
  return record.payload.record_kind === "POSITION";
}

function NoTradeRecord({ record }: { record: CompetitionRecord }) {
  if (record.payload.record_kind !== "NO_TRADE") return null;
  return (
    <article className="competition-record-card competition-record-card--no-trade">
      <div className="competition-record-card__heading">
        <div>
          <p className="eyebrow">{copy.competitionRecord.decision}</p>
          <h2>{copy.competitionRecord.noTrade}</h2>
        </div>
        <time dateTime={record.payload.decided_at}>{formatUtc(record.payload.decided_at)}</time>
      </div>
      <p>{copy.competitionRecord.strategyNotReady}</p>
      <p className="competition-record-id">
        {copy.competitionRecord.recordId} <span>{record.public_record_id.slice(0, 12)}</span>
      </p>
    </article>
  );
}

function PositionRecordCard({ record }: { record: PositionRecord }) {
  const position = record.payload;
  const spread = position.current_spread ?? position.opening_spread;
  return (
    <article className="competition-record-card competition-record-card--position">
      <div className="competition-record-card__heading">
        <div>
          <p className="eyebrow">{copy.competitionRecord.paperPosition}</p>
          <h2>{position.underlying}</h2>
        </div>
        <span className={`position-state position-state--${position.state.toLowerCase()}`}>
          {position.state === "OPEN" ? copy.competitionRecord.open : copy.competitionRecord.closed}
        </span>
      </div>
      <dl className="competition-position-summary">
        <div>
          <dt>{copy.competitionRecord.spread}</dt>
          <dd>{spreadLabel(spread)}</dd>
        </div>
        <div>
          <dt>{copy.competitionRecord.expiry}</dt>
          <dd>{spread.expiration}</dd>
        </div>
        <div>
          <dt>{copy.competitionRecord.quantity}</dt>
          <dd>{spread.quantity}</dd>
        </div>
        <div>
          <dt>{copy.competitionRecord.direction}</dt>
          <dd>{copy.competitionRecord[position.thesis.direction.toLowerCase() as "bullish" | "bearish" | "neutral"]}</dd>
        </div>
      </dl>
      <ol className="competition-timeline" aria-label={copy.competitionRecord.timeline}>
        {position.events.map((event, index) => (
          <li key={`${event.occurred_at}-${event.event_kind}-${event.action}-${index}`}>
            <span className="competition-timeline__rail" aria-hidden="true" />
            <div>
              <time dateTime={event.occurred_at}>{formatUtc(event.occurred_at)}</time>
              <strong>{eventLabel(event)}</strong>
              <p className={cashflowClass(event)}>{eventDetail(event)}</p>
            </div>
          </li>
        ))}
      </ol>
      <p className="competition-record-id">
        {copy.competitionRecord.recordId} <span>{record.public_record_id.slice(0, 12)}</span>
      </p>
    </article>
  );
}

export function CompetitionRecordView({ archive }: CompetitionRecordViewProps) {
  if (archive === undefined) {
    return <section className="competition-record-state" aria-live="polite"><h2>{copy.competitionRecord.loading}</h2></section>;
  }
  if (archive === null) {
    return (
      <section className="competition-record-state" aria-live="polite">
        <h2>{copy.competitionRecord.unavailable}</h2>
        <p>{copy.competitionRecord.unavailableDetail}</p>
      </section>
    );
  }
  if (archive.publication_status === "NOT_PUBLISHED") {
    return (
      <section className="competition-record-state" aria-live="polite">
        <h2>{copy.competitionRecord.notPublished}</h2>
        <p>{copy.competitionRecord.notPublishedDetail}</p>
      </section>
    );
  }
  return (
    <section className="competition-record-list" aria-label={copy.competitionRecord.timeline}>
      {archive.records.map((record) =>
        isPositionRecord(record) ? (
          <PositionRecordCard key={record.public_record_id} record={record} />
        ) : (
          <NoTradeRecord key={record.public_record_id} record={record} />
        ),
      )}
    </section>
  );
}
