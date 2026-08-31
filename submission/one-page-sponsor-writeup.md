# alphadecay

An options lifecycle agent that checks whether a paper trade still matches its thesis.

`PAPER · SIMULATED · $100,000 COMPETITION START · NO COMPETITION ORDER YET`

## What it does

An options position can stop matching its opening idea before a profit or loss trigger notices. alphadecay keeps that idea beside the position while evidence, time, and exposure change.

Replay follows a sample through its opening checks, then shows four ways the same position could develop. Each example starts with the thesis, loss limit, time horizon, Greek ranges, and option details. Fixed rules compare `HOLD`, `CLOSE`, and a complete spread `ROLL`. The record includes the rejected choices, expected exposure, and a clear note that Replay sent no order.

## AI logic

Replay uses fixed sample data and no model. The paper application can use Gemini or a configured service that follows the OpenAI API format. Before a model sees anything, alphadecay removes private identifiers and narrows the input to evidence fields and source IDs. The model returns a structured classification: event, relation, materiality, relevance, and confidence. It cannot choose a tool, calculate Greeks, select an action, or place an order.

Application code calculates exposure and drift, checks each choice, and applies the risk rules. With no open position, a scheduled paper run may review one candidate from an event plan that was fixed in advance. With a managed position, it runs the lifecycle review. The same evidence hash reuses an earlier classification. If the model fails, the run takes no discretionary action.

## Risk gates

alphadecay accepts paper accounts only. Replay, development checks, and competition results are kept separate. The supported position is a 1:1 defined risk vertical spread. Naked short options, same day entries, market orders, separate leg orders, bulk account actions, exercise, and live trading are unavailable.

Before any paper order, the service checks the market clock, both option legs, quote age and consistency, liquidity, multipliers, Greek units, the position, account status, assignment risk, buying power, maximum loss, total risk, quantity, and earlier orders. Private limits cap loss and quantity. Missing or conflicting data means `NO_ACTION`. A required risk close does not depend on the model, but it still needs broker data.

The service saves its intent before contacting Alpaca. If the connection fails at an uncertain point, it looks up the existing order instead of sending another. It records execution only after Alpaca reports a final order state and the paper account agrees with that result.

## How Alpaca is used

The Trading API supplies account, position, order, and option data. It is the only part of the application allowed to send and reconcile a complete paper spread order. No public paper run has sent an order through this path.

Alpaca MCP provides a small set of read only option, news, corporate action, and clock tools chosen by the application. The model cannot select tools. Alpaca's Basic options feed is indicative rather than OPRA.

The Alpaca CLI stays outside the application. It completed a two leg options limit order dry run against the development paper host and sent no order.

A development rehearsal ran the application's normal startup path, verified the paper endpoint, made eleven provider reads and zero writes, and called one MCP clock tool. It stopped because there was no single managed position. That result shows the connection worked and the service stopped safely. It does not prove a trade, fill, or competition result.

## Competition Record and limits

The competition paper account began at the required $100,000, and its baseline was sealed before any eligible order. No competition order has been submitted. Development history and Replay do not count toward competition performance.

The public record keeps position events separate from the account snapshot. A snapshot will report equity change and return only while the baseline and account history still verify. Those figures are not the organizer's official score. Paper fills leave out market impact, queue position, latency slippage, and other live effects. The organizer has not published its exact P&L formula.

## What can be checked now

Open the [Replay](https://alphadecay.onrender.com), [sample decision API](https://alphadecay.onrender.com/docs#/Replay/anonymous_replay), [repository tests](https://github.com/broken-branch/alphadecay/actions), and [Competition Record](https://alphadecay.onrender.com/api/competition-record). The tests cover Replay policy, anonymous order blocking, duplicate prevention, and the final broker reconciliation rule. The CLI receipt and development rehearsal show paper connections that sent no order. Competition results will appear only after they are verified and published.

Built for the Aug. 28 to Sept. 4, 2026 hackathon. MIT licensed. Paper trading only.
