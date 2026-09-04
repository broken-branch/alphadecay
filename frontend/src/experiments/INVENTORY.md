# Product-pivot surface inventory

The primary user is a person testing a bounded options thesis. The page's single job is to show what was tested, what happened, and whether the evidence supports another run.

| Current surface | Decision | Product value after the pivot |
|---|---|---|
| Workspace gateway and Competition / Demo / Setup switcher | RESHAPE | Replace product-mode language with a strategy workspace and experiment history. Setup stays reachable but should not lead the experience. |
| Position Review hero and scenario picker | RESHAPE | A position is one phase of an experiment, not the product. Lead with strategy state, return, and the current decision; keep Replay selection clearly labeled as a demonstration. |
| Selected-position strip | KEEP | Underlying, spread, expiry, quantity, quote age, and state are the quickest way to understand what capital is exposed. Give the strip more room and pair it with entry, exit, and maximum-risk context. |
| Opening thesis | KEEP | This is the experiment's hypothesis. Add why it was chosen and the evidence that would invalidate it. |
| Opening checks / acquisition summary | RESHAPE | Translate candidate-selection mechanics into “Why this strategy” and “What had to be true.” Provider and routing mechanics are not primary user information. |
| Autonomous-cycle facts | SUPPORTING DETAIL | Armed state, schedule, and reconciliation prove autonomy, but they do not explain performance. Put them in a secondary disclosure or operational view. |
| Market / option context | RESHAPE | Keep entry, current/exit value, spread terms, maximum risk, and fill state. Remove the wall of equally weighted metrics; Greeks and quote diagnostics belong in supporting detail. |
| Decision summary | KEEP | Make the current decision/state the first sentence on the page and connect it to the evidence that caused it. |
| Thesis-versus-position comparison | KEEP | This is the clearest explanation of whether the experiment still matches its premise. Present it after performance, with plain-language state labels. |
| Exposure comparison and drift score | SUPPORTING DETAIL | Useful for an options practitioner, but too technical for the first read. Keep behind an “Options detail” disclosure. |
| Evidence panel and invalidation table | RESHAPE | Keep sources, observations, support/contradiction, and invalidation. Collapse source IDs, materiality scores, and provider tiers into technical detail. |
| Alternative choices | KEEP | Showing what was rejected makes the autonomous decision legible. Place it beside the decision event that used it, not on a detached tab. |
| Agent run log | RESHAPE | Replace infrastructure stages with a chronological experiment timeline: strategy accepted, entry considered/filled, reviews, exit, and outcome. Provider calls and hash validation are secondary detail. |
| Joined decision record / certificate | SUPPORTING DETAIL | Preserve auditability, but move policy versions and hashes into one disclosure at the end. “Certificate” is internal-sounding and should not be a primary destination. |
| Competition Record | RESHAPE | Turn the archive into experiment history with one truthful lifecycle per strategy. A no-trade result remains a result and should explain the failed condition. |
| Competition account proof | KEEP | Paper P&L and return are central evidence. Show explicit unavailable/not-published states and never borrow Replay values. |
| Proof hashes, predecessor links, linked-certificate counts | SUPPORTING DETAIL | Important for verification, low value for understanding the strategy. Keep in “Technical record.” |
| Replay fixture provenance | SUPPORTING DETAIL | Replay is valuable for explaining behavior, but must never look like live or competition performance. Keep the label adjacent to every Replay result. |
| Setup / owner credentials / self-host paths | KEEP | Necessary for operating the product, but separate from strategy analysis and never part of the primary performance workspace. |
| Repeated environment, provider, and technology badges | REMOVE | They consume attention without answering whether the strategy worked. One clear “paper” or “Replay” label is enough. |
| Thick semantic side borders and stacked status cards | REMOVE | They create the rejected AI-dashboard effect and make every fact look urgent. Use spacing, type, and thin structural rules instead. |

## Integration intent

`ExperimentWorkspace` accepts a reviewed strategy definition plus the existing typed competition position and performance-proof shapes. Optional value-path and benchmark arrays are shown only when supplied. Missing proof, benchmark, entry, exit, risk, or path data produces an explicit absent-data state rather than a placeholder result.
