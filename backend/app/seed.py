from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  keeper TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '空闲'
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  biz_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  owner_shift_id INTEGER REFERENCES shifts(id),
  status TEXT NOT NULL DEFAULT '在途',
  note TEXT NOT NULL DEFAULT ''
);
-- 交班计划：一次发起交班的总单。snapshot_json 为发起时刻的在途快照，落库后不可变。
CREATE TABLE IF NOT EXISTS handover_plans (
  id INTEGER PRIMARY KEY,
  from_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  to_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  segment_size INTEGER NOT NULL CHECK (segment_size >= 1),
  status TEXT NOT NULL DEFAULT '进行中',
  batch_count INTEGER NOT NULL DEFAULT 0,
  segment_count INTEGER NOT NULL DEFAULT 0,
  snapshot_json TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
-- 段交班单：外键挂计划 id，(plan_id, seq) 唯一，链内按 seq 有序。
CREATE TABLE IF NOT EXISTS handover_segments (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES handover_plans(id),
  seq INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待处理',
  batch_count INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE (plan_id, seq)
);
CREATE TABLE IF NOT EXISTS handover_segment_items (
  segment_id INTEGER NOT NULL REFERENCES handover_segments(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  PRIMARY KEY (segment_id, batch_id)
);
-- 计划级对账报告：并集校验结果物化落档（发起切分校验 + 终态定案各一份）。
CREATE TABLE IF NOT EXISTS plan_reconcile_reports (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES handover_plans(id),
  stage TEXT NOT NULL,
  snapshot_count INTEGER NOT NULL,
  union_count INTEGER NOT NULL,
  missing_count INTEGER NOT NULL,
  extra_count INTEGER NOT NULL,
  overlap_count INTEGER NOT NULL,
  disjoint INTEGER NOT NULL,
  union_ok INTEGER NOT NULL,
  all_segments_ok INTEGER NOT NULL,
  plan_ok INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS c FROM shifts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for s in data["shifts"]:
            conn.execute(
                "INSERT INTO shifts(id, name, keeper, status) VALUES (?, ?, ?, ?)",
                (s["id"], s["name"], s["keeper"], s["status"]),
            )
        for b in data["batches"]:
            conn.execute(
                "INSERT INTO batches(id, biz_key, title, owner_shift_id, status, note) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    b["id"],
                    b["biz_key"],
                    b["title"],
                    b["owner_shift_id"],
                    b["status"],
                    b.get("note", ""),
                ),
            )
        conn.commit()
