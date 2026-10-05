"""End-to-end and concurrency tests for rescheduling approved screenings."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import create_app
from app.service import Store
from app.timeutil import to_iso

# fixed far-future / far-past slots so the "not started yet" rule does not
# depend on the wall clock
FUTURE = "2030-06-01T20:00:00Z"
FUTURE_2 = "2030-06-02T20:00:00Z"
FUTURE_3 = "2030-06-03T20:00:00Z"
LATE = "2030-08-01T20:00:00Z"
PAST = "2020-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2031-01-01T00:00:00Z",
}


def canon(value: str) -> str:
    """Turn an ISO-8601 string into the canonical fixed-width UTC form."""
    return to_iso(datetime.fromisoformat(value.replace("Z", "+00:00")))


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 2,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def verify(client, key, start=FUTURE, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def reschedule(client, key, op, new_start=FUTURE_2):
    return client.post(
        f"/verifications/{key}/reschedule",
        json={"operation_key": op, "new_start_time": new_start},
    )


def history(client, key):
    return client.get(f"/verifications/{key}/reschedules")


def used(client, license_id):
    return client.get(f"/licenses/{license_id}").json()["used_sessions"]


# ----------------------------------------------------------------------
# happy path & quota migration
# ----------------------------------------------------------------------
def test_reschedule_moves_schedule_and_returns_record(client):
    lic = make_license(client, max_sessions=3)
    approved = verify(client, "r-1").json()
    assert approved["decision"] == "approved"

    resp = reschedule(client, "r-1", "op-1", FUTURE_2)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] >= 1
    assert data["idempotency_key"] == "r-1"
    assert data["operation_key"] == "op-1"
    assert data["verification_id"] == approved["id"]
    assert data["film_id"] == "film-1"
    assert data["venue_id"] == "hall-a"
    assert data["from_start_time"] == canon(FUTURE)
    assert data["to_start_time"] == canon(FUTURE_2)
    assert data["from_license_id"] == lic["id"]
    assert data["to_license_id"] == lic["id"]
    assert data["from_pool_id"] is None
    assert data["to_pool_id"] is None
    assert data["rescheduled_at"]
    assert data["replay"] is False

    # the current schedule is queryable on the verification itself
    fetched = client.get("/verifications/r-1").json()
    assert fetched["start_time"] == canon(FUTURE_2)
    assert fetched["license_id"] == lic["id"]
    # same licence kept the unit: usage unchanged
    assert used(client, lic["id"]) == 1


def test_own_occupation_participates_in_the_new_decision(client):
    """A full licence can still approve the reschedule of its own session."""
    lic = make_license(client, max_sessions=1)
    assert verify(client, "r-1").json()["decision"] == "approved"
    assert used(client, lic["id"]) == 1

    # without counting the session's own unit as released this would be
    # QUOTA_EXHAUSTED; the released unit participates, so it is approved
    resp = reschedule(client, "r-1", "op-1", FUTURE_2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["to_license_id"] == lic["id"]
    assert used(client, lic["id"]) == 1


def test_reschedule_moves_occupation_to_another_license(client):
    early = make_license(client, valid_to="2030-07-01T00:00:00Z", max_sessions=1)
    late = make_license(client, valid_to="2031-01-01T00:00:00Z", max_sessions=1)
    approved = verify(client, "r-1").json()
    assert approved["license_id"] == early["id"]  # earliest expiring first

    # LATE is beyond the early licence window, so the session moves
    resp = reschedule(client, "r-1", "op-1", LATE)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["from_license_id"] == early["id"]
    assert data["to_license_id"] == late["id"]

    # old licence released once, new licence consumed once
    assert used(client, early["id"]) == 0
    assert used(client, late["id"]) == 1
    fetched = client.get("/verifications/r-1").json()
    assert fetched["license_id"] == late["id"]
    assert fetched["start_time"] == canon(LATE)


def test_reschedule_releases_and_consumes_bound_pools(client):
    pool_a = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    pool_b = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    la = make_license(
        client, valid_to="2030-07-01T00:00:00Z", max_sessions=5,
        pool_id=pool_a["id"],
    )
    lb = make_license(
        client, valid_to="2031-01-01T00:00:00Z", max_sessions=5,
        pool_id=pool_b["id"],
    )
    approved = verify(client, "r-1").json()
    assert approved["license_id"] == la["id"]
    assert approved["pool_id"] == pool_a["id"]

    data = reschedule(client, "r-1", "op-1", LATE).json()
    assert data["from_pool_id"] == pool_a["id"]
    assert data["to_pool_id"] == pool_b["id"]

    # each pool's live usage moved exactly once
    assert client.get(f"/pools/{pool_a['id']}").json()["used_sessions"] == 0
    assert client.get(f"/pools/{pool_b['id']}").json()["used_sessions"] == 1
    assert used(client, la["id"]) == 0
    assert used(client, lb["id"]) == 1


def test_reschedule_within_same_pool_keeps_single_occupation(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    lic = make_license(client, max_sessions=5, pool_id=pool["id"])
    approved = verify(client, "r-1").json()
    assert approved["pool_remaining_quota"] == 0

    # the session's own pool unit participates, so the full pool still
    # approves the move; afterwards exactly one unit is occupied
    resp = reschedule(client, "r-1", "op-1", FUTURE_2)
    assert resp.status_code == 200, resp.text
    assert client.get(f"/pools/{pool['id']}").json()["used_sessions"] == 1
    assert used(client, lic["id"]) == 1


def test_chained_reschedules_keep_history_and_current_schedule(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "r-1")
    first = reschedule(client, "r-1", "op-1", FUTURE_2).json()
    second = reschedule(client, "r-1", "op-2", FUTURE_3).json()
    assert second["from_start_time"] == canon(FUTURE_2)
    assert second["to_start_time"] == canon(FUTURE_3)

    rows = history(client, "r-1").json()
    assert [r["id"] for r in rows] == [first["id"], second["id"]]
    assert rows[0]["to_start_time"] == canon(FUTURE_2)
    assert rows[1]["to_start_time"] == canon(FUTURE_3)
    assert all(r["replay"] is False for r in rows)

    fetched = client.get("/verifications/r-1").json()
    assert fetched["start_time"] == canon(FUTURE_3)
    assert used(client, lic["id"]) == 1


# ----------------------------------------------------------------------
# re-evaluation failures: legacy reject reasons, nothing changes
# ----------------------------------------------------------------------
def test_reschedule_rejected_when_new_slot_quota_exhausted(client):
    la = make_license(client, max_sessions=1)
    lb = make_license(
        client,
        valid_from="2030-06-02T00:00:00Z",
        valid_to="2030-06-02T21:00:00Z",
        max_sessions=1,
    )
    verify(client, "r-1", start=FUTURE)          # occupies la
    moved = verify(client, "r-2", start=FUTURE_2).json()
    assert moved["license_id"] == lb["id"]       # only lb covers that slot

    resp = reschedule(client, "r-2", "op-1", FUTURE_3)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "RESCHEDULE_REJECTED"
    assert body["reject_reason"] == "QUOTA_EXHAUSTED"

    # schedule and occupations unchanged
    assert client.get("/verifications/r-2").json()["start_time"] == canon(FUTURE_2)
    assert used(client, la["id"]) == 1
    assert used(client, lb["id"]) == 1
    assert history(client, "r-2").json() == []


def test_reschedule_rejected_when_new_slot_pool_exhausted(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    la = make_license(client, max_sessions=5, pool_id=pool["id"])
    lb = make_license(
        client,
        valid_from="2030-06-02T00:00:00Z",
        valid_to="2030-06-02T21:00:00Z",
        max_sessions=5,
    )
    verify(client, "r-1", start=FUTURE)          # occupies la + the pool
    moved = verify(client, "r-2", start=FUTURE_2).json()
    assert moved["license_id"] == lb["id"]

    resp = reschedule(client, "r-2", "op-1", FUTURE_3)
    assert resp.status_code == 422
    assert resp.json()["reject_reason"] == "POOL_EXHAUSTED"
    assert client.get(f"/pools/{pool['id']}").json()["used_sessions"] == 1
    assert used(client, lb["id"]) == 1
    assert history(client, "r-2").json() == []


def test_reschedule_rejected_when_new_slot_out_of_window(client):
    lic = make_license(client, valid_to="2030-07-01T00:00:00Z", max_sessions=1)
    verify(client, "r-1")
    resp = reschedule(client, "r-1", "op-1", "2031-06-01T20:00:00Z")
    assert resp.status_code == 422
    assert resp.json()["reject_reason"] == "TIME_OUT_OF_WINDOW"
    assert client.get("/verifications/r-1").json()["start_time"] == canon(FUTURE)
    assert used(client, lic["id"]) == 1


def test_reschedule_rejected_when_new_slot_license_revoked(client):
    la = make_license(client, max_sessions=1)
    lb = make_license(
        client,
        valid_from="2030-06-02T00:00:00Z",
        valid_to="2030-06-02T21:00:00Z",
        max_sessions=1,
    )
    verify(client, "r-1", start=FUTURE)
    verify(client, "r-2", start=FUTURE_2)
    client.post(f"/licenses/{la['id']}/revoke", json={"reason": "撤片"})

    resp = reschedule(client, "r-2", "op-1", FUTURE_3)
    assert resp.status_code == 422
    assert resp.json()["reject_reason"] == "LICENSE_REVOKED"
    assert used(client, lb["id"]) == 1
    assert history(client, "r-2").json() == []


def test_reschedule_rejected_when_new_slot_license_suspended(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "r-1")
    # suspension registered after the approval applies to the re-evaluation
    client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2030-06-03T00:00:00Z",
            "suspend_to": "2030-06-04T00:00:00Z",
            "reason": "版权方临时通知暂停",
        },
    )
    resp = reschedule(client, "r-1", "op-1", FUTURE_3)
    assert resp.status_code == 422
    assert resp.json()["reject_reason"] == "LICENSE_SUSPENDED"
    assert client.get("/verifications/r-1").json()["start_time"] == canon(FUTURE)
    assert used(client, lic["id"]) == 1


def test_failed_reschedule_leaves_no_record_and_key_is_reusable(client):
    la = make_license(client, max_sessions=1)
    lb = make_license(
        client,
        valid_from="2030-06-02T00:00:00Z",
        valid_to="2030-06-02T21:00:00Z",
        max_sessions=1,
    )
    verify(client, "r-1", start=FUTURE)
    verify(client, "r-2", start=FUTURE_2)
    assert reschedule(client, "r-2", "op-1", FUTURE_3).status_code == 422
    assert history(client, "r-2").json() == []

    # once the blocker frees its unit, the same operation key succeeds
    client.post("/verifications/r-1/cancel", json={"reason": "场次取消"})
    resp = reschedule(client, "r-2", "op-1", FUTURE_3)
    assert resp.status_code == 200, resp.text
    assert resp.json()["to_license_id"] == la["id"]
    assert used(client, la["id"]) == 1
    assert used(client, lb["id"]) == 0


# ----------------------------------------------------------------------
# eligibility & request validation
# ----------------------------------------------------------------------
def test_reschedule_unknown_key_is_404(client):
    resp = reschedule(client, "no-such-key", "op-1")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_reschedule_rejected_verification_is_422(client):
    rejected = verify(client, "r-rej", film="unknown", venue="nowhere").json()
    assert rejected["decision"] == "rejected"
    resp = reschedule(client, "r-rej", "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"


def test_reschedule_cancelled_session_is_422(client):
    make_license(client)
    verify(client, "r-1")
    client.post("/verifications/r-1/cancel", json={"reason": "场次取消"})
    resp = reschedule(client, "r-1", "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_CANCELLED"


def test_reschedule_after_opening_code_claimed_is_422(client):
    make_license(client)
    verify(client, "r-1")
    claim = client.post("/verifications/r-1/opening-code")
    assert claim.status_code == 200
    resp = reschedule(client, "r-1", "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "OPENING_CODE_ALREADY_ISSUED"
    # the claimed code and the schedule are untouched
    assert client.get("/verifications/r-1/opening-code").json()["status"] == "issued"
    assert client.get("/verifications/r-1").json()["start_time"] == canon(FUTURE)


def test_reschedule_started_session_is_422(client):
    lic = make_license(client, max_sessions=5)
    verify(client, "r-past", start=PAST)
    resp = reschedule(client, "r-past", "op-1", FUTURE_2)
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"
    assert used(client, lic["id"]) == 1


def test_reschedule_new_start_must_be_in_the_future(client):
    make_license(client)
    verify(client, "r-1")
    resp = reschedule(client, "r-1", "op-1", PAST)
    assert resp.status_code == 422
    assert resp.json()["code"] == "NEW_START_NOT_IN_FUTURE"


def test_reschedule_new_start_must_differ_from_current(client):
    make_license(client)
    verify(client, "r-1")
    assert reschedule(client, "r-1", "op-1", FUTURE).status_code == 422
    # the same instant written with another offset is still "unchanged"
    resp = reschedule(client, "r-1", "op-2", "2030-06-01T22:00:00+02:00")
    assert resp.status_code == 422
    assert resp.json()["code"] == "NEW_START_UNCHANGED"


def test_reschedule_requires_operation_key_and_new_start(client):
    make_license(client)
    verify(client, "r-1")
    assert client.post("/verifications/r-1/reschedule", json={}).status_code == 422
    assert client.post(
        "/verifications/r-1/reschedule", json={"operation_key": "op-1"}
    ).status_code == 422
    assert client.post(
        "/verifications/r-1/reschedule",
        json={"operation_key": "op-1", "new_start_time": "not-a-time"},
    ).status_code == 422
    # rejected attempts leave the session reschedulable
    assert reschedule(client, "r-1", "op-1", FUTURE_2).status_code == 200


# ----------------------------------------------------------------------
# idempotency of the reschedule operation itself
# ----------------------------------------------------------------------
def test_same_operation_key_same_content_replays_without_remigration(client):
    lic = make_license(client, max_sessions=2)
    verify(client, "r-1")
    verify(client, "r-2", start=FUTURE_3)

    first = reschedule(client, "r-1", "op-1", FUTURE_2).json()
    second = reschedule(client, "r-1", "op-1", FUTURE_2).json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["rescheduled_at"] == first["rescheduled_at"]
    assert second["to_start_time"] == first["to_start_time"]

    # migrated exactly once: one history row, usage unchanged
    assert len(history(client, "r-1").json()) == 1
    assert used(client, lic["id"]) == 2
    assert client.get("/verifications/r-1").json()["start_time"] == canon(FUTURE_2)


def test_same_operation_key_different_content_conflicts(client):
    make_license(client, max_sessions=3)
    verify(client, "r-1")
    first = reschedule(client, "r-1", "op-1", FUTURE_2).json()

    resp = reschedule(client, "r-1", "op-1", FUTURE_3)
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["existing"]["id"] == first["id"]
    assert body["existing"]["to_start_time"] == canon(FUTURE_2)
    # nothing moved again
    assert client.get("/verifications/r-1").json()["start_time"] == canon(FUTURE_2)
    assert len(history(client, "r-1").json()) == 1


def test_operation_key_reused_on_another_verification_conflicts(client):
    make_license(client, max_sessions=3)
    verify(client, "r-1")
    verify(client, "r-2", start=FUTURE_2)
    reschedule(client, "r-1", "op-1", FUTURE_3)

    resp = reschedule(client, "r-2", "op-1", FUTURE_3)
    assert resp.status_code == 409
    assert resp.json()["existing"]["idempotency_key"] == "r-1"


def test_replay_is_returned_even_after_later_state_changes(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "r-1")
    first = reschedule(client, "r-1", "op-1", FUTURE_2).json()
    # move on and even cancel afterwards; the operation key still replays
    reschedule(client, "r-1", "op-2", FUTURE_3)
    client.post("/verifications/r-1/cancel", json={"reason": "场次取消"})

    replay = reschedule(client, "r-1", "op-1", FUTURE_2).json()
    assert replay["replay"] is True
    assert replay["id"] == first["id"]
    assert used(client, lic["id"]) == 0


# ----------------------------------------------------------------------
# original verification semantics survive; current schedule is queryable
# ----------------------------------------------------------------------
def test_original_decision_snapshot_and_replay_survive_reschedule(client):
    make_license(client, max_sessions=2)
    approved = verify(client, "r-1").json()
    assert approved["remaining_quota"] == 1
    reschedule(client, "r-1", "op-1", FUTURE_2)

    # the stored decision and quota snapshot are untouched; the schedule
    # fields reflect the current (rescheduled) slot
    fetched = client.get("/verifications/r-1").json()
    assert fetched["decision"] == "approved"
    assert fetched["remaining_quota"] == 1
    assert fetched["verified_at"] == approved["verified_at"]
    assert fetched["start_time"] == canon(FUTURE_2)

    # same-key replay of the original verification still returns the row
    replay = verify(client, "r-1").json()
    assert replay["replay"] is True
    assert replay["decision"] == "approved"
    assert replay["remaining_quota"] == 1
    assert replay["start_time"] == canon(FUTURE_2)

    # and a different payload for the same key still conflicts
    conflict = verify(client, "r-1", start=FUTURE_3)
    assert conflict.status_code == 409


def test_verification_list_shows_current_schedule(client):
    make_license(client, max_sessions=5)
    verify(client, "r-1")
    verify(client, "r-2", start=FUTURE_2)
    reschedule(client, "r-1", "op-1", FUTURE_3)

    rows = client.get("/verifications", params={"film_id": "film-1"}).json()
    by_key = {r["idempotency_key"]: r for r in rows}
    assert by_key["r-1"]["start_time"] == canon(FUTURE_3)
    assert by_key["r-2"]["start_time"] == canon(FUTURE_2)


def test_history_endpoint_unknown_key_and_empty_history(client):
    assert history(client, "no-such-key").status_code == 404
    make_license(client)
    verify(client, "r-1")
    assert history(client, "r-1").json() == []


# ----------------------------------------------------------------------
# follow-up operations (cancel / claim / redeem) use the current schedule
# ----------------------------------------------------------------------
def test_cancel_after_reschedule_releases_the_new_license(client):
    early = make_license(client, valid_to="2030-07-01T00:00:00Z", max_sessions=1)
    late = make_license(client, valid_to="2031-01-01T00:00:00Z", max_sessions=1)
    verify(client, "r-1")
    reschedule(client, "r-1", "op-1", LATE)

    data = client.post(
        "/verifications/r-1/cancel", json={"reason": "场次取消"}
    ).json()
    assert data["license_id"] == late["id"]
    assert used(client, early["id"]) == 0
    assert used(client, late["id"]) == 0


def test_claim_and_redeem_follow_the_new_schedule(client, monkeypatch):
    make_license(client, max_sessions=1)
    monkeypatch.setattr(service, "now_iso", lambda: canon("2030-06-01T00:00:00Z"))
    verify(client, "r-1", start=FUTURE)
    reschedule(client, "r-1", "op-1", FUTURE_2)

    # past the ORIGINAL start but before the NEW start: claim still works
    monkeypatch.setattr(service, "now_iso", lambda: canon("2030-06-02T19:00:00Z"))
    claim = client.post("/verifications/r-1/opening-code")
    assert claim.status_code == 200, claim.text
    code = claim.json()["code"]

    # inside the NEW 30-minute window (the old one is long closed): redeem
    monkeypatch.setattr(service, "now_iso", lambda: canon("2030-06-02T20:10:00Z"))
    redeemed = client.post(
        "/redemptions", json={"code": code, "venue_id": "hall-a"}
    )
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["start_time"] == canon(FUTURE_2)


def test_cancel_follows_the_new_schedule(client, monkeypatch):
    lic = make_license(client, max_sessions=1)
    monkeypatch.setattr(service, "now_iso", lambda: canon("2030-06-01T00:00:00Z"))
    verify(client, "r-1", start=FUTURE)
    reschedule(client, "r-1", "op-1", FUTURE_3)

    # past the ORIGINAL start but before the NEW start: cancel still works
    monkeypatch.setattr(service, "now_iso", lambda: canon("2030-06-02T12:00:00Z"))
    resp = client.post("/verifications/r-1/cancel", json={"reason": "场次取消"})
    assert resp.status_code == 200, resp.text
    assert used(client, lic["id"]) == 0


# ----------------------------------------------------------------------
# concurrency: no oversell, no double migration
# ----------------------------------------------------------------------
def test_concurrent_duplicate_reschedule_migrates_once(tmp_path):
    store = Store(str(tmp_path / "reschedule-dup.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=2,
    )
    store.verify(
        film_id="film-x",
        venue_id="hall-1",
        start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
        idempotency_key="seed",
    )

    results, errors = [], []

    def worker() -> None:
        try:
            results.append(
                store.reschedule_verification(
                    "seed",
                    operation_key="op-shared",
                    new_start_time=datetime(2030, 6, 2, 20, 0,
                                            tzinfo=timezone.utc),
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    assert sum(1 for r in results if not r["replay"]) == 1
    assert len({r["id"] for r in results}) == 1
    # migrated exactly once: usage moved once, one history row
    assert store.get_license(lic["id"])["used_sessions"] == 1
    assert len(store.list_reschedules("seed")) == 1
    assert store.get_verification("seed")["start_time"] == canon(FUTURE_2)


def test_concurrent_reschedule_and_verify_never_exceed_quota(tmp_path):
    store = Store(str(tmp_path / "reschedule-concurrent.db"))
    la = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2030, 7, 1, tzinfo=timezone.utc),
        max_sessions=1,
    )
    lb = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2030, 7, 15, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=1,
    )
    seed = store.verify(
        film_id="film-x",
        venue_id="hall-1",
        start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
        idempotency_key="seed",
    )
    assert seed["license_id"] == la["id"]

    results, errors = [], []
    barrier = threading.Barrier(21)

    def reschedule_worker() -> None:
        barrier.wait()
        try:
            # LATE is beyond la's window, so the seed moves to lb and
            # frees la's single unit exactly once
            results.append(
                ("reschedule", store.reschedule_verification(
                    "seed",
                    operation_key="op-1",
                    new_start_time=datetime(2030, 8, 1, 20, 0,
                                            tzinfo=timezone.utc),
                ))
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def verify_worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(
                (
                    "verify",
                    store.verify(
                        film_id="film-x",
                        venue_id="hall-1",
                        start_time=datetime(2030, 6, 1, 20, 0,
                                            tzinfo=timezone.utc),
                        idempotency_key=f"new-{i}",
                    ),
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=verify_worker, args=(i,)) for i in range(20)]
    threads.append(threading.Thread(target=reschedule_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    reschedules = [r for kind, r in results if kind == "reschedule"]
    assert len(reschedules) == 1 and reschedules[0]["replay"] is False
    approved = [
        r for kind, r in results
        if kind == "verify" and r["decision"] == "approved"
    ]
    # at most one newcomer grabs the freed unit; live usage always matches
    assert len(approved) <= 1
    assert store.get_license(la["id"])["used_sessions"] == len(approved)
    assert store.get_license(lb["id"])["used_sessions"] == 1
    assert store.get_verification("seed")["license_id"] == lb["id"]
