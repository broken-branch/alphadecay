from pathlib import Path

SCENARIOS = (
    "THESIS_INTACT",
    "THETA_TAKEOVER",
    "CATALYST_BROKEN",
    "STALE_QUOTE",
)


def test_agent_guide_is_prominent_and_covers_the_anonymous_one_visit_path() -> None:
    readme_lines = Path("README.md").read_text(encoding="utf-8").splitlines()
    guide_link_line = next(
        index for index, line in enumerate(readme_lines, start=1) if "AGENT_GUIDE.md" in line
    )
    assert guide_link_line <= 20

    guide = Path("docs/public/AGENT_GUIDE.md").read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        assert scenario in guide
    for route in (
        "/api/health",
        "/api/replays/$scenario",
        "/api/competition-record",
        "/api/proof",
    ):
        assert route in guide
    for boundary in (
        "synthetic fixtures",
        "does not ask you to supply a plan or direction",
        "does not call Alpaca, an AI provider, MCP, or the CLI",
        "cannot place an order",
        "do not need to return later",
    ):
        assert boundary in guide
    assert "jq '{scenario, action:" in guide
    assert "/openapi.json" in guide
    assert "/docs" in guide


def test_agent_guide_separates_competition_lifecycle_from_account_performance() -> None:
    guide = Path("docs/public/AGENT_GUIDE.md").read_text(encoding="utf-8")

    assert "published timeline for the competition paper account" in guide
    assert "published paper `NO_TRADE` decisions and position events" in guide
    assert "account performance snapshot" in guide
    assert "It is not the lifecycle record or a score calculated by the organizer" in guide
    assert "does not call that model" in guide
