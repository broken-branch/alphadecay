import { loadCompetitionRecord } from "./competition-record/api";
import { loadCompetitionProof, loadReplayFixture } from "./replay/api";
import { ReplayShell } from "./replay/ReplayShell";
import { loadRuntimeStatus } from "./runtime/api";

export function App() {
  return (
    <ReplayShell
      archiveLoader={loadCompetitionRecord}
      replayLoader={loadReplayFixture}
      proofLoader={loadCompetitionProof}
      runtimeLoader={loadRuntimeStatus}
    />
  );
}
