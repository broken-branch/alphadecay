import type {
  StrategyDirection,
  StrategyHorizon,
  StrategyIntakeFields,
} from "./types";

const fieldAliases = {
  market: ["symbol", "symbol or market", "ticker", "market", "universe"],
  thesis: ["thesis", "idea", "claim", "what i expect"],
  direction: ["direction", "expected direction"],
  horizon: ["horizon", "time window", "timeframe"],
  evidence: ["evidence", "support", "what would support it"],
  invalidation: ["invalidation", "what would prove it wrong", "stop condition"],
  maximumRiskUsd: ["maximum risk", "max risk", "maximum loss", "risk"],
  notes: ["notes", "context", "other context"],
} as const;

type ImportKey = keyof typeof fieldAliases;

const aliasToKey = new Map<string, ImportKey>(
  Object.entries(fieldAliases).flatMap(([key, aliases]) =>
    aliases.map((alias) => [alias, key as ImportKey]),
  ),
);

function normalizedLabel(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/^#{1,6}\s*/, "")
    .replace(/[.:]+$/, "")
    .replace(/\s+/g, " ");
}

function directionFromText(value: string): StrategyDirection {
  const normalized = value.trim().toLowerCase();
  if (/\b(bullish|up|higher|rise|rising|long)\b/.test(normalized)) return "BULLISH";
  if (/\b(bearish|down|lower|fall|falling|short)\b/.test(normalized)) return "BEARISH";
  if (/\b(neutral|range|range-bound|sideways)\b/.test(normalized)) return "NEUTRAL";
  return "UNSURE";
}

function horizonFromText(value: string): StrategyHorizon | "" {
  const normalized = value.trim().toLowerCase();
  if (/\b(intraday|one day|same day|day trade)\b/.test(normalized)) return "INTRADAY";
  if (/\b(days|few days|several days|daily)\b/.test(normalized)) return "DAYS";
  if (/\b(weeks|few weeks|weekly)\b/.test(normalized)) return "WEEKS";
  if (/\b(months|few months|monthly)\b/.test(normalized)) return "MONTHS";
  if (/\b(unsure|not sure|unknown)\b/.test(normalized)) return "UNSURE";
  return "";
}

function riskFromText(value: string): string {
  const match = value.replaceAll(",", "").match(/(?:\$|usd\s*)?(\d+(?:\.\d{1,2})?)/i);
  return match?.[1] ?? "";
}

export function emptyStrategyIntake(): StrategyIntakeFields {
  return {
    market: "",
    thesis: "",
    direction: "UNSURE",
    horizon: "",
    evidence: "",
    invalidation: "",
    maximumRiskUsd: "",
    notes: "",
  };
}

export function parseStrategyText(source: string): Partial<StrategyIntakeFields> {
  const text = source.replace(/\r\n?/g, "\n").trim();
  if (!text) return {};

  const sections = new Map<ImportKey, string[]>();
  let activeKey: ImportKey | null = null;
  let foundLabel = false;

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    const inlineMatch = line.match(/^(#{1,6}\s*)?([^:]{1,40}):\s*(.*)$/);
    const headingKey = aliasToKey.get(normalizedLabel(line));
    const inlineKey = inlineMatch
      ? aliasToKey.get(normalizedLabel(inlineMatch[2]))
      : undefined;
    if (inlineKey) {
      activeKey = inlineKey;
      foundLabel = true;
      const existing = sections.get(activeKey) ?? [];
      sections.set(activeKey, inlineMatch?.[3] ? [...existing, inlineMatch[3].trim()] : existing);
      continue;
    }
    if (headingKey) {
      activeKey = headingKey;
      foundLabel = true;
      if (!sections.has(activeKey)) sections.set(activeKey, []);
      continue;
    }
    if (activeKey && line) sections.get(activeKey)?.push(line);
  }

  if (!foundLabel) return { thesis: text };

  const value = (key: ImportKey) => sections.get(key)?.join("\n").trim() ?? "";
  const direction = value("direction");
  const horizon = value("horizon");
  const risk = value("maximumRiskUsd");
  return {
    market: value("market"),
    thesis: value("thesis"),
    direction: direction ? directionFromText(direction) : undefined,
    horizon: horizon ? horizonFromText(horizon) : undefined,
    evidence: value("evidence"),
    invalidation: value("invalidation"),
    maximumRiskUsd: risk ? riskFromText(risk) : undefined,
    notes: value("notes"),
  };
}

export function mergeImportedStrategy(
  current: StrategyIntakeFields,
  imported: Partial<StrategyIntakeFields>,
): StrategyIntakeFields {
  const next = { ...current };
  for (const key of Object.keys(imported) as (keyof StrategyIntakeFields)[]) {
    const value = imported[key];
    if (value !== undefined && value !== "") {
      Object.assign(next, { [key]: value });
    }
  }
  return next;
}
