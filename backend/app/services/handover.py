from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts

# 设计抉择（题目要求三处择一，均在此落实）：
# 1) 计划-段关系：独立交班计划表 handover_plans + 段交班单 handover_segments
#    外键挂 plan_id，并加 UNIQUE(plan_id, seq)；发起时一次性切出全部段。
# 2) 失败隔离：段级独立事务提交。每段过户在自己的事务里提交；本段失败只回滚
#    本段过户，已提交的前序段过户不回滚，后续段置「已阻断」，计划标失败。
# 3) 并集校验物化：独立计划对账报告表 plan_reconcile_reports，发起切分时与
#    计划终态各落一份；校验为真实集合并集对比快照，禁止用成功计数加总冒充。


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_tx() -> sqlite3.Connection:
    """手动事务连接（autocommit），段级事务用 BEGIN IMMEDIATE 显式控制。"""
    conn = connect()
    conn.isolation_level = None
    return conn


# ---------------------------------------------------------------- 只读查询


def list_shifts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())


def list_batches(owner_shift_id: int | None = None) -> list[dict]:
    with connect() as conn:
        if owner_shift_id is None:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                ORDER BY b.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                WHERE b.owner_shift_id = ?
                ORDER BY b.id
                """,
                (owner_shift_id,),
            ).fetchall()
        return rows_to_dicts(rows)


def list_handovers() -> list[dict]:
    """交班计划列表（含段进度与最近一次并集校验结论）。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name,
                   (SELECT COUNT(*) FROM handover_segments s
                     WHERE s.plan_id = p.id AND s.status = '已完成') AS done_segments,
                   (SELECT r.union_ok FROM plan_reconcile_reports r
                     WHERE r.plan_id = p.id ORDER BY r.id DESC LIMIT 1) AS latest_union_ok,
                   (SELECT r.plan_ok FROM plan_reconcile_reports r
                     WHERE r.plan_id = p.id ORDER BY r.id DESC LIMIT 1) AS latest_plan_ok
            FROM handover_plans p
            JOIN shifts fs ON fs.id = p.from_shift_id
            JOIN shifts ts ON ts.id = p.to_shift_id
            ORDER BY p.id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["latest_union_ok"] = None if d["latest_union_ok"] is None else bool(d["latest_union_ok"])
            d["latest_plan_ok"] = None if d["latest_plan_ok"] is None else bool(d["latest_plan_ok"])
            out.append(d)
        return out


def get_handover(plan_id: int) -> dict:
    """计划详情：计划 + 有序段链 + 每段清单 + 物化报告 + 实时并集预览。"""
    with connect() as conn:
        p = conn.execute(
            """
            SELECT p.*, fs.name AS from_shift_name, ts.name AS to_shift_name
            FROM handover_plans p
            JOIN shifts fs ON fs.id = p.from_shift_id
            JOIN shifts ts ON ts.id = p.to_shift_id
            WHERE p.id = ?
            """,
            (plan_id,),
        ).fetchone()
        if not p:
            raise HTTPException(404, "交班计划不存在")
        segs = conn.execute(
            "SELECT * FROM handover_segments WHERE plan_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
        seg_list = []
        all_items: list[dict] = []
        for s in segs:
            items = conn.execute(
                """
                SELECT b.*, os.name AS owner_shift_name
                FROM handover_segment_items hi
                JOIN batches b ON b.id = hi.batch_id
                LEFT JOIN shifts os ON os.id = b.owner_shift_id
                WHERE hi.segment_id = ?
                ORDER BY b.id
                """,
                (s["id"],),
            ).fetchall()
            sd = dict(s)
            sd["keys"] = [i["biz_key"] for i in items]
            sd["items"] = rows_to_dicts(items)
            seg_list.append(sd)
            all_items.extend(sd["items"])
        report = conn.execute(
            "SELECT * FROM plan_reconcile_reports WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        out = dict(p)
        out["segments"] = seg_list
        out["items"] = all_items
        out["done_segments"] = sum(1 for s in seg_list if s["status"] == "已完成")
        out["reconcile"] = _report_to_dict(report) if report else None
        # 进行中计划的并集实时预览（终态后以物化报告为准）；同源同事务内计算
        out["union_check"] = _union_check(conn, plan_id)
        return out


def dashboard() -> dict:
    with connect() as conn:
        shifts = rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())
        inflight = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '在途'"
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM handover_plans WHERE status = '进行中'"
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT * FROM handover_plans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "班次数": len(shifts),
            "在途批次数": inflight,
            "进行中交班": active,
            "最近交班": dict(last) if last else None,
            "班次": shifts,
        }


# ---------------------------------------------------------------- 并集校验


def _collect_sets(conn, plan_id: int) -> dict:
    """真实集合运算：各段清单键的并集 vs 发起快照；段间键求交。禁止计数加总。"""
    plan = conn.execute("SELECT * FROM handover_plans WHERE id = ?", (plan_id,)).fetchone()
    snapshot = json.loads(plan["snapshot_json"])
    snap_keys = [s["biz_key"] for s in snapshot]
    segs = conn.execute(
        "SELECT * FROM handover_segments WHERE plan_id = ? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    per_seg = []
    union: set[str] = set()
    overlap: list[str] = []
    for s in segs:
        keys = [
            r["biz_key"]
            for r in conn.execute(
                """
                SELECT b.biz_key
                FROM handover_segment_items hi
                JOIN batches b ON b.id = hi.batch_id
                WHERE hi.segment_id = ?
                ORDER BY b.id
                """,
                (s["id"],),
            ).fetchall()
        ]
        for k in keys:
            if k in union and k not in overlap:
                overlap.append(k)
            union.add(k)
        per_seg.append({"seq": s["seq"], "status": s["status"], "keys": keys})
    snap_set = set(snap_keys)
    missing = [k for k in snap_keys if k not in union]
    extra = sorted(k for k in union if k not in snap_set)
    all_ok = bool(segs) and all(s["status"] == "已完成" for s in segs)
    return {
        "snap_keys": snap_keys,
        "per_seg": per_seg,
        "union_keys": [k for k in snap_keys if k in union] + extra,
        "union": union,
        "missing": missing,
        "extra": extra,
        "overlap": overlap,
        "union_ok": not missing and not extra and not overlap,
        "all_segments_ok": all_ok,
    }


def _union_check(conn, plan_id: int) -> dict:
    c = _collect_sets(conn, plan_id)
    return {
        "快照键数": len(c["snap_keys"]),
        "并集键数": len(c["union"]),
        "缺失键": c["missing"],
        "多出键": c["extra"],
        "段间相交键": c["overlap"],
        "段间互斥": not c["overlap"],
        "并集完整": c["union_ok"],
        "每段成功": c["all_segments_ok"],
        "计划成功": c["all_segments_ok"] and c["union_ok"],
    }


def _write_plan_report(conn, plan_id: int, stage: str) -> None:
    """把并集校验结果物化到 plan_reconcile_reports（与状态变更同事务）。"""
    c = _collect_sets(conn, plan_id)
    plan_ok = c["all_segments_ok"] and c["union_ok"]
    detail = {
        "阶段": stage,
        "快照键": c["snap_keys"],
        "段清单": c["per_seg"],
        "并集键": c["union_keys"],
        "缺失键": c["missing"],
        "多出键": c["extra"],
        "段间相交键": c["overlap"],
        "说明": "并集为各段清单键的集合并，与发起快照逐一比对；非各段成功计数加总",
    }
    conn.execute(
        """
        INSERT INTO plan_reconcile_reports(
          plan_id, stage, snapshot_count, union_count,
          missing_count, extra_count, overlap_count,
          disjoint, union_ok, all_segments_ok, plan_ok, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            stage,
            len(c["snap_keys"]),
            len(c["union"]),
            len(c["missing"]),
            len(c["extra"]),
            len(c["overlap"]),
            1 if not c["overlap"] else 0,
            1 if c["union_ok"] else 0,
            1 if c["all_segments_ok"] else 0,
            1 if plan_ok else 0,
            json.dumps(detail, ensure_ascii=False),
            _now(),
        ),
    )


def _report_to_dict(r) -> dict:
    d = dict(r)
    d["detail"] = json.loads(d.pop("detail_json"))
    for k in ("disjoint", "union_ok", "all_segments_ok", "plan_ok"):
        d[k] = bool(d[k])
    d["一致"] = d["plan_ok"]
    return d


def list_reconcile() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, p.from_shift_id, p.to_shift_id,
                   fs.name AS from_shift_name, ts.name AS to_shift_name
            FROM plan_reconcile_reports r
            JOIN handover_plans p ON p.id = r.plan_id
            JOIN shifts fs ON fs.id = p.from_shift_id
            JOIN shifts ts ON ts.id = p.to_shift_id
            ORDER BY r.id DESC
            """
        ).fetchall()
        return [_report_to_dict(r) for r in rows]


# ---------------------------------------------------------------- 发起交班


def start_handover(from_shift_id: int, to_shift_id: int, segment_size: int | None = None) -> dict:
    if from_shift_id == to_shift_id:
        raise HTTPException(400, "交班双方不能是同一班次")
    if segment_size is not None and segment_size < 1:
        raise HTTPException(400, "分段上限 N 必须 ≥ 1")
    conn = _connect_tx()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fs = conn.execute("SELECT * FROM shifts WHERE id = ?", (from_shift_id,)).fetchone()
        ts = conn.execute("SELECT * FROM shifts WHERE id = ?", (to_shift_id,)).fetchone()
        if not fs or not ts:
            raise HTTPException(404, "班次不存在")
        active = conn.execute(
            "SELECT id FROM handover_plans WHERE status = '进行中' LIMIT 1"
        ).fetchone()
        if active:
            raise HTTPException(409, f"已有进行中的交班计划#{active['id']}，禁止并行交班")
        batches = conn.execute(
            "SELECT id, biz_key FROM batches WHERE owner_shift_id = ? AND status = '在途' ORDER BY id",
            (from_shift_id,),
        ).fetchall()
        if not batches:
            raise HTTPException(400, "交班方没有在途批次可交接")
        # 缺省单段全量（等价旧行为）；N ≥ 快照键数时同样只有一段
        n = segment_size if segment_size is not None else len(batches)
        snapshot = [{"batch_id": b["id"], "biz_key": b["biz_key"]} for b in batches]
        chunks = [snapshot[i : i + n] for i in range(0, len(snapshot), n)]
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO handover_plans(
              from_shift_id, to_shift_id, segment_size, status,
              batch_count, segment_count, snapshot_json, started_at
            ) VALUES (?, ?, ?, '进行中', ?, ?, ?, ?)
            """,
            (
                from_shift_id,
                to_shift_id,
                n,
                len(snapshot),
                len(chunks),
                json.dumps(snapshot, ensure_ascii=False),
                now,
            ),
        )
        pid = cur.lastrowid
        # 一次性切出全部段交班单并落库——禁止只创建第一段却声称已切分
        for idx, chunk in enumerate(chunks, start=1):
            cur = conn.execute(
                """
                INSERT INTO handover_segments(plan_id, seq, status, batch_count, started_at)
                VALUES (?, ?, '待处理', ?, ?)
                """,
                (pid, idx, len(chunk), now),
            )
            sid = cur.lastrowid
            for item in chunk:
                conn.execute(
                    "INSERT INTO handover_segment_items(segment_id, batch_id) VALUES (?, ?)",
                    (sid, item["batch_id"]),
                )
        conn.execute("UPDATE shifts SET status = '交班中' WHERE id = ?", (from_shift_id,))
        conn.execute("UPDATE shifts SET status = '接班中' WHERE id = ?", (to_shift_id,))
        # 发起切分校验物化：证明全部段已切出且并集 == 快照、段间不交
        _write_plan_report(conn, pid, stage="发起切分校验")
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_handover(pid)


# ---------------------------------------------------------------- 逐段办理


def _do_complete_segment(plan_id: int, seq: int) -> dict:
    """完成指定段：段级独立事务。本段失败只回滚本段，前序已提交段不回滚。"""
    conn = _connect_tx()
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute("SELECT * FROM handover_plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "交班计划不存在")
        if plan["status"] != "进行中":
            raise HTTPException(409, f"计划#{plan_id}已{plan['status']}，禁止再办理段交班单")
        seg = conn.execute(
            "SELECT * FROM handover_segments WHERE plan_id = ? AND seq = ?",
            (plan_id, seq),
        ).fetchone()
        if not seg:
            raise HTTPException(404, f"计划#{plan_id}不存在第{seq}段")
        if seg["status"] == "已完成":
            raise HTTPException(409, f"第{seq}段已完成，禁止重复办理")
        if seg["status"] == "已阻断":
            raise HTTPException(409, f"前序段失败，第{seq}段已阻断，禁止办理")
        # 段必须按序：上一段（及之前所有段）已完成才能办本段
        undone_prev = conn.execute(
            "SELECT COUNT(*) AS c FROM handover_segments WHERE plan_id = ? AND seq < ? AND status != '已完成'",
            (plan_id, seq),
        ).fetchone()["c"]
        if undone_prev:
            raise HTTPException(409, "上一段未完成，禁止跳段办理：段必须按序完成")
        items = conn.execute(
            """
            SELECT b.id, b.biz_key
            FROM handover_segment_items hi
            JOIN batches b ON b.id = hi.batch_id
            WHERE hi.segment_id = ?
            ORDER BY b.id
            """,
            (seg["id"],),
        ).fetchall()
        # 本段过户：整段原子，任一键已不在交班方在途名下则本段整体失败
        failed_keys = []
        for it in items:
            cur = conn.execute(
                "UPDATE batches SET owner_shift_id = ? WHERE id = ? AND owner_shift_id = ? AND status = '在途'",
                (plan["to_shift_id"], it["id"], plan["from_shift_id"]),
            )
            if cur.rowcount == 0:
                failed_keys.append(it["biz_key"])
        if failed_keys:
            # 段级独立事务：回滚本段过户；前序段已各自提交，不受牵连
            conn.rollback()
            reason = f"段内键 {'、'.join(failed_keys)} 已不在交班方在途名下，本段过户整体回滚"
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            conn.execute(
                "UPDATE handover_segments SET status = '失败', reason = ?, finished_at = ? WHERE id = ?",
                (reason, now, seg["id"]),
            )
            # 任一段失败：后续段阻断，计划标失败
            conn.execute(
                "UPDATE handover_segments SET status = '已阻断', reason = '前序段失败，按计划失败处理' WHERE plan_id = ? AND status = '待处理'",
                (plan_id,),
            )
            conn.execute(
                "UPDATE handover_plans SET status = '失败', reason = ?, finished_at = ? WHERE id = ?",
                (f"第{seq}段失败：{reason}", now, plan_id),
            )
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (plan["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '空闲' WHERE id = ?", (plan["to_shift_id"],))
            _write_plan_report(conn, plan_id, stage="终态定案")
            conn.commit()
            return get_handover(plan_id)
        now = _now()
        conn.execute(
            "UPDATE handover_segments SET status = '已完成', finished_at = ? WHERE id = ?",
            (now, seg["id"]),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM handover_segments WHERE plan_id = ? AND status != '已完成'",
            (plan_id,),
        ).fetchone()["c"]
        if remaining == 0:
            # 全部段已完成：计划级并集校验，物化落档；计划成功 ⟺ 每段成功且并集完整
            check = _union_check(conn, plan_id)
            if check["并集完整"]:
                conn.execute(
                    "UPDATE handover_plans SET status = '已完成', finished_at = ? WHERE id = ?",
                    (now, plan_id),
                )
                conn.execute("UPDATE shifts SET status = '已交班' WHERE id = ?", (plan["from_shift_id"],))
                conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (plan["to_shift_id"],))
            else:
                conn.execute(
                    "UPDATE handover_plans SET status = '失败', reason = ?, finished_at = ? WHERE id = ?",
                    ("计划级并集校验未通过：各段清单并集与发起快照不一致", now, plan_id),
                )
                conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (plan["from_shift_id"],))
                conn.execute("UPDATE shifts SET status = '空闲' WHERE id = ?", (plan["to_shift_id"],))
            _write_plan_report(conn, plan_id, stage="终态定案")
        conn.commit()
        return get_handover(plan_id)
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_segment(plan_id: int, seq: int) -> dict:
    return _do_complete_segment(plan_id, seq)


def complete_handover(plan_id: int) -> dict:
    """按序完成剩余全部段（每段仍是独立事务提交）；任一段失败即停。"""
    with connect() as conn:
        row = conn.execute("SELECT id FROM handover_plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            raise HTTPException(404, "交班计划不存在")
    while True:
        with connect() as conn:
            plan = conn.execute(
                "SELECT status FROM handover_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if plan["status"] != "进行中":
                break
            nxt = conn.execute(
                "SELECT seq FROM handover_segments WHERE plan_id = ? AND status = '待处理' ORDER BY seq LIMIT 1",
                (plan_id,),
            ).fetchone()
            if not nxt:
                break
        result = _do_complete_segment(plan_id, nxt["seq"])
        if result["status"] != "进行中":
            break
    return get_handover(plan_id)
