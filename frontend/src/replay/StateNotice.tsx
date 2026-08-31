import { copy } from "../content/copy";
import { stateCopy } from "./labels";
import type { OperationalState } from "./types";

type StateNoticeProps = {
  state: Exclude<OperationalState, "READY">;
};

export function StateNotice({ state }: StateNoticeProps) {
  const message = stateCopy[state];

  return (
    <section
      className={`state-notice state-notice--${state.toLowerCase()}`}
      aria-busy={state === "COLD"}
      aria-labelledby="safe-state-title"
    >
      <p className="section-kicker">{copy.states.title}</p>
      <h2 id="safe-state-title">{message.heading}</h2>
      <p>{message.body}</p>
    </section>
  );
}
