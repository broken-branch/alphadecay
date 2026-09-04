import { z } from "zod";

const hash = z.string().regex(/^[0-9a-f]{64}$/);
const decimal = z.string().regex(/^-?(?:0|[1-9]\d{0,11})(?:\.\d{1,9})?$/);

export const ExperimentMetricUnavailableReasonSchema = z.enum([
  "NO_OPENED_TRADES",
  "NO_CLOSED_TRADES",
]);

const unavailableMetric = z.object({
  value: z.null(),
  unavailable_reason: ExperimentMetricUnavailableReasonSchema,
}).strict();

const moneyMetric = z.union([
  z.object({ value: decimal, unavailable_reason: z.null() }).strict(),
  unavailableMetric,
]);

const countMetric = z.union([
  z.object({ value: z.number().int().nonnegative(), unavailable_reason: z.null() }).strict(),
  unavailableMetric,
]);

export const ExperimentPerformanceProjectionSchema = z.object({
  lineage: z.object({
    experiment_id: z.string().uuid(),
    source_definition_hash: hash,
    protocol_hash: hash,
  }).strict(),
  decision_count: z.number().int().nonnegative(),
  opened_trade_count: z.number().int().nonnegative(),
  closed_trade_count: z.number().int().nonnegative(),
  terminal_state: z.enum(["NO_POSITION", "OPEN", "CLOSED"]),
  total_defined_maximum_risk_at_entry: moneyMetric,
  entry_cash_flow: moneyMetric,
  management_cash_flow: moneyMetric,
  exit_cash_flow: moneyMetric,
  realized_strategy_pnl: moneyMetric,
  win_count: countMetric,
  loss_count: countMetric,
  breakeven_count: countMetric,
}).strict().superRefine((projection, context) => {
  const issue = (path: string, message: string) => context.addIssue({
    code: "custom",
    message,
    path: [path],
  });
  const openedMetrics = [
    "total_defined_maximum_risk_at_entry",
    "entry_cash_flow",
    "management_cash_flow",
  ] as const;
  const closedMetrics = [
    "exit_cash_flow",
    "realized_strategy_pnl",
    "win_count",
    "loss_count",
    "breakeven_count",
  ] as const;

  if (
    projection.closed_trade_count > projection.opened_trade_count
    || projection.decision_count < projection.opened_trade_count
  ) {
    issue("closed_trade_count", "Experiment performance counts are inconsistent");
  }

  const expectedTerminalState = projection.opened_trade_count === 0
    ? "NO_POSITION"
    : projection.closed_trade_count === projection.opened_trade_count
      ? "CLOSED"
      : "OPEN";
  if (projection.terminal_state !== expectedTerminalState) {
    issue("terminal_state", "Experiment performance terminal state is inconsistent");
  }

  for (const field of openedMetrics) {
    const metric = projection[field];
    if (projection.opened_trade_count === 0) {
      if (metric.value !== null || metric.unavailable_reason !== "NO_OPENED_TRADES") {
        issue(field, "Opened-trade metric availability is inconsistent");
      }
    } else if (metric.value === null || metric.unavailable_reason !== null) {
      issue(field, "Opened-trade metric must be available");
    }
  }

  for (const field of closedMetrics) {
    const metric = projection[field];
    if (projection.closed_trade_count === 0) {
      if (metric.value !== null || metric.unavailable_reason !== "NO_CLOSED_TRADES") {
        issue(field, "Closed-trade metric availability is inconsistent");
      }
    } else if (metric.value === null || metric.unavailable_reason !== null) {
      issue(field, "Closed-trade metric must be available");
    }
  }

  const outcomes = [projection.win_count, projection.loss_count, projection.breakeven_count];
  if (
    projection.closed_trade_count > 0
    && outcomes.every((metric) => metric.value !== null)
    && outcomes.reduce((total, metric) => total + (metric.value ?? 0), 0)
      !== projection.closed_trade_count
  ) {
    issue("win_count", "Closed-trade outcomes do not match the closed trade count");
  }
});

export type ExperimentMetricUnavailableReason = z.infer<
  typeof ExperimentMetricUnavailableReasonSchema
>;
export type ExperimentPerformanceProjection = z.infer<
  typeof ExperimentPerformanceProjectionSchema
>;
