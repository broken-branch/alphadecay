import { useEffect, useMemo, useState } from "react";
import { copy as publicCopy } from "../content/copy";
import { ProtocolBuilder } from "../protocol-builder";
import type { ReviewedExecutableProtocolRequest } from "../protocol-builder";
import { compileRequestFromProtocol } from "./compiled-experiment-contracts";
import type { CompiledExperimentVersion } from "./compiled-experiment-contracts";
import type { ExperimentAuthorizationStatus } from "./experiment-authorization-contracts";
import {
  reviewedExperimentClient,
  ReviewedExperimentRequestError,
} from "./reviewed-registry-api";
import type { ReviewedExperimentClient } from "./reviewed-registry-api";
import type { ReviewedExperimentDefinition } from "./reviewed-registry-contracts";
import "./reviewed-registry.css";

const copy = publicCopy.experiment.registry;

export type ReviewedExperimentRegistryProps = {
  experiments: readonly ReviewedExperimentDefinition[] | null | undefined;
  csrfToken: string;
  client?: ReviewedExperimentClient;
  onSessionRejected?: () => void;
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

function Rule({ label, value }: { label: string; value: string | null }) {
  return <div><dt>{label}</dt><dd>{value ?? copy.notProvided}</dd></div>;
}

type CompiledState = "CHECKING" | "MISSING" | "READY" | "UNAVAILABLE";
type AuthorizationState = "IDLE" | "CHECKING" | "READY" | "UNAVAILABLE";

export function ReviewedExperimentRegistry({
  experiments,
  csrfToken,
  client = reviewedExperimentClient,
  onSessionRejected,
}: ReviewedExperimentRegistryProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [compiled, setCompiled] = useState<CompiledExperimentVersion | null>(null);
  const [compiledState, setCompiledState] = useState<CompiledState>("CHECKING");
  const [authorization, setAuthorization] = useState<ExperimentAuthorizationStatus | null>(null);
  const [authorizationState, setAuthorizationState] = useState<AuthorizationState>("IDLE");
  const [authorizationBusy, setAuthorizationBusy] = useState(false);
  const [authorizationError, setAuthorizationError] = useState<string | null>(null);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [compileBusy, setCompileBusy] = useState(false);
  const [compileError, setCompileError] = useState<string | null>(null);
  const [statusVersion, setStatusVersion] = useState(0);
  const orderedExperiments = useMemo(
    () => experiments?.slice().sort((left, right) => (
      right.created_at.localeCompare(left.created_at)
      || right.experiment_id.localeCompare(left.experiment_id)
    )),
    [experiments],
  );
  const selected = orderedExperiments?.find((item) => item.experiment_id === selectedId)
    ?? orderedExperiments?.[0]
    ?? null;

  useEffect(() => {
    if (!orderedExperiments?.length) {
      setSelectedId(null);
      return;
    }
    if (!orderedExperiments.some((item) => item.experiment_id === selectedId)) {
      setSelectedId(orderedExperiments[0].experiment_id);
    }
  }, [orderedExperiments, selectedId]);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    setCompiled(null);
    setCompiledState("CHECKING");
    setAuthorization(null);
    setAuthorizationState("IDLE");
    setAuthorizationBusy(false);
    setBuilderOpen(false);
    setCompileBusy(false);
    setCompileError(null);
    let compiledLoaded = false;
    const load = async () => {
      try {
        const result = await client.readCompiled(selected.experiment_id, csrfToken);
        if (!active) return;
        const exactResult = result?.source_definition_hash === selected.definition_hash
          ? result
          : null;
        setCompiled(exactResult);
        setCompiledState(result && !exactResult ? "UNAVAILABLE" : exactResult ? "READY" : "MISSING");
        if (!exactResult) return;
        compiledLoaded = true;
        setAuthorizationState("CHECKING");
        const status = await client.readAuthorization(
          exactResult.experiment_id,
          exactResult.source_definition_hash,
          exactResult.protocol_hash,
          csrfToken,
        );
        if (!active) return;
        setAuthorization(status);
        setAuthorizationState("READY");
      } catch (error) {
        if (!active) return;
        if (compiledLoaded) {
          setAuthorizationState("UNAVAILABLE");
        } else {
          setCompiledState("UNAVAILABLE");
        }
        if (
          error instanceof ReviewedExperimentRequestError
          && (error.status === 401 || error.status === 403)
        ) onSessionRejected?.();
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [client, csrfToken, onSessionRejected, selected, statusVersion]);

  const compileProtocol = async (protocol: ReviewedExecutableProtocolRequest) => {
    if (!selected || compileBusy) return;
    setCompileBusy(true);
    setCompileError(null);
    try {
      const result = await client.compile(
        selected.experiment_id,
        compileRequestFromProtocol(selected.definition_hash, protocol),
        csrfToken,
      );
      setCompiled(result);
      setCompiledState("READY");
      setBuilderOpen(false);
      setAuthorizationState("CHECKING");
      try {
        const status = await client.readAuthorization(
          result.experiment_id,
          result.source_definition_hash,
          result.protocol_hash,
          csrfToken,
        );
        setAuthorization(status);
        setAuthorizationState("READY");
      } catch (error) {
        setAuthorizationState("UNAVAILABLE");
        if (
          error instanceof ReviewedExperimentRequestError
          && (error.status === 401 || error.status === 403)
        ) onSessionRejected?.();
      }
    } catch (error) {
      if (error instanceof ReviewedExperimentRequestError && error.status === 409) {
        setCompileError(copy.compileConflict);
      } else {
        setCompileError(copy.compileFailed);
      }
      if (
        error instanceof ReviewedExperimentRequestError
        && (error.status === 401 || error.status === 403)
      ) onSessionRejected?.();
    } finally {
      setCompileBusy(false);
    }
  };

  const changeAuthorization = async (operation: "arm" | "disarm") => {
    if (!selected || !compiled || !authorization || authorizationBusy) return;
    setAuthorizationBusy(true);
    setAuthorizationError(null);
    try {
      const result = await client[operation](
        selected.experiment_id,
        {
          schema_version: "v1",
          source_definition_hash: compiled.source_definition_hash,
          protocol_hash: compiled.protocol_hash,
          expected_revision: authorization.authorization_revision,
        },
        csrfToken,
      );
      setAuthorization(result);
      setAuthorizationState("READY");
    } catch (error) {
      if (error instanceof ReviewedExperimentRequestError && error.status === 409) {
        setAuthorizationError(copy.authorization.stale);
        setStatusVersion((current) => current + 1);
      } else if (
        error instanceof ReviewedExperimentRequestError
        && (error.status === 401 || error.status === 403)
      ) {
        setAuthorizationError(copy.authorization.sessionRejected);
        onSessionRejected?.();
      } else {
        setAuthorizationError(copy.authorization.failed);
      }
    } finally {
      setAuthorizationBusy(false);
    }
  };

  return (
    <section className="reviewed-registry" aria-labelledby="reviewed-registry-title">
      <header className="reviewed-registry__header">
        <div>
          <p className="reviewed-registry__eyebrow">{copy.eyebrow}</p>
          <h1 id="reviewed-registry-title">{copy.title}</h1>
          <p>{copy.intro}</p>
        </div>
        <p className="reviewed-registry__boundary">{copy.boundary}</p>
      </header>

      {experiments === undefined ? <p className="reviewed-registry__empty">{copy.loading}</p> : null}
      {experiments === null ? <p className="reviewed-registry__empty" role="alert">{copy.unavailable}</p> : null}
      {experiments?.length === 0 ? <p className="reviewed-registry__empty">{copy.empty}</p> : null}

      {orderedExperiments?.length && selected ? (
        <div className="reviewed-registry__layout">
          <ol className="reviewed-registry__index" aria-label={copy.indexLabel}>
            {orderedExperiments.map((experiment) => (
              <li key={experiment.experiment_id}>
                <button
                  type="button"
                  disabled={authorizationBusy}
                  aria-current={experiment.experiment_id === selected.experiment_id ? "true" : undefined}
                  onClick={() => {
                    setAuthorizationError(null);
                    setSelectedId(experiment.experiment_id);
                  }}
                >
                  <time dateTime={experiment.created_at}>{formatDate(experiment.created_at)}</time>
                  <strong>{experiment.original_thesis.market_scope ?? copy.untitled}</strong>
                  <span>{copy.reviewed}</span>
                </button>
              </li>
            ))}
          </ol>

          <article className="reviewed-registry__detail" aria-live="polite">
            <header>
              <p className="reviewed-registry__eyebrow">{copy.savedSource}</p>
              <h2>{selected.original_thesis.market_scope ?? copy.untitled}</h2>
              <p>{selected.original_thesis.source.content}</p>
            </header>

            <div className="reviewed-registry__state" aria-label={copy.stateLabel} aria-live="polite">
              <strong>{compiledState === "READY" ? copy.compiled : copy.reviewed}</strong>
              {compiledState === "CHECKING" ? <span>{copy.checkingCompiled}</span> : null}
              {compiledState === "MISSING" ? <span>{copy.setupIncomplete}</span> : null}
              {compiledState === "READY" && authorizationState === "CHECKING"
                ? <span>{copy.authorization.checking}</span>
                : null}
              {authorizationState === "READY" && authorization
                ? <span>{copy.authorization.state[authorization.authorization_state]}</span>
                : null}
              {authorizationState === "UNAVAILABLE"
                ? <span>{copy.authorization.unavailable}</span>
                : null}
              {compiledState === "UNAVAILABLE" ? <span>{copy.compileUnavailable}</span> : null}
              <span>{copy.authorization.notConnected}</span>
              {compiledState !== "READY" ? <span>{copy.automationOff}</span> : null}
              {compiledState !== "READY" ? <span>{copy.noOrderAccess}</span> : null}
            </div>

            {compiledState === "MISSING" && !builderOpen ? (
              <section className="reviewed-registry__setup" aria-labelledby="reviewed-setup-title">
                <div>
                  <h3 id="reviewed-setup-title">{copy.setupIncomplete}</h3>
                  <p>{copy.setupIntro}</p>
                </div>
                <button type="button" onClick={() => setBuilderOpen(true)}>{copy.setupAction}</button>
              </section>
            ) : null}

            {compiledState === "UNAVAILABLE" ? (
              <section className="reviewed-registry__setup" role="alert">
                <p>{copy.compileUnavailable}</p>
                <button type="button" onClick={() => setStatusVersion((current) => current + 1)}>
                  {copy.checkAgain}
                </button>
              </section>
            ) : null}

            {compiledState === "READY" && compiled ? (
              <>
                <section className="reviewed-registry__compiled" aria-labelledby="reviewed-compiled-title">
                  <h3 id="reviewed-compiled-title">{copy.compiled}</h3>
                  <p>{copy.compiledIntro}</p>
                </section>

                <section className="reviewed-registry__authorization" aria-labelledby="reviewed-authorization-title">
                  <div className="reviewed-registry__authorization-heading">
                    <div>
                      <h3 id="reviewed-authorization-title">{copy.authorization.title}</h3>
                      <p>{copy.authorization.intro}</p>
                    </div>
                    {authorizationState === "READY" && authorization ? (
                      <button
                        type="button"
                        disabled={authorizationBusy}
                        onClick={() => void changeAuthorization(
                          authorization.authorization_state === "ARMED" ? "disarm" : "arm",
                        )}
                      >
                        {authorizationBusy
                          ? copy.authorization.changing
                          : authorization.authorization_state === "ARMED"
                            ? copy.authorization.disarmAction
                            : copy.authorization.armAction}
                      </button>
                    ) : null}
                  </div>

                  <dl className="reviewed-registry__authorization-status">
                    <div>
                      <dt>{copy.authorization.stateLabel}</dt>
                      <dd>{authorization
                        ? copy.authorization.state[authorization.authorization_state]
                        : authorizationState === "UNAVAILABLE"
                          ? copy.authorization.unavailable
                          : copy.authorization.checking}</dd>
                    </div>
                    <div>
                      <dt>{copy.authorization.revisionLabel}</dt>
                      <dd>{authorization?.authorization_revision ?? copy.authorization.notAvailable}</dd>
                    </div>
                    <div>
                      <dt>{copy.authorization.runtimeLabel}</dt>
                      <dd>{copy.authorization.notConnected}</dd>
                    </div>
                    <div>
                      <dt>{copy.authorization.executionLabel}</dt>
                      <dd>{copy.authorization.executionUnavailable}</dd>
                    </div>
                  </dl>

                  <details className="reviewed-registry__lineage-details">
                    <summary>{copy.authorization.lineageSummary}</summary>
                    <dl className="reviewed-registry__lineage">
                      <div>
                        <dt>{copy.authorization.sourceHash}</dt>
                        <dd><code>{compiled.source_definition_hash}</code></dd>
                      </div>
                      <div>
                        <dt>{copy.authorization.protocolHash}</dt>
                        <dd><code>{compiled.protocol_hash}</code></dd>
                      </div>
                    </dl>
                  </details>

                  {authorization ? (
                    authorization.authorization_state === "ARMED"
                      ? <p>{copy.authorization.armedBoundary}</p>
                      : <p>{copy.authorization.disarmedBoundary}</p>
                  ) : null}
                  {authorization?.authorization_state === "DISARMED"
                    ? <p>{copy.authorization.managementBoundary}</p>
                    : null}
                  {authorizationState === "UNAVAILABLE" ? (
                    <p role="alert">{copy.authorization.unavailable}</p>
                  ) : null}
                  {authorizationError ? <p role="alert">{authorizationError}</p> : null}
                </section>
              </>
            ) : null}

            <section aria-labelledby="reviewed-rules-title">
              <h3 id="reviewed-rules-title">{copy.rulesTitle}</h3>
              <dl className="reviewed-registry__rules">
                <Rule label={copy.entry} value={selected.reviewed_protocol.entry_rule} />
                <Rule label={copy.noTrade} value={selected.reviewed_protocol.no_trade_rule} />
                <Rule label={copy.profit} value={selected.reviewed_protocol.profit_exit_rule} />
                <Rule label={copy.loss} value={selected.reviewed_protocol.loss_exit_rule} />
                <Rule label={copy.time} value={selected.reviewed_protocol.time_exit_rule} />
              </dl>
            </section>

            <section aria-labelledby="reviewed-invalidation-title">
              <h3 id="reviewed-invalidation-title">{copy.invalidation}</h3>
              {selected.reviewed_protocol.invalidation_rules.length ? (
                <ul>{selected.reviewed_protocol.invalidation_rules.map((rule, index) => (
                  <li key={`${index}-${rule}`}>{rule}</li>
                ))}</ul>
              ) : <p>{copy.notProvided}</p>}
            </section>

            {builderOpen && compiledState === "MISSING" ? (
              <section className="reviewed-registry__builder" aria-label={copy.setupAction}>
                {compileError ? <p className="reviewed-registry__compile-error" role="alert">{compileError}</p> : null}
                <ProtocolBuilder
                  key={selected.experiment_id}
                  curation={selected.curation}
                  onComplete={(protocol) => void compileProtocol(protocol)}
                  submitting={compileBusy}
                  submitLabel={copy.compileAction}
                  submittingLabel={copy.compiling}
                  boundaryText={copy.compileBoundary}
                />
              </section>
            ) : null}
          </article>
        </div>
      ) : null}
    </section>
  );
}
