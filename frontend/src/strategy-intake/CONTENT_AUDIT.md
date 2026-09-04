# Strategy-entry content audit

Scope: every current browser surface and every section of `frontend/src/content/public-copy.json` that shapes how a person supplies, understands, reviews, or measures a strategy. This audit does not recommend deleting the working lifecycle code. It classifies its role in the product pivot.

Industry pattern used for calibration: Composer starts with natural language, then shows an editable visual interpretation before insertion and backtesting. Capitalise.ai accepts free-form text. Option Alpha exposes plain-language decision recipes and keeps automation explicitly off until a separate activation step. AlphaDecay should borrow the no-file-required entry, visible interpretation, editable rules, and separate review/arm boundary. It should avoid an opaque chat-only intake and any direct prose-to-trade path.

## Surface inventory

| Existing surface or copy group | Classification | Product value and required change |
|---|---|---|
| Brand lockup and `brand` copy | KEEP | The restrained mark and lowercase name already fit an analytical workbench. |
| Dark/light theme controls and `theme` copy | KEEP | Useful, unobtrusive preference held only in memory. No strategy meaning is attached to it. |
| Product-view navigation and `gateway` copy | RESHAPE | `Competition Record / Explore Demo / Set Up` describes a showcase, not a workflow. Root integration should lead with `Strategies` or `New strategy`, then separate experiments, positions, and account evidence. |
| Workspace state labels | RESHAPE | `REPLAY`, `SET UP`, and `NO COMPETITION TRADES` should become contextual states such as draft, testing, paper-ready, active, stopped, and complete. Keep the compact state treatment. |
| Lifecycle hero and `hero` copy | RESHAPE | “Is this still the trade you meant to own?” is excellent after entry but cannot introduce the product. New-strategy entry needs “Put your market idea into words”; retain the original question inside an active experiment. |
| Position context and scenario selector | SUPPORTING DETAIL | Symbol, structure, expiry, quantity, and scenario choice matter after a protocol exists. They should not appear as required knowledge on the intake screen. |
| Replay provenance banner and `provenance` copy | SUPPORTING DETAIL | The execution boundary is valuable, but repeated Replay labels dominate the current first impression. Show provenance once in Replay and show `DRAFT / not armed / cannot place an order` once during intake. |
| Opening checks and `acquisition` copy | RESHAPE | Event, direction, structure, and candidate checks are the right concepts. Present them later as AlphaDecay’s visible interpretation and test plan, not as preselected fixture facts. |
| Autonomous-cycle diagram and `autonomy` copy | RESHAPE | The empty-book versus managed-position split is real product behavior. Move it after protocol review and keep activation as a separate explicit state. Do not make automation the intake action. |
| Opening/frozen thesis and `thesis` copy | KEEP | Direction, time window, evidence, invalidation, and maximum loss are the core of the pivot. The new form captures them before a strategy becomes a protocol. Keep Greek explanations for later position review. |
| Current-observation and evidence provenance | KEEP | Source, observation time, source tier, classification, relevance, and confidence explain why the system believes something. They belong in experiment evidence after data has been gathered. |
| Evidence-card grid and `provenanceDetail` copy | SUPPORTING DETAIL | Auditability matters, but nine fields per source are too dense for the default view. Show the human conclusion and source first; keep raw classification detail behind disclosure. |
| Market context and `market` copy | SUPPORTING DETAIL | Quotes, IV, DTE, maximum loss, and order/fill state are necessary when evaluating an actual option structure. None should be fake-filled or requested from a user during thesis intake. |
| Scenario detail tabs and `tabs` / `navigation` copy | RESHAPE | `Decision / Thesis vs. position / Agent run / Decision record` is a useful post-entry sequence. A strategy workspace should instead progress from brief to test protocol to results to decision, then reuse these lifecycle tabs after entry. |
| “What changed” decision summary and `scenarios` copy | KEEP | This is the clearest current answer to whether the opening idea still holds. Reuse it for each experiment result rather than only fictional Replay scenarios. |
| Thesis-versus-position comparison table | KEEP | The plan/current/result comparison is distinctive and useful. Extend the same visual grammar to expected-versus-observed test results. |
| Drift score and `drift` copy | SUPPORTING DETAIL | Helpful for an active options position, but too technical for initial strategy quality. Do not repurpose the numeric score as an unexplained strategy grade. |
| Exposure comparison and `exposure` copy | KEEP | Before/current/after makes actions understandable. It belongs after AlphaDecay has selected an option structure, not in idea entry. |
| Alternatives and `alternatives` copy | KEEP | Showing choices considered and rejected is central to trust and the judge-facing story. Use plain-language reasons before technical details. |
| Run log and `run` copy | RESHAPE | The ordered trace is valuable; “Trading API / MCP / model / CLI” is implementation proof, not the user’s primary story. Lead with what was checked and decided; place integrations in supporting detail. |
| Joined decision record and `certificate` / `decisionTrail` copy | SUPPORTING DETAIL | Hashes and separate assessment/execution records prove integrity. Keep them behind “How this was checked,” while the visible result tells the strategy story. |
| Competition Record empty, no-trade, and position states | RESHAPE | Timeline and position history have high value. “Strategy not promoted” is a terminal result, not the product identity. A strategy list should show each experiment’s outcome, including rejected drafts and tests, without making one empty competition account the home page. |
| Position timeline and `competitionRecord` lifecycle copy | KEEP | Open, review, roll, close, cash flow, and rationale form the human-readable story the pivot needs. Add the originating strategy/protocol when root connects this slice. |
| Competition account performance proof and `performance` copy | KEEP | Starting equity, current equity, return, and reconciled lifecycle P&L are judge-visible evidence. Pair them with per-strategy results; keep baseline-contamination and missing-data states fail-closed. |
| Setup view and self-host path | REMOVE from primary workflow | Requiring a person to understand operator deployment before expressing an idea is the wrong front door. Keep deployment documentation as a secondary settings/help destination. |
| Owner provider settings and `ownerSettings` copy | SUPPORTING DETAIL | Provider choice is an operator concern. It enables AI curation but should not be confused with creating a strategy. Keep the protected modal away from the main workflow. |
| Operational empty/error states and `states` copy | RESHAPE | Current messages assume a position. Add useful strategy states: no drafts yet, parsing failed, missing test evidence, rejected, and ready for review. Preserve direct next-step language. |
| Keyboard guide | KEEP | Accessible navigation is valuable once new workspace controls are added. Update its scope only during shell integration. |
| Privacy and important-information dialogs | KEEP | Paper-only, storage, provider-data, model-limit, and no-advice boundaries remain accurate. Add draft handling only if a backend later persists user strategies. |
| Footer and public links | KEEP | Secondary product/legal links remain useful and should stay visually quiet. |
| ACME fixture facts and repeated example labels | REMOVE from intake | They prove Replay but create sample-data clutter and make screenshots look fictional. The intake template uses placeholders, never a fake ticker, trade, return, or completed test. |
| Raw files as an implied strategy contract | REMOVE | A person must not author JSON or know an internal schema. The primary path is an ordinary form/paste. Optional `.txt` and `.md` import accepts prose or simple labeled sections. |

## Design direction

Subject: an options trader or curious investor turning a rough market belief into a bounded experiment. The page’s single job is to make that belief explicit enough for AlphaDecay to review; it cannot arm or trade.

Tokens are inherited rather than reinvented: canvas `#0d0d0d`, sheet `#171717`, quiet `#282828`, primary `#f6f6f6`, muted `#8e8e8e`, and violet accent `#a991ff`, with their existing light-theme counterparts. IBM Plex Sans remains the reading face and IBM Plex Mono remains the utility/status face.

```text
large thesis-led introduction
────────────────────────────────────────────────────────
guided form (wide)             live DRAFT protocol (quiet)
symbol / market                DRAFT · NOT ARMED
plain-language thesis          claim to test
direction · horizon            market · direction · horizon
support | invalidation         support / stop condition
maximum risk · notes           what happens after review
optional text import
plain template
Review draft
```

The signature is the live protocol margin: the person can see AlphaDecay’s exact interpretation while typing, before any AI curation, test, or activation. It borrows the clarity of a lab notebook rather than a chat transcript. The page uses whitespace and thin horizontal rules instead of stacked cards. There are no gradients, thick colored edge accents, fake performance numbers, or decorative motion.

The first design pass considered a stepper. It was removed because numbered stages would imply a linear, complete workflow before the backend review and test phases exist. A single spacious form beside its visible interpretation is more honest and more specific to the current product boundary.

## Integration contract

- Render `StrategyIntake` as the new-strategy view.
- `onDraftReady` emits the exact `StrategyBriefRequest` shape accepted by `POST /api/owner/strategy-drafts`: source, market scope, direction, horizon, evidence, invalidation, dollar risk budget, and notes.
- A paste emits `PASTED_TEXT`; `.txt` emits `TEXT_FILE`; `.md` or `.markdown` emits `MARKDOWN_FILE`. Structured evidence and invalidation use one nonblank line per item.
- Root owns the authenticated, origin/CSRF-protected POST and renders its `DRAFT_REVIEW_REQUIRED / NOT_CURATED / OFF / execution_eligible: false` response. This component performs no fetch and knows no endpoint.
- Register `frontend/src/strategy-intake/public-copy.json` in the public-copy path registry during integration.
- Keep arming, provider calls, orders, app-shell navigation, Replay, and experiment results outside this slice.
