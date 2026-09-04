import { describe, expect, it } from "vitest";
import { ExperimentPerformanceProjectionSchema } from "./experiment-performance-contracts";

const lineage = {
  experiment_id: "00000000-0000-4000-8000-000000000001",
  source_definition_hash: "1".repeat(64),
  protocol_hash: "2".repeat(64),
};

const unavailable = (unavailable_reason: "NO_OPENED_TRADES" | "NO_CLOSED_TRADES") => ({
  value: null,
  unavailable_reason,
});

describe("experiment performance projection contract", () => {
  it("keeps a no-trade decision unavailable instead of reporting zero performance", () => {
    const projection = ExperimentPerformanceProjectionSchema.parse({
      lineage,
      decision_count: 1,
      opened_trade_count: 0,
      closed_trade_count: 0,
      terminal_state: "NO_POSITION",
      total_defined_maximum_risk_at_entry: unavailable("NO_OPENED_TRADES"),
      entry_cash_flow: unavailable("NO_OPENED_TRADES"),
      management_cash_flow: unavailable("NO_OPENED_TRADES"),
      exit_cash_flow: unavailable("NO_CLOSED_TRADES"),
      realized_strategy_pnl: unavailable("NO_CLOSED_TRADES"),
      win_count: unavailable("NO_CLOSED_TRADES"),
      loss_count: unavailable("NO_CLOSED_TRADES"),
      breakeven_count: unavailable("NO_CLOSED_TRADES"),
    });

    expect(projection.realized_strategy_pnl).toEqual(unavailable("NO_CLOSED_TRADES"));
    expect(projection.total_defined_maximum_risk_at_entry).toEqual(
      unavailable("NO_OPENED_TRADES"),
    );
  });

  it("keeps an open trade's exit and outcome unavailable", () => {
    const projection = ExperimentPerformanceProjectionSchema.parse({
      lineage,
      decision_count: 2,
      opened_trade_count: 1,
      closed_trade_count: 0,
      terminal_state: "OPEN",
      total_defined_maximum_risk_at_entry: { value: "500", unavailable_reason: null },
      entry_cash_flow: { value: "-100", unavailable_reason: null },
      management_cash_flow: { value: "20", unavailable_reason: null },
      exit_cash_flow: unavailable("NO_CLOSED_TRADES"),
      realized_strategy_pnl: unavailable("NO_CLOSED_TRADES"),
      win_count: unavailable("NO_CLOSED_TRADES"),
      loss_count: unavailable("NO_CLOSED_TRADES"),
      breakeven_count: unavailable("NO_CLOSED_TRADES"),
    });

    expect(projection.exit_cash_flow.value).toBeNull();
    expect(projection.realized_strategy_pnl.unavailable_reason).toBe("NO_CLOSED_TRADES");
  });

  it("rejects a closed outcome that does not account for every closed trade", () => {
    const result = ExperimentPerformanceProjectionSchema.safeParse({
      lineage,
      decision_count: 3,
      opened_trade_count: 1,
      closed_trade_count: 1,
      terminal_state: "CLOSED",
      total_defined_maximum_risk_at_entry: { value: "500", unavailable_reason: null },
      entry_cash_flow: { value: "-100", unavailable_reason: null },
      management_cash_flow: { value: "20", unavailable_reason: null },
      exit_cash_flow: { value: "130", unavailable_reason: null },
      realized_strategy_pnl: { value: "50", unavailable_reason: null },
      win_count: { value: 0, unavailable_reason: null },
      loss_count: { value: 0, unavailable_reason: null },
      breakeven_count: { value: 0, unavailable_reason: null },
    });

    expect(result.success).toBe(false);
  });
});
