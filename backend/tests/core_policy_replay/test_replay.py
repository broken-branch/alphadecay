import json
from copy import deepcopy
from datetime import timedelta

import pytest
from pydantic import ValidationError

from backend.app.contracts.v1 import Action, ReplayPresentation, ReplayResponse
from backend.app.replay import available_scenarios, run_replay
from backend.app.replay import runner as replay_runner


def test_all_named_replays_are_deterministic_and_execution_disabled() -> None:
    expected_actions = {
        "THESIS_INTACT": Action.HOLD,
        "THETA_TAKEOVER": Action.ROLL,
        "CATALYST_BROKEN": Action.CLOSE,
        "STALE_QUOTE": Action.NO_ACTION,
    }

    assert available_scenarios() == tuple(expected_actions)
    for scenario, expected_action in expected_actions.items():
        first = run_replay(scenario)
        second = run_replay(scenario)

        assert isinstance(first, ReplayResponse)
        assert first == second
        assert first.assessment.action == expected_action
        assert first.provenance_label == "REPLAY / FIXTURE DATA"
        assert first.execution_enabled is False
        assert first.certificate.account_role.value == "REPLAY"
        assert first.certificate.execution_state == "NOT_REQUESTED"
        assert len(first.input_hash) == 64


def test_replay_assessment_matches_the_public_policy_seam() -> None:
    replay = run_replay("THETA_TAKEOVER")

    assert replay.assessment.drift_score == 47
    assert tuple(item.action for item in replay.assessment.alternatives) == (
        Action.HOLD,
        Action.CLOSE,
        Action.ROLL,
    )
    assert replay.certificate.assessment == replay.assessment


def test_scenarios_share_one_prospective_thesis_identity() -> None:
    replays = tuple(run_replay(scenario) for scenario in available_scenarios())

    assert {replay.certificate.thesis.thesis_id for replay in replays} == {
        replays[0].certificate.thesis.thesis_id
    }
    assert {replay.certificate.thesis.thesis_hash for replay in replays} == {
        replays[0].certificate.thesis.thesis_hash
    }
    assert {replay.certificate.thesis.thesis.thesis_code for replay in replays} == {
        "POST_EVENT_CONTINUATION_V1"
    }
    assert {replay.certificate.thesis.thesis.underlying for replay in replays} == {"ACME"}


def test_replay_presentation_is_bound_to_one_consistent_opening_record() -> None:
    replays = tuple(run_replay(scenario) for scenario in available_scenarios())

    openings = {replay.presentation.opening.model_dump_json() for replay in replays}
    assert len(openings) == 1
    for replay in replays:
        opening = replay.presentation.opening
        market = replay.presentation.market
        assert (
            opening.maximum_loss
            == opening.entry_net_debit_per_share_usd
            * opening.quantity
            * opening.contract_multiplier
        )
        assert opening.maximum_loss <= opening.approved_risk_cap
        assert market.dte == (opening.expiration_date - market.assessed_at.date()).days
        assert replay.presentation.integration.model_dump(
            mode="json", exclude={"schema_version"}
        ) == {
            "fixture_validation": "COMPLETE",
            "deterministic_policy": "COMPLETE",
            "trading_api": "NOT_RUN",
            "mcp": "NOT_RUN",
            "model": "NOT_RUN",
            "cli": "NOT_RUN",
            "order_entry": "DISABLED",
        }


def test_replay_presentation_rejects_inconsistent_market_and_roll_math() -> None:
    replay = run_replay("THETA_TAKEOVER")
    presentation = replay.presentation.model_dump(mode="json")

    bad_pnl = deepcopy(presentation)
    bad_pnl["market"]["open_pnl"] = "-149"
    with pytest.raises(ValidationError, match="position arithmetic"):
        ReplayPresentation.model_validate(bad_pnl)

    bad_dte = deepcopy(presentation)
    bad_dte["market"]["dte"] += 1
    with pytest.raises(ValidationError, match="DTE"):
        ReplayPresentation.model_validate(bad_dte)

    bad_roll = deepcopy(presentation)
    bad_roll["roll"]["resulting_maximum_loss"] = "501"
    with pytest.raises(ValidationError, match="roll record"):
        ReplayPresentation.model_validate(bad_roll)

    bad_width = deepcopy(presentation)
    bad_width["roll"]["short_strike"] = "140"
    with pytest.raises(ValidationError, match="roll record"):
        ReplayPresentation.model_validate(bad_width)

    negative_debit = deepcopy(presentation)
    negative_debit["roll"]["estimated_net_debit_per_share_usd"] = "-0.10"
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ReplayPresentation.model_validate(negative_debit)

    future_evidence = deepcopy(presentation)
    future_evidence["evidence"]["classifications"][0]["observed_at"] = "2026-09-03T15:15:01Z"
    with pytest.raises(ValidationError, match="evidence cannot follow"):
        ReplayPresentation.model_validate(future_evidence)

    assert replay.presentation.roll is not None
    assert (
        replay.presentation.roll.expiration_date - replay.presentation.opening.expiration_date
        == timedelta(days=7)
    )


def test_stale_quote_replay_stops_before_action_or_execution() -> None:
    replay = run_replay("STALE_QUOTE")

    assert replay.assessment.quality == "STALE"
    assert replay.assessment.action == Action.NO_ACTION
    assert replay.assessment.execution_decision == "NO_ACTION"
    assert replay.assessment.rationale_code == "EXECUTION_DATA_MISSING"
    assert replay.assessment.components is None
    assert all(not alternative.eligible for alternative in replay.assessment.alternatives)
    assert replay.certificate.expected_after_exposure is None
    assert replay.certificate.attempts == ()
    assert replay.certificate.execution_state == "NOT_REQUESTED"
    assert replay.execution_enabled is False


def test_replay_rejects_quote_status_that_disagrees_with_policy_quality(
    tmp_path, monkeypatch
) -> None:
    envelope = json.loads((replay_runner.FIXTURE_ROOT / "STALE_QUOTE.json").read_text())
    envelope["payload"]["quality"] = "COMPLETE"
    envelope["input_hash"] = replay_runner._hash(envelope["payload"])
    (tmp_path / "STALE_QUOTE.json").write_text(json.dumps(envelope))
    monkeypatch.setattr(replay_runner, "FIXTURE_ROOT", tmp_path)

    with pytest.raises(replay_runner.ReplayFixtureError, match="REPLAY_PRESENTATION_MISMATCH"):
        run_replay("STALE_QUOTE")
