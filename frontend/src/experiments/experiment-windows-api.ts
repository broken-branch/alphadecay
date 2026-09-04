import { z } from "zod";

const timestamp = z.string()
  .regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/)
  .datetime({ offset: true });
const money = z.string().regex(/^-?(?:0|[1-9]\d{0,11})(?:\.\d{1,6})?$/);

const intervalSchema = z.object({
  schema_version: z.enum(["v1", "v2"]),
  opens_at: timestamp,
  closes_at: timestamp,
}).strict().superRefine((interval, context) => {
  if (Date.parse(interval.closes_at) < Date.parse(interval.opens_at)) {
    context.addIssue({ code: "custom", message: "Experiment entry window is reversed" });
  }
});

const decisionSchema = z.object({
  schema_version: z.enum(["v1", "v2"]),
  outcome_code: z.enum(["ENTRY_APPROVED", "NO_TRADE", "PROVIDER_FAILURE_NO_TRADE"]),
  reason: z.string().min(1).max(240),
  decided_at: timestamp,
}).strict();

const lifecycleSchema = z.object({
  schema_version: z.enum(["v1", "v2"]),
  status: z.enum(["OPEN", "CLOSED"]),
  opened_at: timestamp,
  closed_at: timestamp.nullable(),
  exit_reason: z.string().min(1).max(240).nullable(),
  realized_paper_pnl: money.nullable(),
}).strict().superRefine((lifecycle, context) => {
  const closed = lifecycle.status === "CLOSED";
  if (closed !== (lifecycle.closed_at !== null) || closed !== (lifecycle.exit_reason !== null)) {
    context.addIssue({ code: "custom", message: "Experiment lifecycle state is inconsistent" });
  }
  if (!closed && lifecycle.realized_paper_pnl !== null) {
    context.addIssue({ code: "custom", message: "Open experiment has realized P&L" });
  }
});

const windowSchema = z.object({
  schema_version: z.enum(["v1", "v2"]),
  plan_version: z.number().int().positive(),
  protocol: z.object({
    schema_version: z.enum(["v1", "v2"]),
    name: z.string().min(1).max(120),
    summary: z.string().min(1).max(320),
  }).strict(),
  frozen_at: timestamp,
  decision_boundary: timestamp,
  entry_window: intervalSchema,
  terminal_decision: decisionSchema.nullable(),
  lifecycle: lifecycleSchema.nullable(),
  status: z.enum(["PENDING", "OPEN", "DECIDED", "ABORTED"]).default("DECIDED"),
  aborted_reason: z.string().min(1).max(240).nullable().default(null),
  tick_outcome_code: z.string().min(1).max(64).nullable().default(null),
  tick_outcome_text: z.string().min(1).max(240).nullable().default(null),
  collapsed_versions: z.array(z.number().int().positive()).default([]),
}).strict().superRefine((window, context) => {
  if (
    Date.parse(window.frozen_at) > Date.parse(window.decision_boundary)
    || window.decision_boundary !== window.entry_window.opens_at
  ) {
    context.addIssue({ code: "custom", message: "Experiment window chronology is inconsistent" });
  }
  if (
    window.lifecycle !== null
    && window.terminal_decision?.outcome_code !== "ENTRY_APPROVED"
  ) {
    context.addIssue({ code: "custom", message: "Experiment lifecycle lacks an approved entry" });
  }
  if ((window.status === "ABORTED") !== (window.aborted_reason !== null)) {
    context.addIssue({ code: "custom", message: "Experiment abort reason is inconsistent" });
  }
  if ((window.tick_outcome_code === null) !== (window.tick_outcome_text === null)) {
    context.addIssue({ code: "custom", message: "Experiment tick outcome is inconsistent" });
  }
});

export const experimentWindowListSchema = z.object({
  schema_version: z.enum(["v1", "v2"]),
  windows: z.array(windowSchema),
}).strict().superRefine((response, context) => {
  const times = response.windows.map((window) => Date.parse(window.frozen_at));
  if (times.some((time, index) => index > 0 && time > times[index - 1])) {
    context.addIssue({ code: "custom", message: "Experiment windows are not newest first" });
  }
});

export type ExperimentWindowList = z.infer<typeof experimentWindowListSchema>;
export type ExperimentWindowRecord = ExperimentWindowList["windows"][number];

export async function loadExperimentWindows(
  fetcher: typeof fetch = fetch,
): Promise<ExperimentWindowList> {
  const response = await fetcher("/api/experiments/windows", {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
    headers: { Accept: "application/json", "Cache-Control": "no-store" },
  });
  if (!response.ok) throw new Error("EXPERIMENT_WINDOWS_UNAVAILABLE");
  return experimentWindowListSchema.parse(await response.json());
}
