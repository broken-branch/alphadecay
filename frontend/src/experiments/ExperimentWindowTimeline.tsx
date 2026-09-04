import { copy as publicCopy } from "../content/copy";
import type { ExperimentWindowList, ExperimentWindowRecord } from "./experiment-windows-api";
import "./experiment-window-timeline.css";

const copy = publicCopy.experiment.windows;

export type ExperimentWindowTimelineProps = {
  windows: ExperimentWindowList | null | undefined;
};

function formatTime(value: string): string {
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

function formatMoney(value: string): string {
  const amount = Number(value);
  const absolute = Math.abs(amount).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${amount > 0 ? "+" : amount < 0 ? "−" : ""}$${absolute}`;
}

function decisionLabel(window: ExperimentWindowRecord): string {
  if (window.status === "ABORTED") return copy.aborted;
  if (window.status === "OPEN") return copy.openWindow;
  const outcome = window.terminal_decision?.outcome_code;
  if (!outcome) return copy.pendingDecision;
  return {
    ENTRY_APPROVED: copy.entryApproved,
    NO_TRADE: copy.noTrade,
    PROVIDER_FAILURE_NO_TRADE: copy.providerFailure,
  }[outcome];
}

function versionLabel(window: ExperimentWindowRecord): string {
  const versions = window.collapsed_versions;
  if (versions.length < 2) return `${copy.window} ${window.plan_version}`;
  return `${copy.versions} ${versions[0]}–${versions[versions.length - 1]}`;
}

function outcome(window: ExperimentWindowRecord): string {
  if (!window.terminal_decision) return copy.pendingOutcome;
  if (window.terminal_decision.outcome_code !== "ENTRY_APPROVED") return copy.noPosition;
  if (!window.lifecycle) return copy.approvedNoPosition;
  if (window.lifecycle.status === "OPEN") return copy.open;
  if (window.lifecycle.realized_paper_pnl === null) return copy.closedUncertified;
  return `${copy.closed} · ${formatMoney(window.lifecycle.realized_paper_pnl)}`;
}

function WindowCard({ window }: { window: ExperimentWindowRecord }) {
  return (
    <li className="experiment-window">
      <article>
        <header>
          <div>
            <p className="experiment-window__eyebrow">
              {versionLabel(window)}
            </p>
            <h3>{window.protocol.name}</h3>
          </div>
          <strong>{decisionLabel(window)}</strong>
        </header>
        <p className="experiment-window__summary">{window.protocol.summary}</p>
        <dl>
          <div>
            <dt>{copy.frozen}</dt>
            <dd><time dateTime={window.frozen_at}>{formatTime(window.frozen_at)}</time></dd>
          </div>
          <div>
            <dt>{copy.decisionBoundary}</dt>
            <dd><time dateTime={window.decision_boundary}>{formatTime(window.decision_boundary)}</time></dd>
          </div>
          <div>
            <dt>{copy.entryWindow}</dt>
            <dd>
              <time dateTime={window.entry_window.opens_at}>{formatTime(window.entry_window.opens_at)}</time>
              <span aria-hidden="true"> {copy.timeRangeSeparator} </span>
              <time dateTime={window.entry_window.closes_at}>{formatTime(window.entry_window.closes_at)}</time>
            </dd>
          </div>
          <div>
            <dt>{copy.outcome}</dt>
            <dd>{outcome(window)}</dd>
          </div>
          {window.tick_outcome_text ? (
            <div>
              <dt>{copy.executionOutcome}</dt>
              <dd>{window.tick_outcome_text}</dd>
            </div>
          ) : null}
        </dl>
        <section aria-label={copy.decision}>
          <h4>{copy.decision}</h4>
          <p>{window.aborted_reason ? copy.abortedReason : window.terminal_decision?.reason ?? copy.pendingReason}</p>
          {window.lifecycle?.exit_reason ? <p>{window.lifecycle.exit_reason}</p> : null}
          {window.lifecycle?.status === "OPEN" ? <p>{copy.openDetail}</p> : null}
          {window.collapsed_versions.length > 1 ? (
            <details>
              <summary>{copy.versions}</summary>
              <p>{window.collapsed_versions.join(", ")}</p>
            </details>
          ) : null}
        </section>
      </article>
    </li>
  );
}

export function ExperimentWindowTimeline({ windows }: ExperimentWindowTimelineProps) {
  const protocolGroups = windows === null || windows === undefined
    ? []
    : Object.values(windows.windows.reduce<Record<string, ExperimentWindowRecord[]>>(
      (groups, window) => ({
        ...groups,
        [window.protocol.name]: [...(groups[window.protocol.name] ?? []), window],
      }),
      {},
    ));
  return (
    <section className="experiment-windows" aria-labelledby="experiment-windows-title">
      <header>
        <p className="experiment-windows__eyebrow">{copy.eyebrow}</p>
        <h2 id="experiment-windows-title">{copy.title}</h2>
        <p>{copy.intro}</p>
      </header>
      {windows === undefined ? (
        <p className="experiment-windows__empty">{copy.loading}</p>
      ) : windows === null ? (
        <p className="experiment-windows__empty">{copy.unavailable}</p>
      ) : windows.windows.length === 0 ? (
        <p className="experiment-windows__empty">{copy.empty}</p>
      ) : (
        <div className="experiment-window-groups">
          {protocolGroups.map((group) => {
            const first = group[0];
            if (!first) return null;
            return (
              <section key={first.protocol.name} aria-label={`${first.protocol.name} ${copy.protocolWindows}`}>
                <h3>{first.protocol.name}</h3>
                <ol aria-label={copy.timelineLabel}>
                  {group.map((window) => (
                    <WindowCard
                      key={`${window.frozen_at}:${window.plan_version}:${window.protocol.name}`}
                      window={window}
                    />
                  ))}
                </ol>
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}
