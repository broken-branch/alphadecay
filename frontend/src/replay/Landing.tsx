import type { CompetitionPerformanceProofResponse } from "../contracts/v1";
import type { CompetitionRecordResponse } from "../competition-record/api";
import { copy } from "../content/copy";
import type { ExperimentWindowList, ExperimentWindowRecord } from "../experiments";

const landingCopy = copy.productShell.landing;

type LandingProps = {
  archive: CompetitionRecordResponse | null | undefined;
  proof: CompetitionPerformanceProofResponse | null | undefined;
  windows: ExperimentWindowList | null | undefined;
  onOpenReplay: () => void;
};

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
    year: "numeric",
  }).format(new Date(value));
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  }).format(new Date(value));
}

function decisionLabel(window: ExperimentWindowRecord): string {
  if (window.status === "ABORTED") return landingCopy.recordAborted;
  if (window.status === "PENDING" || window.status === "OPEN") return landingCopy.recordPending;
  const outcome = window.terminal_decision?.outcome_code;
  if (outcome === "ENTRY_APPROVED") return landingCopy.recordEntryApproved;
  if (outcome === "PROVIDER_FAILURE_NO_TRADE") return landingCopy.recordProviderFailure;
  return landingCopy.recordNoTrade;
}

function decisionDetail(window: ExperimentWindowRecord): string {
  if (window.status === "ABORTED") return window.aborted_reason ?? landingCopy.abortedReason;
  if (window.status === "PENDING" || window.status === "OPEN") return landingCopy.recordPendingDetail;
  return window.terminal_decision?.reason ?? landingCopy.recordPendingDetail;
}

function recordState(
  archive: CompetitionRecordResponse | null | undefined,
  windows: ExperimentWindowList | null | undefined,
): "loading" | "unavailable" | "empty" | "ready" {
  if (archive === null || windows === null) return "unavailable";
  if (archive === undefined || windows === undefined) return "loading";
  return windows.windows.length > 0 ? "ready" : "empty";
}

function BrokerWrites({ proof }: { proof: CompetitionPerformanceProofResponse | null | undefined }) {
  const count = proof?.point?.broker_write_count;
  if (count === undefined) return null;
  return (
    <div>
      <dt>{landingCopy.brokerWrites}</dt>
      <dd>{count}</dd>
    </div>
  );
}

function CompetitionCard({
  archive,
  proof,
  windows,
}: Pick<LandingProps, "archive" | "proof" | "windows">) {
  const state = recordState(archive, windows);
  if (state === "loading") {
    return (
      <section className="landing-record landing-record--state" aria-label={landingCopy.eyebrow}>
        <h2>{landingCopy.recordLoading}</h2>
      </section>
    );
  }
  if (state === "unavailable") {
    return (
      <section className="landing-record landing-record--state" aria-label={landingCopy.eyebrow}>
        <h2>{landingCopy.recordUnavailable}</h2>
        <p>{landingCopy.recordUnavailableDetail}</p>
      </section>
    );
  }
  const window = windows?.windows[0];
  if (!window) {
    return (
      <section className="landing-record landing-record--state" aria-label={landingCopy.eyebrow}>
        <h2>{landingCopy.recordPending}</h2>
        <p>{landingCopy.recordEmpty}</p>
      </section>
    );
  }
  const pending = window.status === "PENDING" || window.status === "OPEN";
  return (
    <section className="landing-record" id="competition-record" aria-labelledby="landing-record-title">
      <div className="landing-record__heading">
        <div>
          <p className="eyebrow">{landingCopy.eyebrow}</p>
          <h2 id="landing-record-title">{window.protocol.name}</h2>
        </div>
        <strong
          className={pending ? "landing-record__status landing-record__status--pending" : "landing-record__status"}
        >
          {decisionLabel(window)}
        </strong>
      </div>
      <p className="landing-record__summary">{window.protocol.summary}</p>
      <dl className="landing-record__facts">
        <div>
          <dt>{landingCopy.decisionDate}</dt>
          <dd>{formatDate(window.decision_boundary)}</dd>
        </div>
        <div>
          <dt>{landingCopy.boundary}</dt>
          <dd>{formatTime(window.decision_boundary)}</dd>
        </div>
        <div>
          <dt>{landingCopy.outcome}</dt>
          <dd>{pending ? landingCopy.recordPendingDetail : decisionDetail(window)}</dd>
        </div>
        <div>
          <dt>{landingCopy.tickOutcome}</dt>
          <dd>{window.tick_outcome_text ?? landingCopy.tickUnavailable}</dd>
        </div>
        <BrokerWrites proof={proof} />
      </dl>
      {!pending ? <p className="landing-record__detail">{decisionDetail(window)}</p> : null}
    </section>
  );
}

export function Landing({ archive, proof, windows, onOpenReplay }: LandingProps) {
  return (
    <div className="landing-view">
      <header className="landing-hero">
        <p className="eyebrow">{landingCopy.eyebrow}</p>
        <h1>{landingCopy.title}</h1>
        <p>{landingCopy.intro}</p>
      </header>
      <CompetitionCard archive={archive} proof={proof} windows={windows} />
      <section className="landing-path" aria-labelledby="landing-path-title">
        <h2 id="landing-path-title">{landingCopy.pathTitle}</h2>
        <ol>
          <li>
            <a href="#competition-record">
              <span>{landingCopy.pathStepRecord}</span>
              <strong>{landingCopy.pathRecord}</strong>
              <small>{landingCopy.pathRecordDetail}</small>
            </a>
          </li>
          <li>
            <a href="#position-review" onClick={onOpenReplay}>
              <span>{landingCopy.pathStepReplay}</span>
              <strong>{landingCopy.pathReplay}</strong>
              <small>{landingCopy.pathReplayDetail}</small>
            </a>
          </li>
          <li>
            <a href="#performance-proof">
              <span>{landingCopy.pathStepProof}</span>
              <strong>{landingCopy.pathProof}</strong>
              <small>{landingCopy.pathProofDetail}</small>
            </a>
          </li>
        </ol>
      </section>
      <section className="landing-alpaca" aria-labelledby="landing-alpaca-title">
        <div>
          <p className="eyebrow">{landingCopy.alpacaTitle}</p>
          <h2 id="landing-alpaca-title">{landingCopy.alpacaTitle}</h2>
          <p>{landingCopy.alpacaIntro}</p>
        </div>
        <ul>
          <li>
            <strong>{landingCopy.tradingApi}</strong>
            <a href="/docs#trading-api">{landingCopy.openArtifact}</a>
          </li>
          <li>
            <strong>{landingCopy.mcpServer}</strong>
            <a href="/docs#mcp-server">{landingCopy.openArtifact}</a>
          </li>
          <li>
            <strong>{landingCopy.cli}</strong>
            <a href="/docs#cli">{landingCopy.openArtifact}</a>
          </li>
        </ul>
      </section>
    </div>
  );
}
