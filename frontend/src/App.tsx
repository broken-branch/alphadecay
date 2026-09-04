import { loadCompetitionRecord } from "./competition-record/api";
import { loadExperimentWindows } from "./experiments";
import { loadCompetitionProof, loadReplayFixture } from "./replay/api";
import { ReplayShell } from "./replay/ReplayShell";
import { loadRuntimeStatus } from "./runtime/api";

export function App() {
  return (
    <ReplayShell
      archiveLoader={loadCompetitionRecord}
      experimentWindowsLoader={loadExperimentWindows}
      replayLoader={loadReplayFixture}
      proofLoader={loadCompetitionProof}
      runtimeLoader={loadRuntimeStatus}
    />
  );
}
