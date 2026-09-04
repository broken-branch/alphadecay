import {
  buildReviewedExecutableProtocolRequest,
} from "../protocol-builder";
import { completeProtocolDraft, readyCurationFixture } from "../protocol-builder/test-fixture";
import {
  compileRequestFromProtocol,
} from "./compiled-experiment-contracts";
import type {
  CompiledExperimentVersion,
  CompileExperimentRequest,
} from "./compiled-experiment-contracts";
import type { ExperimentAuthorizationStatus } from "./experiment-authorization-contracts";

const sourceHash = "a".repeat(64);
const protocolHash = "b".repeat(64);
const ruleHash = "c".repeat(64);

export function compileRequestFixture(): CompileExperimentRequest {
  const built = buildReviewedExecutableProtocolRequest(
    readyCurationFixture(),
    completeProtocolDraft(),
  );
  if (!built.ok) throw new Error("Expected valid protocol fixture");
  return compileRequestFromProtocol(sourceHash, built.value);
}

export function compiledExperimentFixture(): CompiledExperimentVersion {
  const curation = readyCurationFixture();
  const request = compileRequestFixture();
  const compiledRule = (rule: CompileExperimentRequest["rules"]["entry_rule"]) => ({
    source_rule_hash: ruleHash,
    match: rule.match,
    predicates: rule.predicates,
  });
  return {
    schema_version: "v1",
    experiment_id: "10000000-0000-4000-8000-000000000001",
    source_version: 1,
    compiled_version: 1,
    source_definition_hash: sourceHash,
    lifecycle_state: "COMPILED",
    arm_state: "NOT_ARMED",
    automation_state: "OFF",
    execution_eligible: false,
    paper_trading_only: true,
    protocol_hash: protocolHash,
    compiled_protocol: {
      review_state: "REVIEWED",
      compile_status: "COMPILABLE",
      arm_state: "NOT_ARMED",
      automation_state: "OFF",
      execution_eligible: false,
      paper_trading_only: true,
      options_required: true,
      defined_risk_required: true,
      recipe: "TWO_LEG_DEBIT_VERTICAL",
      leg_count: 2,
      net_premium: "DEBIT",
      symbol: curation.intake.market_scope ?? "SPY",
      direction: "BULLISH",
      structure: "BULL_CALL_DEBIT_SPREAD",
      risk_budget: curation.intake.risk_budget!,
      definition: request.definition,
      rules: {
        entry_rule: compiledRule(request.rules.entry_rule),
        no_trade_rule: compiledRule(request.rules.no_trade_rule),
        profit_exit_rule: compiledRule(request.rules.profit_exit_rule),
        loss_exit_rule: compiledRule(request.rules.loss_exit_rule),
        time_exit_rule: compiledRule(request.rules.time_exit_rule),
        invalidation_rules: request.rules.invalidation_rules.map(compiledRule),
      },
      mandatory_safety_facts: ["PAPER_ACCOUNT_CONFIRMED", "RISK_WITHIN_REVIEWED_BUDGET"],
      source_hash: "d".repeat(64),
      compiler_hash: "e".repeat(64),
      definition_hash: "f".repeat(64),
      protocol_hash: protocolHash,
    },
    created_at: "2026-09-01T21:00:00Z",
  };
}

export function experimentAuthorizationFixture(
  state: ExperimentAuthorizationStatus["authorization_state"] = "NOT_ARMED",
  revision = state === "NOT_ARMED" ? 0 : 1,
): ExperimentAuthorizationStatus {
  return {
    schema_version: "v1",
    experiment_id: "10000000-0000-4000-8000-000000000001",
    source_definition_hash: sourceHash,
    protocol_hash: protocolHash,
    authorization_revision: revision,
    authorization_state: state,
    entry_authorized: state === "ARMED",
    existing_position_risk_management_preserved: true,
    runtime_state: "NOT_CONNECTED",
    execution_eligible: false,
    paper_trading_only: true,
    authorization_event_hash: revision === 0 ? null : "f".repeat(64),
    updated_at: revision === 0 ? null : "2026-09-01T21:05:00Z",
  };
}
