from pathlib import Path

SCENARIOS = (
    "THESIS_INTACT",
    "THETA_TAKEOVER",
    "CATALYST_BROKEN",
    "STALE_QUOTE",
)


def test_reviewer_index_covers_the_anonymous_api_path() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert readme.count("docs/public/README.md") == 1

    guide = Path("docs/public/AGENT_GUIDE.md").read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        assert scenario in guide
    for route in (
        "/api/health",
        "/api/replays/THETA_TAKEOVER",
        "/api/competition-record",
        "/api/proof",
    ):
        assert route in guide
    for boundary in (
        "synthetic fixtures",
        "does not ask you to supply a plan or direction",
        "does not call Alpaca, an AI provider, MCP, or the CLI",
        "cannot place an order",
        "browser keeps its sample-data label visible",
    ):
        assert boundary in guide
    assert "jq '{scenario, action:" in guide
    assert "/openapi.json" in guide
    assert "/docs" in guide


def test_agent_guide_separates_competition_lifecycle_from_account_performance() -> None:
    guide = Path("docs/public/AGENT_GUIDE.md").read_text(encoding="utf-8")

    assert "published paper `NO_TRADE` decisions or position events" in guide
    assert "account checkpoint" in guide
    assert "separate from the position timeline" in guide
    assert "not an organizer score" in guide
    assert "three public proof calls" in guide
