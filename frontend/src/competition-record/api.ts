import { z } from "zod";

const decimalSchema = z.string().regex(/^[+-]?\d{1,12}(?:\.\d{1,9})?$/);
const hashSchema = z.string().regex(/^[a-f0-9]{64}$/);
const timestampSchema = z.string()
  .regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/)
  .datetime({ offset: true });

const spreadSchema = z.object({
  structure: z.literal("VERTICAL"),
  underlying: z.string().regex(/^[A-Z]{1,6}$/),
  option_type: z.enum(["CALL", "PUT"]),
  expiration: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  long_strike: decimalSchema,
  short_strike: decimalSchema,
  quantity: z.number().int().positive(),
}).strict().superRefine((spread, context) => {
  if (Number(spread.long_strike) === Number(spread.short_strike)) {
    context.addIssue({ code: "custom", message: "Vertical strikes must be distinct" });
  }
});

const exposureSchema = z.object({
  delta: decimalSchema.nullable(),
  gamma: decimalSchema.nullable(),
  theta_per_day: decimalSchema.nullable(),
  vega_per_iv_point: decimalSchema.nullable(),
}).strict().superRefine((exposure, context) => {
  if (Object.values(exposure).every((value) => value === null)) {
    context.addIssue({ code: "custom", message: "Exposure requires a measured value" });
  }
});

const executionEventSchema = z.object({
  event_kind: z.literal("EXECUTION"),
  action: z.enum(["ENTRY", "ROLL", "CLOSE"]),
  occurred_at: timestampSchema,
  reason_category: z.enum(["POSITION_OPENED", "POSITION_ROLLED", "POSITION_CLOSED"]),
  cashflow_usd: decimalSchema,
  execution_status: z.literal("FILLED"),
  resulting_state: z.enum(["OPEN", "CLOSED"]),
  spread_after: spreadSchema.nullable(),
}).strict();

const assessmentEventSchema = z.object({
  event_kind: z.literal("ASSESSMENT"),
  action: z.enum(["HOLD", "CLOSE", "ROLL", "NO_ACTION"]),
  occurred_at: timestampSchema,
  reason_category: z.enum([
    "POSITION_REVIEWED",
    "RISK_REDUCTION",
    "THESIS_CHANGED",
    "POSITION_ADJUSTMENT",
    "DATA_INCOMPLETE",
  ]),
}).strict();

type AssessmentEvent = z.infer<typeof assessmentEventSchema>;
type AssessmentAction = AssessmentEvent["action"];
type AssessmentReason = AssessmentEvent["reason_category"];
type Spread = z.infer<typeof spreadSchema>;

const assessmentReasons: Record<AssessmentAction, readonly AssessmentReason[]> = {
  HOLD: ["POSITION_REVIEWED"],
  CLOSE: ["RISK_REDUCTION", "THESIS_CHANGED"],
  ROLL: ["POSITION_ADJUSTMENT"],
  NO_ACTION: ["DATA_INCOMPLETE"],
};

function sameSpread(left: Spread | null, right: Spread | null): boolean {
  if (left === null || right === null) return left === right;
  return left.structure === right.structure
    && left.underlying === right.underlying
    && left.option_type === right.option_type
    && left.expiration === right.expiration
    && left.long_strike === right.long_strike
    && left.short_strike === right.short_strike
    && left.quantity === right.quantity;
}

const noTradeProjectionSchema = z.object({
  schema_version: z.literal("v1"),
  record_kind: z.literal("NO_TRADE"),
  public_record_id: hashSchema,
  status: z.literal("NO_TRADE"),
  reason_category: z.literal("STRATEGY_NOT_READY"),
  decided_at: timestampSchema,
  observed_at: timestampSchema,
  paper_trading: z.literal(true),
}).strict().superRefine((projection, context) => {
  if (Date.parse(projection.observed_at) < Date.parse(projection.decided_at)) {
    context.addIssue({ code: "custom", message: "No-trade chronology is inconsistent" });
  }
});

const positionProjectionSchema = z.object({
  schema_version: z.literal("v1"),
  record_kind: z.literal("POSITION"),
  public_record_id: hashSchema,
  state: z.enum(["OPEN", "CLOSED"]),
  underlying: z.string().regex(/^[A-Z]{1,6}$/),
  opening_spread: spreadSchema,
  current_spread: spreadSchema.nullable(),
  opened_at: timestampSchema,
  as_of: timestampSchema,
  closed_at: timestampSchema.nullable(),
  thesis: z.object({
    direction: z.enum(["BULLISH", "BEARISH", "NEUTRAL"]),
    volatility_view: z.enum(["LONG", "SHORT", "NEUTRAL"]),
    target_at: timestampSchema,
  }).strict(),
  events: z.array(z.discriminatedUnion("event_kind", [executionEventSchema, assessmentEventSchema])).min(1),
  current_exposure: exposureSchema.nullable(),
  execution_status: z.literal("FILLED"),
  paper_trading: z.literal(true),
}).strict().superRefine((position, context) => {
  const issue = (message: string) => context.addIssue({ code: "custom", message });
  const eventTimes = position.events.map((event) => Date.parse(event.occurred_at));
  if (
    position.events[0]?.event_kind !== "EXECUTION"
    || position.events[0].action !== "ENTRY"
    || position.events[0].occurred_at !== position.opened_at
  ) issue("Position opening event is inconsistent");
  if (eventTimes.some((time, index) => index > 0 && time < eventTimes[index - 1])) {
    issue("Position events are not chronological");
  }
  if (
    Date.parse(position.as_of) < Date.parse(position.opened_at)
    || eventTimes.some((time) => time > Date.parse(position.as_of))
  ) issue("Position as-of time is inconsistent");

  let lifecycleState: "OPEN" | "CLOSED" | null = null;
  const executions: z.infer<typeof executionEventSchema>[] = [];
  for (const event of position.events) {
    if (event.event_kind === "ASSESSMENT") {
      if (
        lifecycleState !== "OPEN"
        || !assessmentReasons[event.action].includes(event.reason_category)
      ) {
        issue("Assessment event is inconsistent");
      }
      continue;
    }
    const expectedReason = {
      ENTRY: "POSITION_OPENED",
      ROLL: "POSITION_ROLLED",
      CLOSE: "POSITION_CLOSED",
    } as const;
    if (event.reason_category !== expectedReason[event.action]) issue("Execution reason is inconsistent");
    if (event.action === "ENTRY") {
      if (lifecycleState !== null) issue("Position contains more than one entry");
    } else if (lifecycleState !== "OPEN") issue("Execution sequence is inconsistent");
    if (event.action === "CLOSE") {
      if (event.resulting_state !== "CLOSED" || event.spread_after !== null) {
        issue("Close result is inconsistent");
      }
    } else if (event.resulting_state !== "OPEN" || event.spread_after === null) {
      issue("Entry or roll result is inconsistent");
    }
    lifecycleState = event.resulting_state;
    executions.push(event);
  }

  const latest = executions.at(-1);
  if (!latest || latest.resulting_state !== position.state) issue("Position state is inconsistent");
  if (latest && !sameSpread(latest.spread_after, position.current_spread)) {
    issue("Current spread is inconsistent");
  }
  if (executions[0] && !sameSpread(executions[0].spread_after, position.opening_spread)) {
    issue("Opening spread is inconsistent");
  }
  const spreads = [position.opening_spread, position.current_spread, ...executions.map((event) => event.spread_after)];
  if (spreads.some((spread) => spread !== null && spread.underlying !== position.underlying)) {
    issue("Spread underlying is inconsistent");
  }
  if (position.state === "OPEN") {
    if (position.current_spread === null || position.closed_at !== null) issue("Open position is inconsistent");
  } else if (
    position.current_spread !== null
    || position.closed_at === null
    || latest?.action !== "CLOSE"
    || latest.occurred_at !== position.closed_at
  ) issue("Closed position is inconsistent");
});

const recordSchema = z.object({
  schema_version: z.literal("v1"),
  kind: z.enum(["NO_TRADE", "POSITION"]),
  public_record_id: hashSchema,
  occurred_at: timestampSchema,
  published_at: timestampSchema,
  payload: z.union([noTradeProjectionSchema, positionProjectionSchema]),
  projection_hash: hashSchema,
  publication_hash: hashSchema,
  predecessor_hash: hashSchema.nullable(),
}).strict().superRefine((record, context) => {
  if (
    record.kind !== record.payload.record_kind
    || record.public_record_id !== record.payload.public_record_id
    || Date.parse(record.published_at) < Date.parse(record.occurred_at)
  ) {
    context.addIssue({ code: "custom", message: "Competition record identity does not match its projection" });
  }
});

export const competitionRecordResponseSchema = z.object({
  schema_version: z.literal("v1"),
  publication_status: z.enum(["PUBLISHED", "NOT_PUBLISHED"]),
  records: z.array(recordSchema),
}).strict().superRefine((response, context) => {
  if ((response.publication_status === "PUBLISHED") !== (response.records.length > 0)) {
    context.addIssue({ code: "custom", message: "Competition record publication state is inconsistent" });
  }
  response.records.forEach((record, index) => {
    const predecessor = index === 0 ? null : response.records[index - 1].publication_hash;
    if (record.predecessor_hash !== predecessor) {
      context.addIssue({ code: "custom", path: ["records", index, "predecessor_hash"], message: "Competition record chain is inconsistent" });
    }
    if (index > 0 && Date.parse(record.published_at) < Date.parse(response.records[index - 1].published_at)) {
      context.addIssue({ code: "custom", path: ["records", index, "published_at"], message: "Competition records are not chronological" });
    }
  });
});

export type CompetitionRecordResponse = z.infer<typeof competitionRecordResponseSchema>;
export type CompetitionRecord = CompetitionRecordResponse["records"][number];
export type PositionRecord = CompetitionRecord & { payload: z.infer<typeof positionProjectionSchema> };

export async function loadCompetitionRecord(): Promise<CompetitionRecordResponse> {
  const response = await fetch("/api/competition-record", {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "omit",
  });
  if (!response.ok) throw new Error("COMPETITION_RECORD_UNAVAILABLE");
  return competitionRecordResponseSchema.parse(await response.json());
}
