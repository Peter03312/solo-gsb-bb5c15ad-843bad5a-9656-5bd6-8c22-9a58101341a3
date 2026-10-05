"""End-to-end and concurrency tests for one-time opening codes and venue redemption."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import create_app
from app.service import Store
from app.timeutil import to_iso

START = "2026-06-01T19:30:00Z"
WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
}


def canon(value: str) -> str:
    """Turn an ISO-8601 string into the canonical fixed-width UTC form."""
    return to_iso(datetime.fromisoformat(value.replace("Z", "+00:00")))


def iso_at(**delta) -> str:
    base = datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc)
    return to_iso(base + timedelta(**delta))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # default wall clock: 30 minutes before the planned start
    monkeypatch.setattr(service, "now_iso", lambda: iso_at(minutes=-30))
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 5,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def verify(client, key, start=START, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def claim(client, key):
    return client.post(f"/verifications/{key}/opening-code")


def status(client, key):
    return client.get(f"/verifications/{key}/opening-code")


def redeem(client, code, venue="hall-a"):
    return client.post("/redemptions", json={"code": code, "venue_id": venue})


def jump(monkeypatch, value: str) -> None:
    """Freeze the service clock at ``value`` (ISO-8601)."""
    monkeypatch.setattr(service, "now_iso", lambda: canon(value))


def approved_code(client, key, start=START, venue="hall-a", film="film-1"):
    verify(client, key, start=start, venue=venue, film=film)
    resp = claim(client, key)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"]
    return body["code"]


# ----------------------------------------------------------------------
# operator claim
# ----------------------------------------------------------------------
def test_first_claim_returns_one_time_code(client):
    lic = make_license(client)
    approved = verify(client, "oc-1").json()
    resp = claim(client, "oc-1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "issued"
    assert data["replay"] is False
    assert data["code"].startswith("OC-") and len(data["code"]) > 10
    assert data["issued_at"]
    assert data["cancelled_at"] is None
    assert data["redemption"] is None
    assert data["idempotency_key"] == "oc-1"
    # claiming does not move any quota counters
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_codes_are_unique_across_sessions(client):
    make_license(client)
    verify(client, "oc-1")
    verify(client, "oc-2", start="2026-06-02T19:30:00Z")
    code1 = claim(client, "oc-1").json()["code"]
    code2 = claim(client, "oc-2").json()["code"]
    assert code1 != code2


def test_repeat_claim_returns_status_only_never_the_code(client):
    make_license(client)
    verify(client, "oc-1")
    first = claim(client, "oc-1").json()
    second = claim(client, "oc-1").json()
    assert second["replay"] is True
    assert second["code"] is None
    assert second["status"] == "issued"
    assert second["issued_at"] == first["issued_at"]


def test_status_query_shows_not_issued_and_never_exposes_code(client):
    make_license(client)
    verify(client, "oc-1")
    resp = status(client, "oc-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "not_issued"
    assert data["issued_at"] is None
    assert data["cancelled_at"] is None
    assert data["redemption"] is None
    assert "code" not in data  # status surface has no code field at all


def test_status_query_unknown_key_is_404(client):
    resp = status(client, "no-such-key")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_claim_unknown_key_is_404(client):
    resp = claim(client, "no-such-key")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_claim_rejected_verification_is_422(client):
    verify(client, "oc-rej", film="unknown", venue="nowhere")
    resp = claim(client, "oc-rej")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"
    # nothing was issued
    assert status(client, "oc-rej").json()["status"] == "not_issued"


def test_claim_cancelled_session_is_422_and_no_code(client):
    make_license(client)
    verify(client, "oc-cancel")
    cancel = client.post(
        "/verifications/oc-cancel/cancel", json={"reason": "设备故障"}
    )
    assert cancel.status_code == 200
    resp = claim(client, "oc-cancel")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_CANCELLED"
    assert status(client, "oc-cancel").json()["status"] == "not_issued"


def test_claim_at_or_after_start_is_422(client, monkeypatch):
    make_license(client)
    verify(client, "oc-late")
    jump(monkeypatch, START)  # exactly the planned start
    resp = claim(client, "oc-late")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"


def test_claim_after_redemption_reports_redeemed_status_only(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, START)
    first = redeem(client, code)
    assert first.status_code == 200
    # re-claim after redemption: status only, replay, code never re-revealed
    again = claim(client, "oc-1")
    assert again.status_code == 200
    data = again.json()
    assert data["replay"] is True
    assert data["code"] is None
    assert data["status"] == "redeemed"
    assert data["redemption"]["id"] == first.json()["id"]


# ----------------------------------------------------------------------
# venue redemption: happy path
# ----------------------------------------------------------------------
def test_redeem_at_planned_start_returns_receipt_with_linked_authorization(
    client, monkeypatch
):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 5}
    ).json()
    lic = make_license(client, pool_id=pool["id"])
    code = approved_code(client, "oc-1")

    jump(monkeypatch, START)
    resp = redeem(client, code)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["replay"] is False
    assert data["id"] >= 1
    assert data["idempotency_key"] == "oc-1"
    assert data["film_id"] == "film-1"
    assert data["venue_id"] == "hall-a"
    assert data["start_time"] == canon(START)
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] == pool["id"]
    assert data["redeemed_at"] == canon(START)

    # status query now carries the first receipt but still no code
    st = status(client, "oc-1").json()
    assert st["status"] == "redeemed"
    assert st["redemption"]["redeemed_at"] == canon(START)
    assert st["redemption"]["license_id"] == lic["id"]
    assert "code" not in st


def test_redeem_inside_window_succeeds(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, "2026-06-01T19:59:59Z")  # one second before closing
    resp = redeem(client, code)
    assert resp.status_code == 200
    assert resp.json()["replay"] is False


# ----------------------------------------------------------------------
# redemption rejections (no receipt is ever produced)
# ----------------------------------------------------------------------
def test_redeem_before_start_is_too_early(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, "2026-06-01T19:29:59Z")
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "REDEMPTION_TOO_EARLY"
    assert status(client, "oc-1").json()["status"] == "issued"  # no receipt


def test_redeem_at_window_end_is_closed_half_open_window(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, "2026-06-01T20:00:00Z")  # start + exactly 30 minutes
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "REDEMPTION_WINDOW_CLOSED"
    assert status(client, "oc-1").json()["status"] == "issued"  # no receipt


def test_redeem_later_than_window_is_closed(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, "2026-06-01T21:00:00Z")
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "REDEMPTION_WINDOW_CLOSED"


def test_redeem_wrong_venue_is_mismatch(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, START)
    resp = redeem(client, code, venue="hall-b")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VENUE_MISMATCH"
    # rejected attempt leaves the code redeemable by the correct venue
    ok = redeem(client, code, venue="hall-a")
    assert ok.status_code == 200
    assert ok.json()["replay"] is False


def test_redeem_unknown_code_is_404(client, monkeypatch):
    jump(monkeypatch, START)
    resp = redeem(client, "OC-DEADBEEFDEADBEEFDEADBEEF")
    assert resp.status_code == 404
    assert resp.json()["code"] == "OPENING_CODE_NOT_FOUND"


def test_redeem_blank_code_or_venue_is_422(client):
    for body in ({"code": "  ", "venue_id": "hall-a"},
                 {"code": "OC-X", "venue_id": ""},
                 {"venue_id": "hall-a"}):
        resp = client.post("/redemptions", json=body)
        assert resp.status_code == 422, body


def test_redeem_voided_code_after_cancel_is_rejected(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    # cancelled while still before the start; the issued code is void
    client.post("/verifications/oc-1/cancel", json={"reason": "取消"})
    st = status(client, "oc-1").json()
    assert st["status"] == "voided"
    assert st["cancelled_at"]
    jump(monkeypatch, START)
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_CANCELLED"
    assert st["redemption"] is None


# ----------------------------------------------------------------------
# idempotent redemption replay
# ----------------------------------------------------------------------
def test_redeem_replay_after_window_returns_first_receipt(client, monkeypatch):
    lic = make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, START)
    first = redeem(client, code).json()

    # window has long closed and license is gone: replay still wins
    jump(monkeypatch, "2026-06-02T00:00:00Z")
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"})
    second = redeem(client, code).json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["redeemed_at"] == first["redeemed_at"]
    assert second["license_id"] == first["license_id"]


def test_redeem_wrong_venue_never_receives_replay(client, monkeypatch):
    make_license(client)
    code = approved_code(client, "oc-1")
    jump(monkeypatch, START)
    redeem(client, code)
    jump(monkeypatch, "2026-06-01T19:45:00Z")
    resp = redeem(client, code, venue="hall-b")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VENUE_MISMATCH"


# ----------------------------------------------------------------------
# later revocation / suspension must not change redemption eligibility
# ----------------------------------------------------------------------
def test_revocation_after_claim_does_not_block_redemption(client, monkeypatch):
    lic = make_license(client, max_sessions=2)
    code = approved_code(client, "oc-1")
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "临时撤片"})
    jump(monkeypatch, START)
    resp = redeem(client, code)
    assert resp.status_code == 200, resp.text
    # original verification and its quota occupation are untouched
    v = client.get("/verifications/oc-1").json()
    assert v["decision"] == "approved" and v["cancelled"] is False
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_suspension_after_claim_does_not_block_redemption(client, monkeypatch):
    lic = make_license(client)
    code = approved_code(client, "oc-1")
    client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2026-06-01T00:00:00Z",
            "suspend_to": "2026-06-02T00:00:00Z",
            "reason": "暂停放映",
        },
    )
    jump(monkeypatch, START)
    resp = redeem(client, code)
    assert resp.status_code == 200
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_redeem_does_not_change_quota_or_pool_usage(client, monkeypatch):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 5}
    ).json()
    lic = make_license(client, max_sessions=5, pool_id=pool["id"])
    code = approved_code(client, "oc-1")
    jump(monkeypatch, START)
    redeem(client, code)
    redeem(client, code)  # replay
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1
    assert client.get(f"/pools/{pool['id']}").json()["used_sessions"] == 1


# ----------------------------------------------------------------------
# cancellation after claim voids the code immediately
# ----------------------------------------------------------------------
def test_cancel_after_claim_voids_code_and_repeat_claim_shows_voided(client):
    make_license(client)
    verify(client, "oc-1")
    issued = claim(client, "oc-1").json()
    assert issued["status"] == "issued"
    client.post("/verifications/oc-1/cancel", json={"reason": "取消"})

    st = status(client, "oc-1").json()
    assert st["status"] == "voided"
    assert st["cancelled_at"]
    # repeat claim after cancellation returns the voided status, never the code
    again = claim(client, "oc-1").json()
    assert again["replay"] is True
    assert again["code"] is None
    assert again["status"] == "voided"


# ----------------------------------------------------------------------
# concurrency: code uniqueness and receipt uniqueness under contention
# ----------------------------------------------------------------------
class _ThreadClock:
    """Thread-local replacement for service.now_iso used by race tests."""

    def __init__(self, default: str):
        self._local = threading.local()
        self.default = default

    def __call__(self) -> str:
        return getattr(self._local, "value", self.default)

    def set(self, value: str) -> None:
        self._local.value = value


@pytest.fixture()
def race_store(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "now_iso", _ThreadClock(iso_at(minutes=-30)))
    store = Store(str(tmp_path / "race.db"))
    store.ensure()
    store.create_license(
        film_id="film-x",
        venue_id="hall-a",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=10,
    )
    store.verify(
        film_id="film-x",
        venue_id="hall-a",
        start_time=datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc),
        idempotency_key="race-1",
    )
    return store


def test_concurrent_claims_produce_exactly_one_code(race_store):
    payloads, errors = [], []
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        try:
            payloads.append(race_store.claim_opening_code("race-1"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    first = [p for p in payloads if not p["replay"]]
    replays = [p for p in payloads if p["replay"]]
    assert len(first) == 1
    assert len(replays) == 19
    assert first[0]["code"].startswith("OC-")
    assert all(p["code"] is None for p in replays)
    assert len({p["issued_at"] for p in payloads}) == 1
    assert race_store.get_opening_code_status("race-1")["status"] == "issued"


def test_concurrent_claim_and_cancel_keep_code_and_cancellation_unique(
    race_store,
):
    claims, claim_errors, cancels, cancel_errors = [], [], [], []
    barrier = threading.Barrier(11)

    def claim_worker() -> None:
        barrier.wait()
        try:
            claims.append(race_store.claim_opening_code("race-1"))
        except service.UnprocessableError as exc:
            claim_errors.append(exc.code)
        except Exception as exc:  # pragma: no cover - failure path
            claim_errors.append(exc)

    def cancel_worker() -> None:
        barrier.wait()
        try:
            cancels.append(
                race_store.cancel_verification("race-1", reason="并发取消")
            )
        except Exception as exc:  # pragma: no cover - failure path
            cancel_errors.append(exc)

    threads = [threading.Thread(target=claim_worker) for _ in range(10)]
    threads.append(threading.Thread(target=cancel_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not cancel_errors
    assert len(cancels) == 1  # cancellation itself is idempotent-safe here
    assert all(e == "SESSION_CANCELLED" for e in claim_errors)

    st = race_store.get_opening_code_status("race-1")
    if st["status"] == "voided":
        # one claim beat the cancellation; the code exists but is dead
        first = [p for p in claims if not p["replay"]]
        assert len(first) == 1 and first[0]["code"]
        assert all(p["code"] is None for p in claims if p["replay"])
        assert st["cancelled_at"]
    else:
        # cancellation beat every claim: no code may exist at all
        assert st["status"] == "not_issued"
        assert len(claims) == 0
        assert len(claim_errors) == 10


def test_concurrent_redemptions_produce_exactly_one_receipt(
    race_store, monkeypatch
):
    code = race_store.claim_opening_code("race-1")["code"]
    clock = _ThreadClock(iso_at())  # exactly the planned start
    monkeypatch.setattr(service, "now_iso", clock)

    receipts, errors = [], []
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        try:
            receipts.append(
                race_store.redeem_opening_code(code=code, venue_id="hall-a")
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(receipts) == 20
    assert sum(1 for r in receipts if not r["replay"]) == 1
    assert len({r["id"] for r in receipts}) == 1
    assert all(r["redeemed_at"] == receipts[0]["redeemed_at"] for r in receipts)
    assert race_store.get_opening_code_status("race-1")["status"] == "redeemed"


def test_concurrent_redeem_succeeds_while_cancel_at_start_fails(
    race_store, monkeypatch
):
    code = race_store.claim_opening_code("race-1")["code"]
    # everyone observes the clock at exactly start_time: cancellation is
    # already illegal while redemption is just becoming legal
    monkeypatch.setattr(service, "now_iso", _ThreadClock(iso_at()))
    receipts, cancel_errors = [], []
    barrier = threading.Barrier(11)

    def redeem_worker() -> None:
        barrier.wait()
        receipts.append(
            race_store.redeem_opening_code(code=code, venue_id="hall-a")
        )

    def cancel_worker() -> None:
        barrier.wait()
        try:
            race_store.cancel_verification("race-1", reason="开场瞬间取消")
        except service.UnprocessableError as exc:
            cancel_errors.append(exc.code)
        except Exception as exc:  # pragma: no cover - failure path
            cancel_errors.append(exc)

    threads = [threading.Thread(target=redeem_worker) for _ in range(10)]
    threads.append(threading.Thread(target=cancel_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert cancel_errors == ["SESSION_ALREADY_STARTED"]
    assert sum(1 for r in receipts if not r["replay"]) == 1
    assert len({r["id"] for r in receipts}) == 1
    assert race_store.get_opening_code_status("race-1")["status"] == "redeemed"
