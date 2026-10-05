"""End-to-end tests for rescheduling approved screenings before they start.

覆盖：
- 新时间重新判定授权（换授权/释放旧占用/占用新授权），当前自身占用参与释放后额度
- 共享池各释放/占用一次、净迁移为 0 且不超额
- 暂停/撤销/时间窗在新时间重新判定；无法核准时返回既有拒绝原因且排片与占用不变
- 同操作键同内容重放不重复迁移；异内容/跨场次冲突
- 后续取消/领码/核销按当前排片与当前授权处理
- 原核验决定、快照与单场/批量重放语义不变
- 并发改签不超额、不重复释放
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import service
from app.service import Store
from app.timeutil import to_iso

FUTURE = "2030-06-01T20:00:00Z"
FUTURE_2 = "2030-06-02T20:00:00Z"
FUTURE_3 = "2030-06-03T20:00:00Z"
WINDOW = {
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2031-01-01T00:00:00Z",
}

# fixed clock helpers (2026-06-01 19:30 UTC) so reschedule/redemption windows
# do not depend on the wall clock
CLOCK_BASE = datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc)


def _iso_at(**delta) -> str:
    from datetime import timedelta
    return to_iso(CLOCK_BASE + timedelta(**delta))


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


def reschedule(client, key, new_start, op_key, reason=None):
    body = {"new_start_time": new_start, "operation_key": op_key}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"/verifications/{key}/reschedule", json=body)


# ----------------------------------------------------------------------
# happy path: migrate to a different license at the new time
# ----------------------------------------------------------------------
def test_reschedule_moves_to_license_covering_new_time(client):
    # early license expires 2031-06-01, later license reaches 2031-12-01
    early = make_license(client, valid_to="2031-06-01T00:00:00Z", max_sessions=1)
    late = make_license(client, valid_to="2031-12-01T00:00:00Z", max_sessions=5)
    approved = verify(client, "rs-1").json()
    assert approved["license_id"] == early["id"]
    assert approved["remaining_quota"] == 0

    resp = reschedule(client, "rs-1", "2031-07-01T20:00:00Z", "op-1", "顺延")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["decision"] == "approved"
    assert data["replay"] is False
    assert data["from_start_time"].startswith("2030-06-01")
    assert data["to_start_time"].startswith("2031-07-01")
    assert data["from_license_id"] == early["id"]
    assert data["to_license_id"] == late["id"]
    assert data["from_pool_id"] is None and data["to_pool_id"] is None
    assert data["remaining_quota"] == 4
    assert data["reason"] == "顺延"

    # old license released once, new license consumed once
    assert client.get(f"/licenses/{early['id']}").json()["used_sessions"] == 0
    assert client.get(f"/licenses/{late['id']}").json()["used_sessions"] == 1


def test_current_schedule_query_reflects_reschedule_and_keeps_original(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1")
    reschedule(client, "rs-1", FUTURE_2, "op-1")

    cur = client.get("/verifications/rs-1").json()
    # top-level = current schedule
    assert cur["start_time"].startswith("2030-06-02")
    assert cur["rescheduled"] is True
    assert cur["reschedule_count"] == 1
    assert cur["last_reschedule"]["operation_key"] == "op-1"
    # original immutable snapshot preserved
    assert cur["original_start_time"].startswith("2030-06-01")
    assert cur["decision"] == "approved"
    assert cur["cancelled"] is False


def test_reschedule_to_same_license_is_net_zero(client):
    lic = make_license(client, max_sessions=3)
    verify(client, "rs-1", start=FUTURE)
    data = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    assert data["decision"] == "approved"
    assert data["from_license_id"] == data["to_license_id"] == lic["id"]
    # released once then consumed once -> still exactly one usage
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_self_occupancy_is_released_before_redecision(client):
    """License full with this screening; the move frees its own unit first,
    so approving the same license at the new slot must still succeed."""
    lic = make_license(client, max_sessions=1)
    approved = verify(client, "rs-1").json()
    assert approved["remaining_quota"] == 0
    data = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    assert data["decision"] == "approved"
    assert data["remaining_quota"] == 0
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_chain_of_reschedules_uses_most_recent_as_current(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    reschedule(client, "rs-1", FUTURE_2, "op-1")
    third = reschedule(client, "rs-1", FUTURE_3, "op-2").json()
    assert third["from_start_time"].startswith("2030-06-02")
    cur = client.get("/verifications/rs-1").json()
    assert cur["start_time"].startswith("2030-06-03")
    assert cur["reschedule_count"] == 2
    assert len(client.get("/reschedules", params={"idempotency_key": "rs-1"}).json()) == 2


# ----------------------------------------------------------------------
# bound pool: release and consume each happen once
# ----------------------------------------------------------------------
def test_reschedule_migrates_pool_occupancy_net_zero(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    lic = make_license(client, max_sessions=5, pool_id=pool["id"])
    approved = verify(client, "rs-1").json()
    assert approved["pool_id"] == pool["id"]
    assert approved["pool_remaining_quota"] == 0

    # pool is already full; moving this screening frees its own pool unit
    # and re-consumes it -> must approve (net 0), pool still at cap
    data = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    assert data["decision"] == "approved"
    assert data["from_pool_id"] == data["to_pool_id"] == pool["id"]
    assert data["pool_remaining_quota"] == 0
    assert client.get(f"/pools/{pool['id']}").json()["used_sessions"] == 1
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_reschedule_when_new_slot_pool_blocked_is_rejected_unchanged(client):
    """New time falls under a license bound to a *different* pool of the same
    film that is already exhausted by another screening. This screening's own
    unit frees its (old) pool, which cannot help the new pool, so the move is
    rejected POOL_EXHAUSTED and the schedule/occupancy are untouched."""
    pool_a = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 5}
    ).json()["id"]
    pool_b = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()["id"]
    # license A (June, hall-a) bound to pool_a; license B (Aug, hall-a) pool_b
    lic_a = make_license(
        client, max_sessions=5, pool_id=pool_a,
        valid_from="2030-05-01T00:00:00Z", valid_to="2030-07-01T00:00:00Z",
    )
    lic_b = make_license(
        client, max_sessions=5, pool_id=pool_b,
        valid_from="2030-07-01T00:00:00Z", valid_to="2030-12-01T00:00:00Z",
    )
    # another venue consumes the single pool_b unit for good
    make_license(
        client, venue_id="hall-other", max_sessions=5, pool_id=pool_b,
        valid_from="2030-01-01T00:00:00Z", valid_to="2031-12-01T00:00:00Z",
    )
    other = verify(
        client, "rs-other", start="2030-08-01T20:00:00Z", venue="hall-other"
    ).json()
    assert other["decision"] == "approved"
    assert other["pool_id"] == pool_b

    approved = verify(client, "rs-1", start=FUTURE).json()
    assert approved["pool_id"] == pool_a

    data = reschedule(client, "rs-1", "2030-08-01T20:00:00Z", "op-1").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "POOL_EXHAUSTED"

    # unchanged: still on A / pool_a at the original June slot
    cur = client.get("/verifications/rs-1").json()
    assert cur["start_time"].startswith("2030-06-01")
    assert cur["license_id"] == lic_a["id"]
    assert cur["pool_id"] == pool_a
    assert cur["rescheduled"] is False
    assert client.get(f"/licenses/{lic_a['id']}").json()["used_sessions"] == 1
    assert client.get(f"/licenses/{lic_b['id']}").json()["used_sessions"] == 0
    assert client.get(f"/pools/{pool_a}").json()["used_sessions"] == 1
    assert client.get(f"/pools/{pool_b}").json()["used_sessions"] == 1


# ----------------------------------------------------------------------
# re-judgement at new time: suspension / revocation / window
# ----------------------------------------------------------------------
def test_reschedule_into_suspension_is_rejected_and_unchanged(client):
    lic = make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2030-06-02T00:00:00Z",
            "suspend_to": "2030-06-03T00:00:00Z",
            "reason": "暂停",
        },
    )
    data = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "LICENSE_SUSPENDED"
    assert data["to_license_id"] is None
    assert data["remaining_quota"] is None

    # schedule and occupancy unchanged
    cur = client.get("/verifications/rs-1").json()
    assert cur["start_time"].startswith("2030-06-01")
    assert cur["rescheduled"] is False
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_reschedule_after_revocation_is_rejected_and_unchanged(client):
    lic = make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"})
    data = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "LICENSE_REVOKED"
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1
    cur = client.get("/verifications/rs-1").json()
    assert cur["start_time"].startswith("2030-06-01")


def test_reschedule_out_of_window_is_rejected_and_unchanged(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    data = reschedule(client, "rs-1", "2040-06-01T20:00:00Z", "op-1").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "TIME_OUT_OF_WINDOW"
    cur = client.get("/verifications/rs-1").json()
    assert cur["start_time"].startswith("2030-06-01")
    assert cur["rescheduled"] is False


def test_rejected_reschedule_leaves_trace_and_is_replayable(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    first = reschedule(client, "rs-1", "2040-06-01T20:00:00Z", "op-1").json()
    assert first["decision"] == "rejected"

    replay = reschedule(client, "rs-1", "2040-06-01T20:00:00Z", "op-1").json()
    assert replay["replay"] is True
    assert replay["id"] == first["id"]
    assert replay["decision"] == "rejected"
    # only one rejected trace
    rows = client.get(
        "/reschedules", params={"idempotency_key": "rs-1", "decision": "rejected"}
    ).json()
    assert len(rows) == 1


# ----------------------------------------------------------------------
# idempotency / conflicts of the reschedule operation itself
# ----------------------------------------------------------------------
def test_reschedule_replay_same_operation_key_does_not_migrate_again(client):
    lic = make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    first = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    second = reschedule(client, "rs-1", FUTURE_2, "op-1").json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["rescheduled_at"] == first["rescheduled_at"]
    assert client.get("/verifications/rs-1").json()["reschedule_count"] == 1
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_reschedule_same_operation_key_different_content_conflicts(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    reschedule(client, "rs-1", FUTURE_2, "op-1")
    resp = reschedule(client, "rs-1", FUTURE_3, "op-1")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["existing"]["operation_key"] == "op-1"
    # schedule did not move to FUTURE_3
    cur = client.get("/verifications/rs-1").json()
    assert cur["start_time"].startswith("2030-06-02")


def test_reschedule_operation_key_reused_for_another_verification_conflicts(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    verify(client, "rs-2", start=FUTURE)
    reschedule(client, "rs-1", FUTURE_2, "shared-op")
    resp = reschedule(client, "rs-2", FUTURE_2, "shared-op")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"
    # rs-2 untouched
    assert client.get("/verifications/rs-2").json()["rescheduled"] is False


# ----------------------------------------------------------------------
# eligibility gates
# ----------------------------------------------------------------------
def test_reschedule_unknown_key_is_404(client):
    resp = reschedule(client, "no-such", FUTURE_2, "op-1")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_reschedule_rejected_verification_is_422(client):
    verify(client, "rs-rej", film="unknown", venue="nowhere")
    resp = reschedule(client, "rs-rej", FUTURE_2, "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"


def test_reschedule_cancelled_session_is_422(client):
    make_license(client)
    verify(client, "rs-1", start=FUTURE)
    client.post("/verifications/rs-1/cancel", json={"reason": "取消"})
    resp = reschedule(client, "rs-1", FUTURE_2, "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_CANCELLED"


def test_reschedule_session_with_opening_code_is_422(client):
    make_license(client)
    verify(client, "rs-1", start=FUTURE)
    claim = client.post("/verifications/rs-1/opening-code")
    assert claim.status_code == 200
    resp = reschedule(client, "rs-1", FUTURE_2, "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "OPENING_CODE_ALREADY_ISSUED"


def test_reschedule_new_time_must_be_in_future(client):
    make_license(client)
    verify(client, "rs-1", start=FUTURE)
    resp = reschedule(client, "rs-1", "2020-01-01T00:00:00Z", "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "NEW_START_NOT_IN_FUTURE"


def test_reschedule_new_time_must_differ_from_current(client):
    make_license(client)
    verify(client, "rs-1", start=FUTURE)
    same = reschedule(client, "rs-1", FUTURE, "op-1")
    assert same.status_code == 422
    assert same.json()["code"] == "NEW_START_UNCHANGED"

    reschedule(client, "rs-1", FUTURE_2, "op-1")
    # after one move, repeating the *current* time is also unchanged
    same_again = reschedule(client, "rs-1", FUTURE_2, "op-2")
    assert same_again.status_code == 422
    assert same_again.json()["code"] == "NEW_START_UNCHANGED"


def test_reschedule_started_session_is_422(client):
    make_license(client)
    verify(client, "rs-past", start="2020-06-01T20:00:00Z")
    resp = reschedule(client, "rs-past", FUTURE_2, "op-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"


def test_reschedule_validation_errors_leave_no_trace(client):
    make_license(client)
    verify(client, "rs-1", start=FUTURE)
    assert reschedule(client, "rs-1", "2020-01-01T00:00:00Z", "op-1").status_code == 422
    assert reschedule(client, "rs-1", FUTURE, "op-2").status_code == 422
    assert client.get("/reschedules?idempotency_key=rs-1").json() == []
    # missing field -> 422 schema
    assert client.post(
        "/verifications/rs-1/reschedule", json={"operation_key": "op-3"}
    ).status_code == 422
    assert client.get("/verifications/rs-1").json()["rescheduled"] is False


# ----------------------------------------------------------------------
# downstream operations follow the current schedule & authorisation
# ----------------------------------------------------------------------
def test_cancel_after_reschedule_releases_current_license(client):
    early = make_license(client, valid_to="2031-06-01T00:00:00Z", max_sessions=1)
    late = make_license(client, valid_to="2031-12-01T00:00:00Z", max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    reschedule(client, "rs-1", "2031-07-01T20:00:00Z", "op-1")

    can = client.post("/verifications/rs-1/cancel", json={"reason": "取消"}).json()
    assert can["license_id"] == late["id"]
    assert client.get(f"/licenses/{late['id']}").json()["used_sessions"] == 0
    # early had already been released by the move; nothing double-released
    assert client.get(f"/licenses/{early['id']}").json()["used_sessions"] == 0

    cur = client.get("/verifications/rs-1").json()
    assert cur["cancelled"] is True
    assert cur["rescheduled"] is True


def test_opening_code_window_uses_rescheduled_time(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    reschedule(client, "rs-1", FUTURE_2, "op-1")
    code = client.post("/verifications/rs-1/opening-code").json()["code"]
    # redeeming now (2026) is far earlier than the (rescheduled) 2030 slot
    resp = client.post("/redemptions", json={"code": code, "venue_id": "hall-a"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "REDEMPTION_TOO_EARLY"


def test_claim_and_redeem_after_reschedule_follow_new_schedule(client, monkeypatch):
    """Reschedule 20:00 -> 21:30, claim, then walk the clock. The 30-minute
    redemption window and the receipt's start_time follow the NEW schedule."""
    make_license(client, max_sessions=5)
    # freeze clock well before both slots
    monkeypatch.setattr(service, "now_iso", lambda: _iso_at(hours=-3))
    verify(client, "rs-1", start=_iso_at(minutes=30))   # 20:00
    reschedule(client, "rs-1", _iso_at(hours=2), "op-1")  # 21:30

    # claiming right at the OLD start still works (new slot is later)
    monkeypatch.setattr(service, "now_iso", lambda: _iso_at(minutes=30))
    code = client.post("/verifications/rs-1/opening-code").json()["code"]

    # 21:00 — before new start -> too early even though old start has passed
    monkeypatch.setattr(service, "now_iso", lambda: _iso_at(hours=1, minutes=30))
    early = client.post("/redemptions", json={"code": code, "venue_id": "hall-a"})
    assert early.status_code == 422
    assert early.json()["code"] == "REDEMPTION_TOO_EARLY"

    # 21:30 — new planned start -> redeem succeeds, receipt carries new time
    monkeypatch.setattr(service, "now_iso", lambda: _iso_at(hours=2))
    receipt = client.post(
        "/redemptions", json={"code": code, "venue_id": "hall-a"}
    ).json()
    assert receipt["start_time"] == _iso_at(hours=2)

    # status receipt also reflects the rescheduled start
    st = client.get("/verifications/rs-1/opening-code").json()
    assert st["status"] == "redeemed"
    assert st["redemption"]["start_time"] == _iso_at(hours=2)

    # cannot reschedule a redeemed session any more (code already issued gate)
    again = reschedule(client, "rs-1", _iso_at(days=2), "op-2")
    assert again.status_code == 422
    assert again.json()["code"] == "OPENING_CODE_ALREADY_ISSUED"


def test_reschedule_history_endpoints(client):
    make_license(client, max_sessions=5)
    verify(client, "rs-1", start=FUTURE)
    rec = reschedule(client, "rs-1", FUTURE_2, "op-1").json()

    by_id = client.get(f"/reschedules/{rec['id']}").json()
    assert by_id["operation_key"] == "op-1"
    by_op = client.get("/reschedules", params={"operation_key": "op-1"}).json()
    assert len(by_op) == 1 and by_op[0]["id"] == rec["id"]
    assert client.get("/reschedules/99999").status_code == 404


# ----------------------------------------------------------------------
# original verification semantics unchanged
# ----------------------------------------------------------------------
def test_original_verification_replay_and_batch_semantics_survive(client):
    make_license(client, max_sessions=5)
    original = verify(client, "rs-1", start=FUTURE).json()
    reschedule(client, "rs-1", FUTURE_2, "op-1")

    # single replay keeps the first (original) response: no current fields
    replay = verify(client, "rs-1", start=FUTURE).json()
    assert replay["replay"] is True
    assert replay["start_time"].startswith("2030-06-01")
    assert replay["license_id"] == original["license_id"]
    assert "rescheduled" not in replay

    # batch replay uses the same legacy shape
    batch = client.post(
        "/verifications/batch",
        json={"items": [{
            "film_id": "film-1", "venue_id": "hall-a",
            "start_time": FUTURE, "idempotency_key": "rs-1",
        }]},
    ).json()
    item = batch["results"][0]
    assert item["replay"] is True
    assert item["start_time"].startswith("2030-06-01")
    assert "rescheduled" not in item


# ----------------------------------------------------------------------
# concurrency: never over-quota, never double release
# ----------------------------------------------------------------------
def test_concurrent_reschedules_competing_for_one_unit(tmp_path):
    """All screenings start on the same film+venue under many wide licenses,
    then move to a new slot where only one license (cap 1) applies: exactly
    one wins, the rest get QUOTA_EXHAUSTED and keep their original slot."""
    store = Store(str(tmp_path / "rs-race.db"))
    n = 8
    # many licenses valid everywhere (used at the June slot, valid_to late)
    for i in range(n):
        store.create_license(
            film_id="film-x", venue_id="hall-1",
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2030, 7, 1, tzinfo=timezone.utc),
            max_sessions=1,
        )
    # single narrow license: the ONLY one covering the August slot, cap 1
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2030, 7, 1, tzinfo=timezone.utc),
        valid_to=datetime(2030, 9, 1, tzinfo=timezone.utc),
        max_sessions=1,
    )
    for i in range(n):
        store.verify(
            film_id="film-x", venue_id="hall-1",
            start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
            idempotency_key=f"s-{i}",
        )

    results, errors = [], []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(
                store.reschedule_verification(
                    f"s-{i}",
                    new_start_time=datetime(2030, 8, 1, 20, 0, tzinfo=timezone.utc),
                    operation_key=f"op-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 1
    assert len(rejected) == n - 1
    assert all(r["reject_reason"] == "QUOTA_EXHAUSTED" for r in rejected)

    # narrow license holds exactly the one winner
    narrow = store.list_licenses(venue_id="hall-1")[-1]
    assert narrow["used_sessions"] == 1

    # exactly one current schedule moved to August; losers kept June
    current = store.list_verifications(limit=1000)
    moved = [v for v in current if v["start_time"].startswith("2030-08-01")]
    stayed = [v for v in current if v["start_time"].startswith("2030-06-01")]
    assert len(moved) == 1
    assert len(stayed) == n - 1
    # total live approvals unchanged (release+consume, net 0 overall)
    assert len([v for v in current if v["decision"] == "approved"
                and not v["cancelled"]]) == n


def test_concurrent_duplicate_reschedule_operation_migrates_once(tmp_path):
    store = Store(str(tmp_path / "dup.db"))
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    store.verify(
        film_id="film-x", venue_id="hall-1",
        start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
        idempotency_key="s-1",
    )
    results, errors = [], []

    def worker() -> None:
        try:
            results.append(
                store.reschedule_verification(
                    "s-1",
                    new_start_time=datetime(2030, 6, 2, 20, 0, tzinfo=timezone.utc),
                    operation_key="op-dup",
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
    assert store.get_verification("s-1")["reschedule_count"] == 1
