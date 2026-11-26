"""业务逻辑：建单、查询、派工确认、报废重排。

并发模型（报废重排）
--------------------
旧实现整个报废流程都持有 ``db.lock()``，而择纸 DP 可能较慢，导致重排期间
同单/他单的查单与完工确认全部被阻塞。现在：

1. 锁内读取已生效状态的快照并做基础校验（短临界区）；
2. 锁外执行 ``planner`` 择纸计算——期间查单/确认可正常完成；
3. 再次进锁，按「生效时」的最新状态复核：
   - 本批印张若已凭有效派工确认完工（或已报废），整批返回 409，
     不登记报废、不改派；
   - 若状态发生其他变化（例如别的位置先完工），放弃旧方案，按最新
     状态重新择纸（乐观重试）；
4. 复核通过且快照未变后，报废登记与 positions/dispatches 变动在同一个
   SQLite 事务里提交，一并生效。

同一工单的报废在工单级锁内串行（不同工单互不阻塞），使「并发报废各自按
生效时状态判断」成为确定的先后顺序，并避免高争用下的重算风暴；查单与
完工确认不拿该锁。

查单只读已提交数据：报废意向在第 4 步之前绝不落库，因此计算期间 GET
到的始终是上一个已生效状态。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from . import db, imposition, planner

NEW = "NEW"  # 领新纸

logger = logging.getLogger("app.service")

# 同一工单的报废串行化：并发同单报废按各自「生效时」的最新状态排队判断，
# 避免乐观重排在高争用下反复失效/重算。查单与完工确认不获取这把锁，
# 因此重排计算期间同单/他单的查询、确认依然即时。不同工单各用各的锁。
_scrap_locks_guard = threading.Lock()
_scrap_locks: dict[str, threading.Lock] = {}


def _scrap_lock_for(oid: str) -> threading.Lock:
    with _scrap_locks_guard:
        lk = _scrap_locks.get(oid)
        if lk is None:
            lk = threading.Lock()
            _scrap_locks[oid] = lk
        return lk


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


def _replan_delay_seconds() -> float:
    """可选的演示/测试延迟（环境变量 REPLAN_DELAY_SECONDS）。

    用来在小规模数据上稳定复现「重排计算进行中」的时间窗；默认 0，
    不影响正常性能与测试。
    """
    raw = os.environ.get("REPLAN_DELAY_SECONDS", "0")
    try:
        return max(0.0, float(raw or 0))
    except (TypeError, ValueError):
        return 0.0


def _load_order(c, oid: str):
    row = c.execute("SELECT * FROM work_orders WHERE id = ?", (oid,)).fetchone()
    if row is None:
        raise ApiError(404, "工单不存在")
    return row


def _position_rows(c, oid: str):
    return c.execute(
        "SELECT * FROM positions WHERE work_order_id = ? ORDER BY pos_index", (oid,)
    ).fetchall()


def _scrapped_ids(c, oid: str) -> set:
    rows = c.execute("SELECT sheet_id FROM scraps WHERE work_order_id = ?", (oid,)).fetchall()
    return {r["sheet_id"] for r in rows}


def _completed_sheet_ids(rows) -> set[str]:
    """已完工位置当前占用的旧纸（完工必凭当前有效派工确认）。"""
    return {
        r["assignment"]
        for r in rows
        if r["status"] == "completed" and r["assignment"] != NEW
    }


def _state(c, oid: str) -> dict:
    """工单当前状态：返印单（从外到内）+ 汇总。只反映已提交（生效）数据。"""
    order = _load_order(c, oid)
    rows = _position_rows(c, oid)
    positions = []
    new_paper = old_used = faces_remaining = completed = 0
    for r in rows:
        planned = json.loads(r["faces_json"])
        remaining = [] if r["status"] == "completed" else planned
        if r["assignment"] == NEW:
            new_paper += 1
        else:
            old_used += 1
        faces_remaining += len(remaining)
        completed += r["status"] == "completed"
        positions.append(
            {
                "position": r["pos_index"],
                "required": json.loads(r["required_json"]),
                "use": r["assignment"],
                "faces_to_print": remaining,
                "status": r["status"],
                "dispatch_id": r["dispatch_id"],
            }
        )
    return {
        "work_order_id": oid,
        "status": "completed" if completed == len(rows) else "in_progress",
        "padded_pages": json.loads(order["pages_json"]),
        "old_sheets": json.loads(order["old_sheets_json"]),
        "scrapped_sheets": sorted(_scrapped_ids(c, oid)),
        "positions": positions,
        "summary": {
            "positions": len(rows),
            "old_sheets_used": old_used,
            "new_paper": new_paper,
            "faces_to_print": faces_remaining,
            "completed_positions": completed,
        },
        "created_at": order["created_at"],
    }


def create_work_order(pages: list, old_sheets: list[dict]) -> dict:
    """提交新版页序与旧印张，生成返印单（每个位置带派工号）。"""
    padded = imposition.pad_pages(pages)
    required_list = imposition.saddle_stitch_positions(padded)
    plan = planner.plan_positions(required_list, old_sheets)
    oid = _uuid()
    now = _now()
    with db.lock():
        c = db.conn()
        with c:
            c.execute(
                "INSERT INTO work_orders (id, pages_json, old_sheets_json, created_at) VALUES (?,?,?,?)",
                (oid, json.dumps(padded, ensure_ascii=False), json.dumps(old_sheets, ensure_ascii=False), now),
            )
            for pi, (required, p) in enumerate(zip(required_list, plan)):
                dispatch_id = _uuid()
                assignment = p["use"] if p["use"] is not None else NEW
                c.execute(
                    "INSERT INTO positions (work_order_id, pos_index, required_json, assignment, faces_json, status, dispatch_id)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        oid,
                        pi,
                        json.dumps(required, ensure_ascii=False),
                        assignment,
                        json.dumps(p["faces"]),
                        "pending",
                        dispatch_id,
                    ),
                )
                c.execute(
                    "INSERT INTO dispatches (id, work_order_id, pos_index, assignment, state, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (dispatch_id, oid, pi, assignment, "active", now),
                )
        return _state(c, oid)


def get_work_order(oid: str) -> dict:
    with db.lock():
        return _state(db.conn(), oid)


def list_work_orders() -> list[dict]:
    with db.lock():
        c = db.conn()
        rows = c.execute("SELECT id FROM work_orders ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            st = _state(c, r["id"])
            out.append(
                {
                    "work_order_id": r["id"],
                    "created_at": st["created_at"],
                    "status": st["status"],
                    "summary": st["summary"],
                }
            )
        return out


def confirm_position(oid: str, pos_index: int, dispatch_id: str) -> dict:
    """凭该位置的有效派工确认完工。旧派工（已退回）确认会被拒绝。"""
    with db.lock():
        c = db.conn()
        _load_order(c, oid)
        pos = c.execute(
            "SELECT * FROM positions WHERE work_order_id = ? AND pos_index = ?",
            (oid, pos_index),
        ).fetchone()
        if pos is None:
            raise ApiError(404, "位置不存在")
        disp = c.execute(
            "SELECT * FROM dispatches WHERE id = ? AND work_order_id = ?",
            (dispatch_id, oid),
        ).fetchone()
        if disp is None:
            raise ApiError(404, "派工不存在")
        if disp["pos_index"] != pos_index:
            raise ApiError(409, "该派工不属于此位置")
        if disp["state"] == "returned":
            raise ApiError(409, "旧派工已退回：该位置已改派，请按新派工确认")
        if disp["state"] == "active":
            with c:
                c.execute("UPDATE dispatches SET state = 'confirmed' WHERE id = ?", (dispatch_id,))
                c.execute(
                    "UPDATE positions SET status = 'completed' WHERE work_order_id = ? AND pos_index = ?",
                    (oid, pos_index),
                )
        # state == 'confirmed'：重复确认按幂等处理，直接返回当前状态
        return _state(c, oid)


def _scrap_snapshot(c, oid: str):
    """锁内读取报废判断所需的全部已生效状态。"""
    order = _load_order(c, oid)
    rows = _position_rows(c, oid)
    scrapped = _scrapped_ids(c, oid)
    old_sheets = json.loads(order["old_sheets_json"])
    return old_sheets, rows, scrapped


def _check_scrap_batch(sheet_ids: list[str], by_id: dict, scrapped: set, completed_sheet_ids: set):
    """按当前生效状态校验整批；任一不满足即整批拒绝（404/409）。"""
    for sid in sheet_ids:
        if sid not in by_id:
            raise ApiError(404, f"旧印张不存在: {sid}")
        if sid in scrapped:
            raise ApiError(409, f"旧印张已报废: {sid}")
        if sid in completed_sheet_ids:
            raise ApiError(409, f"旧印张已完工，不能报废: {sid}")


def _state_fingerprint(scrapped: set, rows) -> tuple:
    """择纸方案所依赖的已生效状态指纹；变化即需按最新状态重排。"""
    return (
        tuple(sorted(scrapped)),
        tuple(
            (r["pos_index"], r["status"], r["assignment"], r["dispatch_id"])
            for r in rows
        ),
    )


def _plan_replan(old_sheets: list[dict], rows, scrapped: set, sheet_ids: list[str]) -> list[dict]:
    """锁外执行：完工位置固定，其余位置 + 剩余旧纸重新优化。

    规则与原实现一致：报废批与已完工占用的纸不进纸池；keep 提示在目标
    值相同时尽量不动原安排。
    """
    completed_sheet_ids = _completed_sheet_ids(rows)
    scrapped_all = scrapped | set(sheet_ids)
    pool = [
        s
        for s in old_sheets
        if s["id"] not in scrapped_all and s["id"] not in completed_sheet_ids
    ]
    free = [r for r in rows if r["status"] != "completed"]
    required_list = [json.loads(r["required_json"]) for r in free]
    keep = {i: r["assignment"] for i, r in enumerate(free) if r["assignment"] != NEW}

    delay = _replan_delay_seconds()
    if delay:
        time.sleep(delay)  # 可选：放大「排程计算中」窗口，便于演示/压测
    return planner.plan_positions(required_list, pool, keep)


def _commit_scrap(c, oid: str, sheet_ids: list[str], rows, plan: list[dict]) -> list[dict]:
    """同一事务内登记报废并应用改派（调用方持 db.lock，且指纹未变）。"""
    now = _now()
    free = [r for r in rows if r["status"] != "completed"]
    returned = []
    with c:
        for sid in sheet_ids:
            c.execute(
                "INSERT INTO scraps (work_order_id, sheet_id, created_at) VALUES (?,?,?)",
                (oid, sid, now),
            )
        for r, p in zip(free, plan):
            new_assignment = p["use"] if p["use"] is not None else NEW
            if new_assignment == r["assignment"]:
                continue  # 未改派：保留原派工
            c.execute("UPDATE dispatches SET state = 'returned' WHERE id = ?", (r["dispatch_id"],))
            new_dispatch = _uuid()
            c.execute(
                "UPDATE positions SET assignment = ?, faces_json = ?, dispatch_id = ?"
                " WHERE work_order_id = ? AND pos_index = ?",
                (new_assignment, json.dumps(p["faces"]), new_dispatch, oid, r["pos_index"]),
            )
            c.execute(
                "INSERT INTO dispatches (id, work_order_id, pos_index, assignment, state, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (new_dispatch, oid, r["pos_index"], new_assignment, "active", now),
            )
            returned.append(
                {
                    "position": r["pos_index"],
                    "returned_dispatch_id": r["dispatch_id"],
                    "old": r["assignment"],
                    "new": new_assignment,
                }
            )
    return returned


def scrap_sheets(oid: str, sheet_ids: list[str]) -> dict:
    """报废旧印张并重排（对外入口）。

    同一工单的报废在工单级锁内串行进入「快照 -> 锁外择纸 -> 生效时复核
    -> 原子提交」；查单/完工确认以及其他工单不拿这把锁，重排期间照常服务。
    """
    sheet_ids = list(dict.fromkeys(sheet_ids))  # 去重保序
    with _scrap_lock_for(oid):
        return _scrap_sheets_run(oid, sheet_ids)


def _scrap_sheets_run(oid: str, sheet_ids: list[str]) -> dict:
    """报废未完工的旧印张并重排（支持计算期间查单/确认与并发报废）。

    - 已完工位置留在原位，其旧纸不再进入纸池；
    - 其余位置用最新状态下的剩余旧纸重新优化（目标不变：先少新纸，再少待印面）；
    - 被改派位置的旧派工退回（confirm 会拒绝），并签发新派工；
    - 未改派位置保留原派工，仍可按原安排确认；
    - 本批印张若在生效前被凭有效派工确认完工，整批 409，不报废、不改派；
    - 并发报废以各自拿到提交锁时的最新状态为准。
    """
    while True:
        # 1) 锁内快照 + 基础校验（请求时已存在的冲突可快速失败）
        with db.lock():
            c = db.conn()
            old_sheets, rows, scrapped = _scrap_snapshot(c, oid)
            by_id = {s["id"]: s for s in old_sheets}
            completed_sheet_ids = _completed_sheet_ids(rows)
            _check_scrap_batch(sheet_ids, by_id, scrapped, completed_sheet_ids)
            fingerprint = _state_fingerprint(scrapped, rows)

        # 2) 锁外重排计算：不阻塞同单/他单的查单与完工确认
        plan = _plan_replan(old_sheets, rows, scrapped, sheet_ids)

        # 3) 锁内按生效时最新状态复核，并与报废登记/改派原子提交
        with db.lock():
            c = db.conn()
            old_sheets2, rows2, scrapped2 = _scrap_snapshot(c, oid)
            by_id2 = {s["id"]: s for s in old_sheets2}
            completed_sheet_ids2 = _completed_sheet_ids(rows2)
            # 权威冲突判断：先于指纹比较，确保完工冲突得到 409 而不是静默重试
            _check_scrap_batch(sheet_ids, by_id2, scrapped2, completed_sheet_ids2)
            if _state_fingerprint(scrapped2, rows2) != fingerprint:
                # 计算期间有别的确认/报废先生效：按最新状态重新择纸
                logger.info(
                    "scrap oid=%s batch=%s snapshot changed before commit; replanning",
                    oid,
                    sheet_ids,
                )
                continue

            returned = _commit_scrap(c, oid, sheet_ids, rows2, plan)
            state = _state(c, oid)
            logger.info(
                "scrap oid=%s batch=%s committed; returned=%d dispatch(es)",
                oid,
                sheet_ids,
                len(returned),
            )
        state["returned_dispatches"] = returned
        return state
