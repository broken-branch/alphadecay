export type StrategyDirection = "BULLISH" | "BEARISH" | "NEUTRAL" | "UNSURE";

export type StrategyHorizon = "INTRADAY" | "DAYS" | "WEEKS" | "MONTHS" | "UNSURE";

export type StrategyIntakeFields = {
  market: string;
  thesis: string;
  direction: StrategyDirection;
  horizon: StrategyHorizon | "";
  evidence: string;
  invalidation: string;
  maximumRiskUsd: string;
  notes: string;
};

export type StrategyBriefSource = {
  kind: "PASTED_TEXT" | "TEXT_FILE" | "MARKDOWN_FILE";
  content: string;
  filename?: string;
};

export type StrategyDraftRequest = {
  source: StrategyBriefSource;
  market_scope: string;
  direction: StrategyDirection;
  horizon: string;
  evidence: string[];
  invalidation: string[];
  risk_budget: {
    max_loss_dollars: string;
  };
  notes?: string;
};

export type StrategyIntakeErrors = Partial<Record<keyof StrategyIntakeFields, string>>;
