from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend.app.alpaca.activities import AccountActivitiesAdapter, ActivityReadError


def activity(activity_id: str, activity_type: str, minute: int) -> dict[str, object]:
    value: dict[str, object] = {
        "id": activity_id,
        "activity_type": activity_type,
        "symbol": "NVDA260918C00230000",
        "qty": "1",
    }
    if activity_type == "FILL":
        value.update(
            transaction_time=f"2026-08-28T15:{minute:02d}:00Z",
            price="8.25",
            side="buy",
            order_id="order-private",
        )
    else:
        value["date"] = "2026-08-28"
    return value


def test_account_activities_use_only_strict_paginated_paper_get() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("page_token") is None:
            payload = [activity("a1", "OPTRD", 10), activity("a2", "OPASN", 11)]
        else:
            payload = [activity("a3", "OPEXC", 12)]
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = AccountActivitiesAdapter(
        client,
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        page_size=2,
    )

    result = adapter.list_activities(datetime(2026, 8, 28, 15, 0, tzinfo=UTC))

    assert [item["activity_id"] for item in result] == ["a1", "a2", "a3"]
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.path == "/v2/account/activities" for request in requests)
    assert "activity_types" not in requests[0].url.params
    assert requests[0].url.params["after"] == "2026-08-27"
    assert requests[1].url.params["page_token"] == "a2"


def test_activity_adapter_rejects_nonpaper_host_before_transport() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    with pytest.raises(ActivityReadError, match="ACTIVITY_HOST_FORBIDDEN"):
        AccountActivitiesAdapter(
            client,
            base_url="https://api.alpaca.markets",
            api_key="fixture-key",
            secret_key="fixture-secret",
        )


def test_activity_schema_tolerates_documented_provider_extensions() -> None:
    payload = activity("a1", "OPTRD", 10)
    payload["unexpected"] = "rejected"
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[payload]))
    )
    adapter = AccountActivitiesAdapter(
        client,
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
    )

    assert len(adapter.list_activities(datetime(2026, 8, 28, 15, 0, tzinfo=UTC))) == 1


def test_activity_collection_uses_fixed_until_and_binds_order_lineage() -> None:
    requests: list[httpx.Request] = []
    since = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
    until = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = activity("a1", "FILL", 10)
        payload["client_order_id"] = "approved-a0"
        payload["cum_qty"] = "1"
        payload["leaves_qty"] = "0"
        payload["type"] = "fill"
        return httpx.Response(200, json=[payload])

    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: until,
    )

    items, pagination = adapter.collect(
        since=since,
        until=until,
        provider_to_client={"order-private": "approved-a0"},
    )

    assert items[0].provider_order_id == "order-private"
    assert items[0].client_order_id == "approved-a0"
    assert items[0].signed_quantity == 1
    assert pagination.requested_end == until
    assert pagination.visibility_complete_through == until - timedelta(hours=24)
    assert requests[0].url.params["until"] == until.isoformat()


def test_fill_activity_requires_exact_order_lineage() -> None:
    payload = activity("a1", "FILL", 10)
    until = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)
    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[payload]))),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: until,
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_ORDER_LINEAGE_INCOMPLETE"):
        adapter.collect(
            since=until - timedelta(days=1),
            until=until,
            provider_to_client={},
        )


def test_activity_collection_rejects_window_shorter_than_visibility_horizon() -> None:
    until = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)
    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: until,
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_VISIBILITY_HORIZON_INCOMPLETE"):
        adapter.collect(
            since=until - timedelta(hours=23),
            until=until,
            provider_to_client={},
        )


def test_activity_collection_normalizes_domain_validation_failure() -> None:
    invalid = activity("a1", "OPTRD", 10)
    invalid["qty"] = "0"
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[invalid]))
    )
    until = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)
    adapter = AccountActivitiesAdapter(
        client,
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: until,
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_SCHEMA_INVALID"):
        adapter.collect(
            since=until - timedelta(days=1),
            until=until,
            provider_to_client={"order-private": "approved-a0"},
        )


@pytest.mark.parametrize(
    "second, code",
    [
        (activity("a1", "FILL", 10), "ACTIVITY_PAGINATION_NONMONOTONIC"),
        (activity("a2", "FILL", 9), "ACTIVITY_PAGINATION_NONMONOTONIC"),
    ],
)
def test_activity_collection_rejects_duplicate_or_nonmonotonic_pages(
    second: dict[str, object], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = activity("a1", "FILL", 10)
        if request.url.params.get("page_token") is not None:
            payload = second
        return httpx.Response(200, json=[payload])

    until = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)
    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        page_size=1,
        clock=lambda: until,
    )

    with pytest.raises(ActivityReadError, match=code):
        adapter.collect(
            since=until - timedelta(days=1),
            until=until,
            provider_to_client={"order-private": "approved-a0"},
        )


def test_activity_collection_rejects_missing_terminal_page() -> None:
    until = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)
    adapter = AccountActivitiesAdapter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=[activity("a1", "FILL", 10)])
            )
        ),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        page_size=1,
        max_pages=1,
        clock=lambda: until,
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_PAGE_LIMIT_EXCEEDED"):
        adapter.collect(
            since=until - timedelta(days=1),
            until=until,
            provider_to_client={"order-private": "approved-a0"},
        )


def test_activity_credentials_are_not_followed_to_a_redirect() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://example.com/collect"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapter = AccountActivitiesAdapter(
        client,
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_HTTP_307"):
        adapter.list_activities(datetime(2026, 8, 28, 15, 0, tzinfo=UTC))
    assert len(requests) == 1
