from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import (
    LIFECYCLE_NO_ACTION_UNBOUND_CODE,
    RETRY_NO_TRADE_STATUS,
    JudgeStrategySummary,
    JudgeSubmissionStory,
    JudgeTimelineEvent,
    OrderAttemptSummary,
    ProviderRetryAuditSummary,
    SubmissionDecisionStory,
)

_IDENTIFIER = re.compile(
    r"(?:\b[0-9a-f]{64}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'])/(?:[^\s\"']+)")
_EASTERN = ZoneInfo("America/New_York")
_PRIVATE_FIELDS = (
    "maximum_loss_usd",
    "limit_per_share",
    "long_strike",
    "short_strike",
    "account_fingerprint",
    "order_id",
    "position_id",
    "request_id",
)


def build_judge_story(story: SubmissionDecisionStory) -> JudgeSubmissionStory:
    events: list[JudgeTimelineEvent] = []

    def add(
        stage: str,
        status: str,
        description: str,
        *,
        occurred_at: datetime | None = None,
        reason_codes: tuple[str, ...] = (),
        attempt: OrderAttemptSummary | None = None,
    ) -> None:
        events.append(
            JudgeTimelineEvent(
                sequence=len(events) + 1,
                stage=stage,
                occurred_at=occurred_at,
                status=status,
                reason_codes=reason_codes,
                attempt_ordinal=None if attempt is None else attempt.ordinal,
                filled_quantity=None if attempt is None else attempt.filled_quantity,
                ordered_quantity=None if attempt is None else attempt.quantity,
                description=description,
            )
        )

    add(
        "PLAN_FROZEN",
        "FROZEN",
        (
            f"The {story.strategy.underlying} defined risk options plan was frozen before "
            "this market decision."
        ),
        occurred_at=story.strategy.frozen_at,
    )
    for status, group in _grouped_checks(story.provider_retry_audit):
        count = len(group)
        first = _clock(group[0].recorded_at)
        last = _clock(group[-1].recorded_at)
        span = f"at {first}" if count == 1 else f"from {first} to {last}"
        if status == "OPPORTUNITY_DECISION_PENDING":
            description = (
                f"{count} scheduled check{'s' if count != 1 else ''} {span} ran before the "
                "entry window opened and recorded no decision."
            )
        else:
            description = (
                f"{count} scheduled check{'s' if count != 1 else ''} {span} authorized no "
                "trade. Each is retained as append only evidence; none replaced the later "
                "binding policy decision."
            )
        add(
            "SCHEDULED_CHECK",
            _public_check_status(status),
            description,
            occurred_at=group[-1].recorded_at,
        )

    entry_decision = "NO_TRADE" if story.outcome == "NO_TRADE" else "ENTRY_APPROVED"
    add(
        "ENTRY_DECISION",
        entry_decision,
        (
            "The deterministic entry policy refused the trade."
            if entry_decision == "NO_TRADE"
            else "The deterministic entry policy approved one defined risk vertical."
        ),
        occurred_at=story.decision_time,
        reason_codes=story.decision_reason_codes,
    )

    if story.entry_execution is not None:
        attempt = story.entry_order_lifecycle.attempts[-1]
        add(
            "ENTRY_ORDER_SUBMITTED",
            "SUBMITTED",
            "AlphaDecay submitted the approved paper order as one multileg spread.",
            occurred_at=story.entry_execution.submitted_at,
        )
        add(
            "ENTRY_FILL",
            attempt.state,
            "The paper broker recorded a complete simulated fill for the spread.",
            occurred_at=story.entry_execution.filled_at,
            attempt=attempt,
        )
        add(
            "ENTRY_RECONCILIATION",
            story.entry_execution_status,
            (
                f"Reconciliation state {story.entry_execution.reconciliation_sequence} "
                "certified the filled entry and whole account check."
            ),
            occurred_at=story.entry_execution.reconciled_at,
        )
        add(
            "POSITION_MATERIALIZED",
            "OPEN",
            "The reconciled spread became an open managed lifecycle position.",
            occurred_at=story.entry_execution.reconciled_at,
        )
    else:
        for attempt in story.entry_order_lifecycle.attempts:
            add(
                "ENTRY_ORDER",
                attempt.state,
                _attempt_description("entry", attempt),
                attempt=attempt,
            )
    if entry_decision == "ENTRY_APPROVED" and story.entry_execution is None:
        add(
            "ENTRY_RECONCILIATION",
            story.entry_execution_status,
            _entry_reconciliation_description(story),
        )

    for assessment in story.lifecycle_assessments:
        add(
            "LIFECYCLE_TICK",
            assessment.action,
            _assessment_description(assessment.action, assessment.reason_code),
            occurred_at=assessment.assessed_at,
            reason_codes=(_public_reason_code(assessment.reason_code),),
        )

    for attempt in story.exit_order_lifecycle.attempts:
        add(
            "EXIT_ORDER",
            attempt.state,
            _attempt_description("exit", attempt),
            attempt=attempt,
        )

    final_time = None if story.terminal is None else story.terminal.outcome_time
    add(
        "CURRENT_STATE",
        story.outcome,
        story.account_impact.description,
        occurred_at=final_time if story.outcome == "CLOSED" else None,
    )
    stage_order = {
        "PLAN_FROZEN": 0,
        "SCHEDULED_CHECK": 1,
        "ENTRY_DECISION": 2,
        "ENTRY_ORDER": 3,
        "ENTRY_ORDER_SUBMITTED": 4,
        "ENTRY_FILL": 5,
        "ENTRY_RECONCILIATION": 6,
        "POSITION_MATERIALIZED": 7,
        "LIFECYCLE_TICK": 8,
        "EXIT_ORDER": 9,
        "CURRENT_STATE": 10,
    }
    events.sort(
        key=lambda event: (
            event.stage == "CURRENT_STATE",
            (
                datetime.max.replace(tzinfo=UTC)
                if event.stage == "CURRENT_STATE"
                else event.occurred_at
                or (
                    final_time
                    if event.stage == "EXIT_ORDER" and final_time is not None
                    else story.decision_time
                )
            ),
            stage_order[event.stage],
            event.sequence,
        )
    )
    events = [event.model_copy(update={"sequence": index}) for index, event in enumerate(events, 1)]

    export = JudgeSubmissionStory(
        strategy=JudgeStrategySummary(
            name=story.strategy.name,
            version=story.strategy.version,
            underlying=story.strategy.underlying,
        ),
        entry_decision=entry_decision,
        decision_time=story.decision_time,
        decision_reason_codes=story.decision_reason_codes,
        rationale=story.why_selected,
        evidence_used=story.evidence_used,
        management_policy=story.management_policy,
        spread=None if story.selected_spread is None else "DEFINED_RISK_VERTICAL",
        expiration=None if story.selected_spread is None else story.selected_spread.expiration,
        quantity=None if story.selected_spread is None else story.selected_spread.quantity,
        debit_paid_usd=(
            None
            if story.account_impact.reconciled_cashflow_usd is None
            else abs(story.account_impact.reconciled_cashflow_usd)
        ),
        scheduled_cycle_count=(
            len(story.provider_retry_audit) + 1 + len(story.lifecycle_assessments)
        ),
        approved_cycle_count=1 if entry_decision == "ENTRY_APPROVED" else 0,
        no_trade_cycle_count=sum(
            item.status == RETRY_NO_TRADE_STATUS for item in story.provider_retry_audit
        ),
        post_fill_no_action_count=sum(
            item.action == "NO_ACTION" and item.reason_code == LIFECYCLE_NO_ACTION_UNBOUND_CODE
            for item in story.lifecycle_assessments
        ),
        no_trade_reason_codes=tuple(
            sorted(
                {
                    _public_check_status(item.status)
                    for item in story.provider_retry_audit
                    if item.status == RETRY_NO_TRADE_STATUS
                }
            )
        ),
        timeline=tuple(events),
        outcome=story.outcome,
        final_reconciliation=story.account_impact.description,
        realized_pnl_status=story.account_impact.realized_pnl_status,
        realized_pnl_usd=story.account_impact.realized_pnl_usd,
    )
    _assert_safe(export.model_dump_json())
    return export


def render_judge_markdown(story: JudgeSubmissionStory) -> str:
    decision_reasons = ", ".join(f"`{item}`" for item in story.decision_reason_codes)
    lines = [
        "# AlphaDecay paper-trading decision story",
        "",
        (
            f"AlphaDecay evaluated frozen plan `{story.strategy.name}` version "
            f"{story.strategy.version}, a {story.strategy.underlying} defined risk options "
            "strategy, in paper trading. This record comes only from persisted decisions, "
            "simulated fills, and reconciliation records."
        ),
        "",
        "## Entry decision",
        "",
        f"The result was **{story.entry_decision}**. The recorded reason was {decision_reasons}.",
        "",
        (
            "A defined risk limit stayed binding throughout the decision. Exact strikes and "
            "private numeric limits are omitted."
        ),
        "",
        "## Why this plan was evaluated",
        "",
    ]
    lines.extend(f"- {item}" for item in story.rationale)
    lines.extend(["", "## Evidence used", ""])
    lines.extend(f"- {item}" for item in story.evidence_used)
    if story.management_policy is not None:
        policy = story.management_policy
        lines.extend(
            [
                "",
                "## Management policy",
                "",
                (
                    f"The position is rechecked every {policy.evaluation_interval_minutes} "
                    "minutes. A close is required at "
                    f"+{policy.profit_target_spread_value_pct}% spread value, "
                    f"{policy.stop_loss_spread_value_pct}% spread value, or the mandatory "
                    f"boundary at {_format_time(policy.mandatory_close_at)}."
                ),
            ]
        )
    lines.extend(["", "## Timeline", ""])
    for event in story.timeline:
        when = _format_time(event.occurred_at)
        reasons = (
            " Reasons: " + ", ".join(f"`{item}`" for item in event.reason_codes) + "."
            if event.reason_codes
            else ""
        )
        fill = (
            f" Filled {event.filled_quantity} of {event.ordered_quantity}."
            if event.attempt_ordinal is not None
            else ""
        )
        if event.sequence > 1:
            lines.append("")
        lines.append(
            f"{event.sequence}. **{when}; {_stage_label(event.stage)}: "
            f"{event.status}.** {event.description}{fill}{reasons}"
        )

    lines.extend(["", "## Outcome", "", story.final_reconciliation])
    if story.realized_pnl_status == "CERTIFIED" and story.realized_pnl_usd is not None:
        lines.extend(
            [
                "",
                (
                    "The final closed-position reconciliation records realized paper P&L of "
                    f"**{_format_usd(story.realized_pnl_usd)}**."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Realized P&L is not reported because the persisted story does not certify it.",
            ]
        )
    lines.extend(
        [
            "",
            "Scheduled checks shown above are audit history. They never replace the terminal "
            "policy decision or the final reconciled outcome.",
            "",
        ]
    )
    output = "\n".join(lines)
    _assert_safe(output)
    return output


def _attempt_description(kind: str, attempt: OrderAttemptSummary) -> str:
    if attempt.state == "PREPARED":
        return f"The paper {kind} order was prepared, but no broker submission is recorded."
    if attempt.ordinal == 0:
        return f"The first paper {kind} order attempt reached state {attempt.state}."
    return f"Paper {kind} replacement attempt {attempt.ordinal} reached state {attempt.state}."


def _entry_reconciliation_description(story: SubmissionDecisionStory) -> str:
    if story.entry_execution_status == "FILLED":
        return "The simulated entry fill was reconciled into the managed position."
    if story.outcome == "PARTIALLY_FILLED":
        return "The partial simulated entry was preserved without inventing a complete position."
    return "The persisted entry state was recorded without inferring a later fill."


def _assessment_description(action: str, reason_code: str) -> str:
    if reason_code == LIFECYCLE_NO_ACTION_UNBOUND_CODE:
        return "The scheduled check authorized no action; the position stays open under its plan."
    if action == "HOLD":
        return "The lifecycle policy kept the reconciled position open."
    if action == "CLOSE" and "FORCED" in reason_code:
        return "The lifecycle policy required the scheduled forced close."
    if action == "CLOSE":
        return "The lifecycle policy required the position to close."
    if action == "ROLL":
        return "The lifecycle policy approved a defined risk roll."
    return "The lifecycle policy recorded no executable action."


def _clock(value: datetime) -> str:
    return value.astimezone(_EASTERN).strftime("%-I:%M %p ET")


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "time not recorded"
    return value.astimezone(_EASTERN).strftime("%B %-d, %Y %-I:%M:%S %p ET")


def _stage_label(stage: str) -> str:
    return stage.replace("_", " ").title()


def _format_usd(value: Decimal) -> str:
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def _assert_safe(text: str) -> None:
    if _IDENTIFIER.search(text) or _ABSOLUTE_PATH.search(text):
        raise ValueError("SUBMISSION_STORY_REDACTION_FAILED")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return
    lowered = text.casefold()
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_kind") != "SUBMISSION_JUDGE_STORY"
        or any(field in lowered for field in _PRIVATE_FIELDS)
    ):
        raise ValueError("SUBMISSION_STORY_REDACTION_FAILED")


def _public_check_status(status: str) -> str:
    """Map a persisted pre-entry check status to its public timeline status."""
    return "NO_TRADE" if status == RETRY_NO_TRADE_STATUS else status


def _public_reason_code(reason_code: str) -> str:
    """Map a persisted lifecycle reason code to its public timeline code."""
    if reason_code == LIFECYCLE_NO_ACTION_UNBOUND_CODE:
        return "CHECK_AUTHORIZED_NO_ACTION"
    return reason_code


def _grouped_checks(
    checks: Sequence[ProviderRetryAuditSummary],
) -> tuple[tuple[str, tuple[ProviderRetryAuditSummary, ...]], ...]:
    """Collapse consecutive scheduled checks with the same status into one timeline entry."""
    groups: list[tuple[str, list[ProviderRetryAuditSummary]]] = []
    for check in checks:
        if groups and groups[-1][0] == check.status:
            groups[-1][1].append(check)
        else:
            groups.append((check.status, [check]))
    return tuple((status, tuple(items)) for status, items in groups)
