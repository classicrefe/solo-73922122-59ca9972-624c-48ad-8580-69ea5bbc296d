"""并发报废/确认的确定性测试。

用门闩把 scrap 的择纸计算卡在锁外，再从另一个执行流插入查单/确认，
验证：
- 重排计算期间查单、确认不被阻塞；
- 查单只显示已生效状态（报废意向提交前不可见）；
- 拟报废印张若先被有效派工确认完工，整批 409、不报废、不改派；
- 先完工的是其他印张时留在原位，并按最新状态为剩余位置重排；
- 并发报废按各自生效时状态判断，结果可串行化；
- 他单操作不被本单重排阻塞。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import planner, service
from app.main import app

PAGES = ["COVER", "P2", "P3", "P4", "P5"]  # 2 个位置
SHEETS = [
    {"id": "S1", "front": {"left": None, "right": "COVER"}, "back": None},
    {"id": "S2", "front": None, "back": {"left": "P4", "right": "P5"}},
    {"id": "S3", "front": {"left": "OLD", "right": "STUFF"}, "back": None},
]


def create_order(client, pages=PAGES, sheets=SHEETS):
    r = client.post("/work-orders", json={"pages": pages, "old_sheets": sheets})
    assert r.status_code == 201, r.text
    return r.json()


def install_gate(monkeypatch):
    """把 planner 包成可门控的慢计算；返回 (entered, release, calls)。"""
    entered = threading.Event()
    release = threading.Event()
    calls: list[float] = []
    orig = planner.plan_positions

    def gated(*args, **kwargs):
        calls.append(time.monotonic())
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test gate timed out")
        return orig(*args, **kwargs)

    monkeypatch.setattr(service.planner, "plan_positions", gated)
    return entered, release, calls


def start_scrap(oid, sheet_ids):
    """在独立线程里直接调 service 层 scrap，捕获返回或 ApiError。"""
    box: dict = {}

    def target():
        try:
            box["value"] = service.scrap_sheets(oid, sheet_ids)
        except service.ApiError as exc:  # noqa: BLE001 - 测试需要拿到业务异常
            box["error"] = exc
        except Exception as exc:  # noqa: BLE001
            box["unexpected"] = exc

    t = threading.Thread(target=target)
    t.start()
    return t, box


def join(t, box):
    t.join(timeout=10)
    assert not t.is_alive(), "scrap 线程超时未结束"
    assert "unexpected" not in box, box["unexpected"]
    return box


def test_get_during_scrap_shows_only_effective_state(client, monkeypatch):
    st = create_order(client)
    oid = st["work_order_id"]
    entered, release, _ = install_gate(monkeypatch)

    t, box = start_scrap(oid, ["S2"])
    assert entered.wait(5)

    # 计算进行中：GET 立即返回，且只看到上一个已生效状态（无报废痕迹）
    t0 = time.monotonic()
    r = client.get(f"/work-orders/{oid}")
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert r.status_code == 200
    mid = r.json()
    assert mid["scrapped_sheets"] == []
    assert mid["positions"][1]["use"] == "S2"

    release.set()
    out = join(t, box)
    assert "value" in out
    assert out["value"]["scrapped_sheets"] == ["S2"]


def test_batch_sheet_completed_first_makes_whole_batch_409(client, monkeypatch):
    st = create_order(client)
    oid = st["work_order_id"]
    d1 = st["positions"][1]["dispatch_id"]

    entered, release, _ = install_gate(monkeypatch)
    # 整批两张：只有 S2 会在计算期间被确认完工
    t, box = start_scrap(oid, ["S1", "S2"])
    assert entered.wait(5)

    # 查单仍显示未报废、未改派
    mid = client.get(f"/work-orders/{oid}").json()
    assert mid["scrapped_sheets"] == []
    assert [p["use"] for p in mid["positions"]] == ["S1", "S2"]

    # 拟报废的 S2 位置先凭有效派工号确认完工
    r = client.post(f"/work-orders/{oid}/positions/1/confirm", json={"dispatch_id": d1})
    assert r.status_code == 200, r.text

    release.set()
    out = join(t, box)

    # 整批 409
    assert "error" in out, out
    err = out["error"]
    assert err.status_code == 409
    assert "完工" in err.detail

    # 不登记报废、也不改派：两张都不在 scraps，位置/派工维持原样
    final = client.get(f"/work-orders/{oid}").json()
    assert final["scrapped_sheets"] == []
    assert [p["use"] for p in final["positions"]] == ["S1", "S2"]
    p1 = final["positions"][1]
    assert p1["status"] == "completed"
    assert p1["dispatch_id"] == d1  # 仍是原有效派工
    p0 = final["positions"][0]
    assert p0["status"] == "pending"
    assert p0["dispatch_id"] == st["positions"][0]["dispatch_id"]

    c = service.db.conn()
    assert c.execute("SELECT COUNT(*) n FROM scraps WHERE work_order_id=?", (oid,)).fetchone()["n"] == 0
    disp = c.execute("SELECT state FROM dispatches WHERE id=?", (d1,)).fetchone()
    assert disp["state"] == "confirmed"


def test_other_position_completed_first_kept_and_replans_rest(client, monkeypatch):
    st = create_order(client)
    oid = st["work_order_id"]
    d0 = st["positions"][0]["dispatch_id"]
    d1_old = st["positions"][1]["dispatch_id"]

    entered, release, calls = install_gate(monkeypatch)
    t, box = start_scrap(oid, ["S2"])
    assert entered.wait(5)

    # 先完工的是「其他印张」S1（位置 0）
    r = client.post(f"/work-orders/{oid}/positions/0/confirm", json={"dispatch_id": d0})
    assert r.status_code == 200, r.text

    release.set()
    out = join(t, box)
    assert "value" in out, out

    # 状态在计算期间变化 -> 按最新状态重新择纸一次
    assert len(calls) == 2

    body = out["value"]
    assert body["scrapped_sheets"] == ["S2"]
    assert body["returned_dispatches"] == [
        {"position": 1, "returned_dispatch_id": d1_old, "old": "S2", "new": "NEW"}
    ]
    p0, p1 = body["positions"]
    assert p0["use"] == "S1"
    assert p0["status"] == "completed"
    assert p0["dispatch_id"] == d0
    assert p1["use"] == "NEW"
    assert p1["status"] == "pending"
    assert p1["dispatch_id"] != d1_old

    # 完工位置的原派工仍可幂等确认；被退回派工再确认 409；新派工可确认
    assert client.post(f"/work-orders/{oid}/positions/0/confirm", json={"dispatch_id": d0}).status_code == 200
    assert client.post(f"/work-orders/{oid}/positions/1/confirm", json={"dispatch_id": d1_old}).status_code == 409
    d1_new = p1["dispatch_id"]
    assert client.post(f"/work-orders/{oid}/positions/1/confirm", json={"dispatch_id": d1_new}).status_code == 200


def test_concurrent_identical_scraps_exactly_one_wins(client):
    st = create_order(client)
    oid = st["work_order_id"]

    n = 8
    barrier = threading.Barrier(n)
    results: list = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        try:
            value = service.scrap_sheets(oid, ["S2"])
            with lock:
                results.append(("ok", value))
        except service.ApiError as exc:
            with lock:
                results.append((exc.status_code, exc.detail))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == 409]
    assert len(oks) == 1, results
    assert len(conflicts) == n - 1, results

    final = client.get(f"/work-orders/{oid}").json()
    assert final["scrapped_sheets"] == ["S2"]
    c = service.db.conn()
    rows = c.execute("SELECT * FROM scraps WHERE work_order_id=?", (oid,)).fetchall()
    assert len(rows) == 1


def test_concurrent_disjoint_scraps_both_commit(client):
    st = create_order(client)
    oid = st["work_order_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f2 = pool.submit(service.scrap_sheets, oid, ["S2"])
        f3 = pool.submit(service.scrap_sheets, oid, ["S3"])
        b2 = f2.result(timeout=10)
        b3 = f3.result(timeout=10)

    # 各自响应只保证包含本批（响应反映各自生效时的状态，可能看不到对方）
    assert "S2" in b2["scrapped_sheets"]
    assert "S3" in b3["scrapped_sheets"]
    final = client.get(f"/work-orders/{oid}").json()
    assert final["scrapped_sheets"] == ["S2", "S3"]


def test_other_order_queries_and_confirm_not_blocked(client, monkeypatch):
    a = create_order(client)
    b = create_order(client)
    aid, bid = a["work_order_id"], b["work_order_id"]
    bd0 = b["positions"][0]["dispatch_id"]

    entered, release, _ = install_gate(monkeypatch)
    t, box = start_scrap(aid, ["S2"])
    assert entered.wait(5)

    t0 = time.monotonic()
    assert client.get(f"/work-orders/{bid}").status_code == 200
    assert client.get("/work-orders").status_code == 200
    r = client.post(f"/work-orders/{bid}/positions/0/confirm", json={"dispatch_id": bd0})
    assert r.status_code == 200
    assert time.monotonic() - t0 < 1.0

    release.set()
    out = join(t, box)
    assert "value" in out
    assert client.get(f"/work-orders/{bid}").json()["positions"][0]["status"] == "completed"


def test_http_scrap_and_confirm_interleave_via_api(client, monkeypatch):
    """完整 HTTP 栈：scrap 请求在后台跑，期间 HTTP 确认能先提交并令 scrap 409。"""
    st = create_order(client)
    oid = st["work_order_id"]
    d1 = st["positions"][1]["dispatch_id"]
    entered, release, _ = install_gate(monkeypatch)

    http = TestClient(app)
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            http.post, f"/work-orders/{oid}/scrap", json={"sheet_ids": ["S2"]}
        )
        assert entered.wait(5)
        mid = client.get(f"/work-orders/{oid}")
        assert mid.status_code == 200
        assert mid.json()["scrapped_sheets"] == []
        conf = client.post(f"/work-orders/{oid}/positions/1/confirm", json={"dispatch_id": d1})
        assert conf.status_code == 200
        release.set()
        resp = fut.result(timeout=10)

    assert resp.status_code == 409, resp.text
    final = client.get(f"/work-orders/{oid}").json()
    assert final["scrapped_sheets"] == []
    assert final["positions"][1]["status"] == "completed"
    assert final["positions"][1]["dispatch_id"] == d1
