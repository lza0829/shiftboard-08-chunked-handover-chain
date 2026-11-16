"""分段交班链条：切分、按序、失败隔离、计划级并集对账。"""
from pathlib import Path
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test_chain")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"
if db_path.exists():
    db_path.unlink()

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402

SEED = json.loads((ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8"))


def reset_db():
    """恢复种子态：清空计划/段/报告，批次与班次回到 fixtures。"""
    with connect() as conn:
        for t in ("plan_reconcile_reports", "handover_segment_items",
                  "handover_segments", "handover_plans", "batches"):
            conn.execute(f"DELETE FROM {t}")
        for s in SEED["shifts"]:
            conn.execute("UPDATE shifts SET status = ? WHERE id = ?", (s["status"], s["id"]))
        for b in SEED["batches"]:
            conn.execute(
                "INSERT INTO batches(id, biz_key, title, owner_shift_id, status, note) VALUES (?, ?, ?, ?, ?, ?)",
                (b["id"], b["biz_key"], b["title"], b["owner_shift_id"], b["status"], b.get("note", "")),
            )
        conn.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(client):
    reset_db()
    yield
    reset_db()  # 模块结束后保持种子态，不影响同进程其它测试模块


def start(client, n=None, frm=1, to=2):
    body = {"from_shift_id": frm, "to_shift_id": to}
    if n is not None:
        body["segment_size"] = n
    return client.post("/api/handovers/start", json=body)


def owners(client):
    return {b["biz_key"]: b["owner_shift_id"] for b in client.get("/api/batches").json()}


# ---------------------------------------------------------------- 切分


def test_start_splits_full_chain_not_just_first_segment(client):
    r = start(client, n=2)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["status"] == "进行中"
    assert plan["segment_size"] == 2
    assert plan["batch_count"] == 4
    # 禁止只创建第一段却声称已切分：全部段必须一次落库成链
    assert plan["segment_count"] == 2
    segs = plan["segments"]
    assert [s["seq"] for s in segs] == [1, 2]
    assert all(s["status"] == "待处理" for s in segs)
    assert all(len(s["keys"]) <= 2 for s in segs)
    # 段间键不交，且并集 == 发起快照
    k1, k2 = set(segs[0]["keys"]), set(segs[1]["keys"])
    assert not (k1 & k2)
    assert k1 | k2 == {"B20261115-01", "B20261115-02", "B20261115-03", "B20261114-09"}
    # 发起切分校验已物化
    rep = plan["reconcile"]
    assert rep["stage"] == "发起切分校验"
    assert rep["union_ok"] is True and rep["disjoint"] is True
    assert rep["all_segments_ok"] is False and rep["plan_ok"] is False


def test_segment_size_must_be_positive(client):
    assert start(client, n=0).status_code == 400
    assert start(client, n=-3).status_code == 400


def test_default_is_single_full_segment(client):
    plan = start(client).json()
    assert plan["segment_count"] == 1
    assert plan["segments"][0]["batch_count"] == 4
    done = client.post(f"/api/handovers/{plan['id']}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "已完成"


def test_parallel_plan_rejected(client):
    assert start(client, n=2).status_code == 200
    again = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 3})
    assert again.status_code == 409


# ---------------------------------------------------------------- 按序


def test_segments_must_complete_in_order(client):
    plan = start(client, n=1).json()  # 4 段
    pid = plan["id"]
    # 跳段：第 2 段先于第 1 段办理 → 拒绝
    r = client.post(f"/api/handovers/{pid}/segments/2/complete")
    assert r.status_code == 409
    assert owners(client)["B20261115-02"] == 1  # 未过户
    # 第 1 段可办
    r = client.post(f"/api/handovers/{pid}/segments/1/complete")
    assert r.status_code == 200, r.text
    assert r.json()["segments"][0]["status"] == "已完成"
    assert owners(client)["B20261115-01"] == 2
    # 重复办理已完成段 → 拒绝
    assert client.post(f"/api/handovers/{pid}/segments/1/complete").status_code == 409
    # 仍禁止跳段（第 4 段）
    assert client.post(f"/api/handovers/{pid}/segments/4/complete").status_code == 409
    # 依次办完
    for seq in (2, 3, 4):
        assert client.post(f"/api/handovers/{pid}/segments/{seq}/complete").status_code == 200
    final = client.get(f"/api/handovers/{pid}").json()
    assert final["status"] == "已完成"
    assert all(s["status"] == "已完成" for s in final["segments"])
    assert all(v == 2 for k, v in owners(client).items() if k.startswith("B202611"))


# ---------------------------------------------------------------- 失败隔离


def test_failed_segment_does_not_roll_back_completed_segments(client):
    plan = start(client, n=1).json()  # 4 段，每段 1 键
    pid = plan["id"]
    assert client.post(f"/api/handovers/{pid}/segments/1/complete").status_code == 200
    assert owners(client)["B20261115-01"] == 2  # 第 1 段已过户提交
    # 制造第 2 段失败：其键已不在交班方在途名下
    with connect() as conn:
        conn.execute("UPDATE batches SET status = '已结清' WHERE biz_key = 'B20261115-02'")
        conn.commit()
    r = client.post(f"/api/handovers/{pid}/segments/2/complete")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["status"] == "失败"
    segs = {s["seq"]: s for s in plan["segments"]}
    assert segs[2]["status"] == "失败"
    assert segs[3]["status"] == "已阻断" and segs[4]["status"] == "已阻断"
    # 后续段不得完成
    assert client.post(f"/api/handovers/{pid}/segments/3/complete").status_code == 409
    # 关键：前段已提交的过户禁止被后段失败回滚
    assert segs[1]["status"] == "已完成"
    assert owners(client)["B20261115-01"] == 2
    # 失败段本段回滚：其键未被过户（仍是交班方名下、已结清）
    b2 = [b for b in client.get("/api/batches").json() if b["biz_key"] == "B20261115-02"][0]
    assert (b2["owner_shift_id"], b2["status"]) == (1, "已结清")
    # 未办理的键原地不动
    assert owners(client)["B20261115-03"] == 1
    assert owners(client)["B20261114-09"] == 1
    # 计划级报告物化：并集仍完整，但每段未成功 → 计划失败
    rep = plan["reconcile"]
    assert rep["stage"] == "终态定案"
    assert rep["union_ok"] is True
    assert rep["all_segments_ok"] is False
    assert rep["plan_ok"] is False
    # 计划已终态，禁止再办任何段；但允许发起新计划（互斥释放）
    assert client.post(f"/api/handovers/{pid}/segments/1/complete").status_code == 409
    assert start(client, n=2, to=3).status_code == 200


# ---------------------------------------------------------------- 计划级对账


def test_plan_success_requires_every_segment_and_complete_union(client):
    plan = start(client, n=3).json()  # 2 段：3 + 1
    pid = plan["id"]
    for seq in (1, 2):
        assert client.post(f"/api/handovers/{pid}/segments/{seq}/complete").status_code == 200
    final = client.get(f"/api/handovers/{pid}").json()
    assert final["status"] == "已完成"
    rep = final["reconcile"]
    assert rep["stage"] == "终态定案"
    assert rep["plan_ok"] is True and rep["union_ok"] is True and rep["all_segments_ok"] is True
    d = rep["detail"]
    assert set(d["并集键"]) == set(d["快照键"]) == {"B20261115-01", "B20261115-02", "B20261115-03", "B20261114-09"}
    assert d["缺失键"] == [] and d["多出键"] == [] and d["段间相交键"] == []
    assert [s["seq"] for s in d["段清单"]] == [1, 2]
    # 对账列表可查（刷新后仍在：物化落库）
    reports = client.get("/api/reconcile").json()
    mine = [r for r in reports if r["plan_id"] == pid]
    assert len(mine) == 2  # 发起切分校验 + 终态定案
    assert mine[0]["stage"] == "终态定案" and mine[0]["一致"] is True
    # 计划详情刷新后仍带段状态与并集校验
    again = client.get(f"/api/handovers/{pid}").json()
    assert again["reconcile"]["plan_ok"] is True
    assert again["union_check"]["并集完整"] is True
    assert [s["status"] for s in again["segments"]] == ["已完成", "已完成"]
