"""Tests for the JSON API silo.

What is worth pinning here is not that it serves JSON, but the four
conditions a consumer of a real small-business API has to survive:
pagination, bearer auth, rate limiting, and the service going away.
"""

import json
import urllib.error
import urllib.request

import pytest

from simulator.ports import PortRegistry
from simulator.silo import SiloError
from simulator.silos.rest import (
    API_PREFIX,
    RETRY_AFTER_SECONDS,
    RestSilo,
    _decode_cursor,
    _encode_cursor,
)


@pytest.fixture
def silo(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["square"])
    made = RestSilo(name="square", data_dir=tmp_path / "square",
                    port=registry.port("square"), page_size=10)
    made.create()
    made.start()
    try:
        yield made
    finally:
        made.stop()


def fetch(silo, path, *, token="sim-token"):
    """One request, returning (status, body, headers)."""
    request = urllib.request.Request(f"{silo.base_url}{path}")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers)


def drain(silo, collection, *, token="sim-token"):
    """Follow the cursor to the end, the way a correct consumer must."""
    records, path = [], f"{API_PREFIX}/{collection}"
    while True:
        status, body, _ = fetch(silo, path, token=token)
        assert status == 200
        records.extend(body["data"])
        if "cursor" not in body:
            return records
        path = f"{API_PREFIX}/{collection}?cursor={body['cursor']}"


# -- pagination ------------------------------------------------------

def test_a_collection_arrives_in_pages(silo):
    silo.publish("payments", [{"id": f"p{index}"} for index in range(25)])
    status, body, _ = fetch(silo, f"{API_PREFIX}/payments")
    assert status == 200
    assert len(body["data"]) == 10
    assert "cursor" in body


def test_following_the_cursor_yields_every_record_exactly_once(silo):
    published = [{"id": f"p{index}"} for index in range(25)]
    silo.publish("payments", published)
    assert drain(silo, "payments") == published


def test_a_consumer_that_ignores_the_cursor_silently_loses_data(silo):
    # THE most common integration bug against APIs of this shape, and
    # the reason pagination is simulated rather than skipped. It is
    # silent: the data looks fine, there is just less of it.
    silo.publish("payments", [{"id": f"p{index}"} for index in range(25)])
    _, first_page, _ = fetch(silo, f"{API_PREFIX}/payments")
    assert len(first_page["data"]) == 10
    assert len(drain(silo, "payments")) == 25


def test_the_last_page_carries_no_cursor(silo):
    silo.publish("payments", [{"id": "p1"}])
    _, body, _ = fetch(silo, f"{API_PREFIX}/payments")
    assert body["data"] == [{"id": "p1"}]
    assert "cursor" not in body


def test_an_empty_collection_is_an_empty_page_not_an_error(silo):
    silo.publish("payments", [])
    status, body, _ = fetch(silo, f"{API_PREFIX}/payments")
    assert status == 200
    assert body == {"data": []}


def test_a_collection_exactly_one_page_long_ends_cleanly(silo):
    # The off-by-one that produces an endless final page.
    silo.publish("payments", [{"id": f"p{index}"} for index in range(10)])
    _, body, _ = fetch(silo, f"{API_PREFIX}/payments")
    assert len(body["data"]) == 10
    assert "cursor" not in body


def test_cursors_are_opaque(silo):
    silo.publish("payments", [{"id": f"p{index}"} for index in range(25)])
    _, body, _ = fetch(silo, f"{API_PREFIX}/payments")
    # Not a readable offset. A consumer that cracks one open and does
    # arithmetic on it is writing a bug this should not make easy.
    assert body["cursor"] != "10"
    assert not body["cursor"].isdigit()
    assert _decode_cursor(body["cursor"]) == 10


def test_a_malformed_cursor_is_rejected_rather_than_ignored(silo):
    silo.publish("payments", [{"id": "p1"}])
    status, body, _ = fetch(silo, f"{API_PREFIX}/payments?cursor=not-a-cursor")
    assert status == 400
    assert body["errors"][0]["code"] == "INVALID_CURSOR"


def test_cursor_round_trip():
    assert _decode_cursor(_encode_cursor(42)) == 42
    with pytest.raises(ValueError, match="malformed cursor"):
        _decode_cursor("!!!")


# -- authentication --------------------------------------------------

def test_a_missing_token_is_a_json_401_not_a_connection_failure(silo):
    # A different thing for a consumer to handle, and the thing that
    # really happens.
    status, body, _ = fetch(silo, f"{API_PREFIX}/payments", token=None)
    assert status == 401
    assert body["errors"][0]["code"] == "UNAUTHORIZED"


def test_a_wrong_token_is_also_401(silo):
    status, _, _ = fetch(silo, f"{API_PREFIX}/payments", token="wrong")
    assert status == 401


def test_authentication_can_be_turned_off(tmp_path):
    # Some small-business APIs really are open on a private network,
    # and a pack should be able to say so.
    registry = PortRegistry.allocate(tmp_path, ["open"])
    silo = RestSilo(name="open", data_dir=tmp_path / "open",
                    port=registry.port("open"), token=None)
    silo.create()
    silo.start()
    try:
        silo.publish("things", [{"id": "t1"}])
        status, body, _ = fetch(silo, f"{API_PREFIX}/things", token=None)
        assert status == 200
        assert body["data"] == [{"id": "t1"}]
        assert "token" not in silo.connection().details
    finally:
        silo.stop()


# -- rate limiting ---------------------------------------------------

def test_rate_limiting_returns_429_with_a_retry_after(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["limited"])
    silo = RestSilo(name="limited", data_dir=tmp_path / "limited",
                    port=registry.port("limited"), rate_limit=2)
    silo.create()
    silo.start()
    try:
        silo.publish("payments", [{"id": "p1"}])
        assert fetch(silo, f"{API_PREFIX}/payments")[0] == 200
        assert fetch(silo, f"{API_PREFIX}/payments")[0] == 200
        status, body, headers = fetch(silo, f"{API_PREFIX}/payments")
        assert status == 429
        assert body["errors"][0]["code"] == "RATE_LIMITED"
        assert headers["Retry-After"] == str(RETRY_AFTER_SECONDS)

        silo.reset_rate_limit()
        assert fetch(silo, f"{API_PREFIX}/payments")[0] == 200
    finally:
        silo.stop()


def test_rate_limiting_is_off_by_default(silo):
    # On by default would make a consumer's first run fail for a reason
    # that has nothing to do with the data.
    silo.publish("payments", [{"id": "p1"}])
    for _ in range(50):
        assert fetch(silo, f"{API_PREFIX}/payments")[0] == 200


# -- unknown routes --------------------------------------------------

def test_an_unknown_collection_is_404(silo):
    status, body, _ = fetch(silo, f"{API_PREFIX}/nope")
    assert status == 404
    assert "nope" in body["errors"][0]["detail"]


def test_a_path_outside_the_api_prefix_is_404(silo):
    assert fetch(silo, "/admin")[0] == 404


# -- lifecycle -------------------------------------------------------

def test_it_needs_a_port(tmp_path):
    assert RestSilo.requires_port is True
    silo = RestSilo(name="square", data_dir=tmp_path, port=0)
    registry = PortRegistry.allocate_for(tmp_path, [silo])
    assert set(registry.ports) == {"square"}


def test_the_descriptor_carries_the_base_url_and_token(silo):
    descriptor = silo.connection()
    assert descriptor.kind == "rest"
    assert descriptor.details["base_url"] == silo.base_url
    assert descriptor.details["auth"] == "bearer"
    assert descriptor.details["token"] == "sim-token"


def test_starting_twice_is_quiet(silo):
    silo.start()
    assert silo.is_reachable()


def test_stopping_twice_is_quiet(silo):
    silo.stop()
    silo.stop()
    assert silo.is_reachable() is False


def test_a_taken_port_fails_loudly(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["a"])
    first = RestSilo(name="a", data_dir=tmp_path / "a", port=registry.port("a"))
    first.create()
    first.start()
    second = RestSilo(name="b", data_dir=tmp_path / "b", port=registry.port("a"))
    second.create()
    try:
        with pytest.raises(SiloError, match="cannot listen"):
            second.start()
    finally:
        first.stop()


# -- reachability and outage -----------------------------------------

def test_a_401_still_counts_as_reachable(tmp_path):
    # The service is up and refusing this request, which is a different
    # condition from the service being down. Conflating them is how a
    # health check reports an outage during a rate-limit window.
    registry = PortRegistry.allocate(tmp_path, ["auth"])
    silo = RestSilo(name="auth", data_dir=tmp_path / "auth",
                    port=registry.port("auth"), token="secret")
    silo.create()
    silo.start()
    try:
        assert fetch(silo, f"{API_PREFIX}/", token=None)[0] == 401
        assert silo.is_reachable() is True
    finally:
        silo.stop()


def test_a_rate_limited_silo_is_still_reachable(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["limited"])
    silo = RestSilo(name="limited", data_dir=tmp_path / "limited",
                    port=registry.port("limited"), rate_limit=0)
    silo.create()
    silo.start()
    try:
        assert fetch(silo, f"{API_PREFIX}/anything")[0] == 429
        assert silo.is_reachable() is True
    finally:
        silo.stop()


def test_terminate_makes_the_api_stop_answering(silo):
    silo.publish("payments", [{"id": "p1"}])
    assert silo.is_reachable()
    silo.terminate()
    assert silo.is_reachable() is False


def test_stopping_after_a_terminate_is_quiet(silo):
    silo.terminate()
    silo.stop()
    assert silo.is_reachable() is False


# -- publishing ------------------------------------------------------

def test_publish_replaces_and_append_adds(silo):
    silo.publish("payments", [{"id": "p1"}])
    silo.append("payments", {"id": "p2"})
    assert drain(silo, "payments") == [{"id": "p1"}, {"id": "p2"}]
    silo.publish("payments", [{"id": "p9"}])
    assert drain(silo, "payments") == [{"id": "p9"}]


def test_published_records_are_copied_not_referenced(silo):
    # A pack holding its own dict must not be able to change what the
    # API already served by mutating it afterwards.
    record = {"id": "p1", "amount": 100}
    silo.publish("payments", [record])
    record["amount"] = 999
    assert drain(silo, "payments") == [{"id": "p1", "amount": 100}]


def test_append_creates_a_collection(silo):
    silo.append("refunds", {"id": "r1"})
    assert drain(silo, "refunds") == [{"id": "r1"}]


def test_timestamps_are_served_as_iso_strings(silo):
    # These APIs emit ISO 8601 with an offset, not epoch integers, and
    # parsing them is a real source of off-by-one-day errors.
    silo.publish("payments", [{"id": "p1", "created_at": "2026-03-02T10:15:00+00:00"}])
    record = drain(silo, "payments")[0]
    assert record["created_at"] == "2026-03-02T10:15:00+00:00"


# -- registration ----------------------------------------------------

def test_it_is_registered_as_a_kind():
    from simulator.silos import SILO_TYPES

    assert SILO_TYPES["rest"] is RestSilo
