"""集中批量核验（POST /verifications/batch）端到端测试。

覆盖：顺序处理与前项额度影响、业务拒绝留流水并继续、同批/历史幂等重放、
同键不同内容整批失败回滚、格式错误不落库、处理异常整批回滚、与单场核验/
取消并发不穿插超额，以及单场接口保持可用。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

START = "2026-06-01T20:00:00Z"
FUTURE = "2030-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2031-01-01T00:00:00Z",
}


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
        "max_sessions": 100,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_pool(client, film="film-1", cap=10):
    resp = client.post("/pools", json={"film_id": film, "max_sessions": cap})
    assert resp.status_code == 201, resp.text
    return resp.json()


def batch(client, items):
    return client.post("/verifications/batch", json={"items": items})


def item(key, start=START, film="film-1", venue="hall-a"):
    return {
        "film_id": film,
        "venue_id": venue,
        "start_time": start,
        "idempotency_key": key,
    }


# ----------------------------------------------------------------------
# 基本顺序处理 / index / 前项额度影响后项
# ----------------------------------------------------------------------
def test_batch_verifies_in_order_with_index(client):
    lic = make_license(client, max_sessions=5)
    resp = batch(client, [item("b-1"), item("b-2"), item("b-3")])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    keys = ["b-1", "b-2", "b-3"]
    for i, row in enumerate(body["results"]):
        assert row["index"] == i
        assert row["idempotency_key"] == keys[i]
        assert row["decision"] == "approved"
        assert row["license_id"] == lic["id"]
        assert row["replay"] is False
        # 快照严格按顺序递减：前项消耗影响后项
        assert row["remaining_quota"] == 4 - i

    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 3
    assert lic_after["remaining_sessions"] == 2


def test_batch_prefers_earliest_expiring_license_across_items(client):
    late = make_license(client, valid_to="2030-12-01T00:00:00Z", max_sessions=5)
    early = make_license(client, valid_to="2026-09-01T00:00:00Z", max_sessions=1)

    resp = batch(client, [item("e-1"), item("e-2")])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows[0]["license_id"] == early["id"]
    assert rows[0]["remaining_quota"] == 0
    # 早到期授权被第 0 项耗尽，第 1 项回退到兜底授权
    assert rows[1]["license_id"] == late["id"]
    assert rows[1]["remaining_quota"] == 4


def test_batch_shared_pool_consumed_in_order_then_exhausted(client):
    pool = create_pool(client, cap=2)
    make_license(client, pool_id=pool["id"], max_sessions=10)

    resp = batch(client, [item("p-1"), item("p-2"), item("p-3")])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert [r["decision"] for r in rows] == ["approved", "approved", "rejected"]
    assert rows[0]["pool_id"] == pool["id"]
    assert rows[0]["pool_remaining_quota"] == 1
    assert rows[1]["pool_remaining_quota"] == 0
    assert rows[2]["reject_reason"] == "POOL_EXHAUSTED"
    assert rows[2]["license_id"] is None

    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 2


# ----------------------------------------------------------------------
# 业务拒绝也落可追溯流水，并继续处理
# ----------------------------------------------------------------------
def test_batch_business_rejection_leaves_trace_and_continues(client):
    make_license(client, max_sessions=2)
    resp = batch(client, [
        item("r-1"),
        item("r-2"),
        item("r-no-license", film="nope", venue="nowhere"),
        item("r-3"),
    ])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert [r["decision"] for r in rows] == [
        "approved", "approved", "rejected", "rejected"
    ]
    assert rows[2]["reject_reason"] == "NO_LICENSE"
    # 第 1、2 项耗尽授权后第 4 项按 QUOTA_EXHAUSTED 拒绝，处理未中断
    assert rows[3]["reject_reason"] == "QUOTA_EXHAUSTED"

    # 每项（含拒绝）都有可追溯流水
    listed = client.get("/verifications").json()
    assert {row["idempotency_key"] for row in listed} == {
        "r-1", "r-2", "r-no-license", "r-3"
    }
    rejected_row = client.get("/verifications/r-no-license").json()
    assert rejected_row["decision"] == "rejected"
    assert rejected_row["reject_reason"] == "NO_LICENSE"


# ----------------------------------------------------------------------
# 幂等：同批 / 历史 同键同内容返回原流水，不重复扣额
# ----------------------------------------------------------------------
def test_batch_same_key_same_content_within_batch_replays(client):
    make_license(client, max_sessions=5)
    resp = batch(client, [item("dup-1"), item("dup-1"), item("dup-2")])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows[0]["replay"] is False
    assert rows[1]["replay"] is True
    assert rows[1]["id"] == rows[0]["id"]
    assert rows[1]["remaining_quota"] == rows[0]["remaining_quota"]
    assert rows[2]["replay"] is False
    # 同键两项只扣一次
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 2


def test_batch_replays_historical_verification_without_double_consume(client):
    make_license(client, max_sessions=5)
    first = client.post("/verifications", json=item("hist-1")).json()
    assert first["replay"] is False

    resp = batch(client, [item("hist-1"), item("hist-2")])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows[0]["replay"] is True
    assert rows[0]["id"] == first["id"]
    assert rows[1]["replay"] is False

    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 2


def test_batch_replays_historical_rejection(client):
    make_license(client, max_sessions=1)
    client.post("/verifications", json=item("only-1"))
    rejected = client.post("/verifications", json=item("only-2")).json()
    assert rejected["decision"] == "rejected"

    resp = batch(client, [item("only-2"), item("only-1")])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows[0]["replay"] is True
    assert rows[0]["decision"] == "rejected"
    assert rows[0]["reject_reason"] == "QUOTA_EXHAUSTED"
    assert rows[1]["replay"] is True
    assert rows[1]["decision"] == "approved"
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 1


# ----------------------------------------------------------------------
# 同键不同内容：整批失败、409 带 index/existing、无新流水无额度占用
# ----------------------------------------------------------------------
def test_batch_same_key_different_content_within_batch_fails_atomically(client):
    lic = make_license(client, max_sessions=5)
    resp = batch(client, [
        item("k-same", start="2026-06-01T20:00:00Z"),
        item("k-same", start="2026-06-02T20:00:00Z"),
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["index"] == 1
    assert body["existing"]["idempotency_key"] == "k-same"

    # 整批回滚：没有任何新流水，额度未占用
    assert client.get("/verifications").json() == []
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 0


def test_batch_conflict_with_history_rolls_back_whole_batch(client):
    lic = make_license(client, max_sessions=5)
    client.post("/verifications", json=item("hist-x", start=START))

    resp = batch(client, [
        item("brand-new-1"),  # 若无回滚本会消耗一次额度
        item("hist-x", start="2026-06-02T20:00:00Z"),  # 与历史内容冲突
        item("brand-new-2"),
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["index"] == 1
    assert body["existing"]["start_time"].startswith("2026-06-01")

    keys = {row["idempotency_key"] for row in client.get("/verifications").json()}
    assert keys == {"hist-x"}  # 批内新键全部未落库
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 1  # 仅历史那一条


def test_batch_conflict_keeps_failed_batch_replayable(client):
    make_license(client, max_sessions=5)
    # 同键不同内容 -> 整批失败
    bad = [
        item("shared", start="2026-06-01T20:00:00Z"),
        item("shared", start="2026-06-02T20:00:00Z"),
    ]
    assert batch(client, bad).status_code == 409

    # 回滚后用一致内容重新提交整批可以成功
    good = [
        item("shared", start="2026-06-01T20:00:00Z"),
        item("other", start="2026-06-03T20:00:00Z"),
    ]
    resp = batch(client, good)
    assert resp.status_code == 200
    assert [r["decision"] for r in resp.json()["results"]] == ["approved", "approved"]


# ----------------------------------------------------------------------
# 格式错误：422 且不落任何流水
# ----------------------------------------------------------------------
def test_batch_empty_items_is_422(client):
    make_license(client)
    resp = batch(client, [])
    assert resp.status_code == 422
    assert client.get("/verifications").json() == []


def test_batch_too_many_items_is_422(client):
    make_license(client)
    resp = batch(client, [item(f"big-{i}") for i in range(101)])
    assert resp.status_code == 422
    assert client.get("/verifications").json() == []


def test_batch_malformed_item_is_422_and_leaves_nothing(client):
    make_license(client)
    resp = batch(client, [
        item("ok-before"),  # 即使本可成功，也因同批格式错误而整体拒绝
        {"film_id": "film-1", "venue_id": "hall-a",
         "start_time": "not-a-time", "idempotency_key": "bad-time"},
    ])
    assert resp.status_code == 422
    assert client.get("/verifications").json() == []


def test_batch_missing_field_is_422(client):
    resp = batch(client, [{"film_id": "film-1", "venue_id": "hall-a"}])
    assert resp.status_code == 422


def test_batch_items_must_be_array(client):
    resp = client.post("/verifications/batch", json={"items": "nope"})
    assert resp.status_code == 422


def test_batch_accepts_naive_datetime_as_utc(client):
    make_license(client)
    resp = batch(client, [
        {"film_id": "film-1", "venue_id": "hall-a",
         "start_time": "2026-06-01T20:00:00", "idempotency_key": "naive-1"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["decision"] == "approved"


# ----------------------------------------------------------------------
# 处理异常：整批回滚（无新流水、无额度占用）
# ----------------------------------------------------------------------
def test_batch_processing_exception_rolls_back_everything(client, monkeypatch):
    make_license(client, max_sessions=5)
    import app.service as svc

    real_decide = svc._decide
    calls = {"n": 0}

    def flaky(candidates, start_iso, suspended_ids=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom mid-batch")
        return real_decide(candidates, start_iso, suspended_ids)

    monkeypatch.setattr(svc, "_decide", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        client.app.state.store.verify_batch(
            [
                {
                    "film_id": "film-1",
                    "venue_id": "hall-a",
                    "start_time": datetime(2026, 6, 1, 20, tzinfo=timezone.utc),
                    "idempotency_key": f"x-{i}",
                }
                for i in range(3)
            ]
        )

    # 第 0 项已 INSERT 但随事务整体回滚
    assert client.get("/verifications").json() == []
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 0


# ----------------------------------------------------------------------
# 与单场核验并发：不穿插超额
# ----------------------------------------------------------------------
def test_batch_concurrent_with_single_verifications_never_oversells(tmp_path):
    cap = 4
    store = Store(str(tmp_path / "concurrent.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=cap,
    )

    results, errors = [], []
    lock = threading.Lock()

    def do_verify(key):
        try:
            out = store.verify(
                film_id="film-x",
                venue_id="hall-1",
                start_time=datetime(2026, 6, 1, 20, tzinfo=timezone.utc),
                idempotency_key=key,
            )
            with lock:
                results.append(out)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    def do_batch(prefix):
        try:
            out = store.verify_batch(
                [
                    {
                        "film_id": "film-x",
                        "venue_id": "hall-1",
                        "start_time": datetime(2026, 6, 1, 20,
                                               tzinfo=timezone.utc),
                        "idempotency_key": f"{prefix}-{i}",
                    }
                    for i in range(10)
                ]
            )
            with lock:
                results.extend(out)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=do_verify, args=(f"single-{i}",))
        for i in range(10)
    ]
    threads += [
        threading.Thread(target=do_batch, args=(f"batch-{b}",))
        for b in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    assert len(approved) == cap
    lic = store.list_licenses()[0]
    assert lic["used_sessions"] == cap
    assert lic["remaining_sessions"] == 0


def test_two_concurrent_batches_never_oversell(tmp_path):
    cap = 3
    store = Store(str(tmp_path / "concurrent.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=cap,
    )
    outcomes = []
    errors = []

    def do_batch(prefix):
        try:
            outcomes.append(
                store.verify_batch(
                    [
                        {
                            "film_id": "film-x",
                            "venue_id": "hall-1",
                            "start_time": datetime(2026, 6, 1, 20,
                                                   tzinfo=timezone.utc),
                            "idempotency_key": f"{prefix}-{i}",
                        }
                        for i in range(6)
                    ]
                )
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=do_batch, args=(p,))
               for p in ("b-a", "b-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [
        r for rows in outcomes for r in rows if r["decision"] == "approved"
    ]
    assert len(approved) == cap
    assert store.list_licenses()[0]["used_sessions"] == cap


# ----------------------------------------------------------------------
# 批次与取消并发：只能看到取消前或取消后的完整状态，不穿插超额
# ----------------------------------------------------------------------
def test_batch_concurrent_with_cancel_keeps_consistent_quota(tmp_path):
    for run in range(10):  # 反复跑以覆盖两种串行次序
        store = Store(str(tmp_path / f"cc-{run}.db"))
        store.create_license(
            film_id="film-x",
            venue_id="hall-1",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
            max_sessions=3,
        )
        # 预先占满 3 场（未来场次，可取消）
        for i in range(3):
            store.verify(
                film_id="film-x",
                venue_id="hall-1",
                start_time=datetime(2030, 6, 1 + i, 20, tzinfo=timezone.utc),
                idempotency_key=f"pre-{i}",
            )

        batch_rows: list = []
        barrier = threading.Barrier(2)

        def do_batch():
            barrier.wait()
            rows = store.verify_batch(
                [
                    {
                        "film_id": "film-x",
                        "venue_id": "hall-1",
                        "start_time": datetime(2026, 6, 1, i % 28 + 1,
                                               tzinfo=timezone.utc),
                        "idempotency_key": f"new-{i}",
                    }
                    for i in range(6)
                ]
            )
            batch_rows.extend(rows)

        def do_cancel():
            barrier.wait()
            store.cancel_verification("pre-0", reason="设备故障")

        t1 = threading.Thread(target=do_batch)
        t2 = threading.Thread(target=do_cancel)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        batch_approved = [r for r in batch_rows
                          if r["decision"] == "approved"]
        used = store.list_licenses()[0]["used_sessions"]
        # 两种整体串行次序必居其一：
        #   取消先到 -> 批次看到 used=2 -> 恰好 1 项进入 -> 最终 used=3；
        #   批次先到 -> 批次看到 used=3 -> 0 项进入 -> 取消后 used=2。
        # 绝不会穿插超额：批准数与最终占用严格一致。
        assert len(batch_approved) in (0, 1)
        assert used == 2 + len(batch_approved)
        assert used <= 3


# ----------------------------------------------------------------------
# 原单场接口继续可用，与批量结果语义一致
# ----------------------------------------------------------------------
def test_single_verification_endpoint_still_works(client):
    lic = make_license(client, max_sessions=3)
    single = client.post("/verifications", json=item("s-1"))
    assert single.status_code == 200
    assert single.json()["decision"] == "approved"
    assert "index" not in single.json()

    resp = batch(client, [item("s-1"), item("s-2")])
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows[0]["replay"] is True
    assert rows[0]["id"] == single.json()["id"]
    assert rows[1]["replay"] is False

    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 2
