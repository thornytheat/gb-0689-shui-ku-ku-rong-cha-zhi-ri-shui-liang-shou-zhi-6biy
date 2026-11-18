"""闸门核定出库账的持久化（仅账本，不含任何闸门控制指令）。

- verify 按 (测站,日) 幂等 upsert：重跑同一天整体替换工作稿，旧工作稿不累积，天然不重复扣减。
- 已发布账本独立保存；核定失败（有待裁决时段）时不动 published_payload，旧版日账保留。
- 发布使用整数版本乐观锁，冲突返回 409。
- 每次核定与发布写入 gate_audit，可回查采用依据与无法裁决的时段。
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError
from app.normalize import serial

SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_ledger (
  station TEXT NOT NULL,
  date TEXT NOT NULL,
  working_payload TEXT NOT NULL,
  published_payload TEXT,
  published_version INTEGER NOT NULL DEFAULT 0,
  run_id TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (station, date)
);
CREATE TABLE IF NOT EXISTS gate_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  station TEXT NOT NULL,
  date TEXT NOT NULL,
  action TEXT NOT NULL,
  run_id TEXT,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def connection():
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path / "runs.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def upsert_working(days, run_id):
    """幂等保存每日核定工作稿；已发布内容原样保留。返回写入的 (station,date) 列表。"""
    keys = []
    with connection() as db:
        for day in days:
            key = (day["station"], day["date"])
            keys.append(key)
            existing = db.execute(
                "SELECT published_payload, published_version FROM gate_ledger WHERE station=? AND date=?",
                key).fetchone()
            payload = json.dumps(serial(day), ensure_ascii=False)
            if existing is None:
                db.execute(
                    "INSERT INTO gate_ledger(station,date,working_payload,published_payload,"
                    "published_version,run_id,updated_at) VALUES(?,?,?,NULL,0,?,?)",
                    (key[0], key[1], payload, run_id, _now()))
            else:
                db.execute(
                    "UPDATE gate_ledger SET working_payload=?, run_id=?, updated_at=? "
                    "WHERE station=? AND date=?",
                    (payload, run_id, _now(), key[0], key[1]))
            db.execute(
                "INSERT INTO gate_audit(station,date,action,run_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                (key[0], key[1], "核定", run_id,
                 json.dumps({"state": day["state"],
                             "unadjudicated": len(day["uncertain"]),
                             "basis_count": day["basis_count"]}, ensure_ascii=False),
                 _now()))
    return keys


def list_ledger():
    with connection() as db:
        rows = db.execute(
            "SELECT station,date,working_payload,published_payload,published_version,run_id,updated_at "
            "FROM gate_ledger ORDER BY date DESC, station").fetchall()
    return [_row_to_dict(row) for row in rows]


def fetch_day(station, date):
    with connection() as db:
        row = db.execute(
            "SELECT station,date,working_payload,published_payload,published_version,run_id,updated_at "
            "FROM gate_ledger WHERE station=? AND date=?", (station, date)).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "该日核定账不存在", 404)
    return _row_to_dict(row)


def publish(station, date, version):
    """发布工作稿供班次使用。有待裁决时段则拒绝并保留旧版；版本陈旧返回 409。"""
    if not isinstance(version, int):
        raise DomainError("STALE", "版本必须为整数")
    with connection() as db:
        row = db.execute(
            "SELECT * FROM gate_ledger WHERE station=? AND date=?", (station, date)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "该日核定账不存在", 404)
        if row["published_version"] != version:
            raise DomainError("STALE", "核定账版本已更新", 409)
        working = json.loads(row["working_payload"])
        if working["uncertain"]:
            db.execute(
                "INSERT INTO gate_audit(station,date,action,run_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                (station, date, "发布失败", row["run_id"],
                 json.dumps({"uncertain": serial(working["uncertain"])}, ensure_ascii=False), _now()))
            db.commit()  # 拒绝发布也要保留审计痕迹，避免随异常回滚
            raise DomainError("REVIEW", "存在无法裁决时段，核定失败，旧版日账保留")
        new_version = version + 1
        db.execute(
            "UPDATE gate_ledger SET published_payload=working_payload, published_version=? "
            "WHERE station=? AND date=?", (new_version, station, date))
        db.execute(
            "INSERT INTO gate_audit(station,date,action,run_id,detail,created_at) VALUES(?,?,?,?,?,?)",
            (station, date, "发布", row["run_id"],
             json.dumps({"published_version": new_version,
                         "verified_m3": working["verified_m3"]}, ensure_ascii=False), _now()))
    return fetch_day(station, date)


def audit(station=None, date=None, limit=200):
    sql = "SELECT * FROM gate_audit WHERE 1=1"
    args = []
    if station:
        sql += " AND station=?"
        args.append(station)
    if date:
        sql += " AND date=?"
        args.append(date)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connection() as db:
        rows = db.execute(sql, args).fetchall()
    return [dict(row) for row in rows]


def _row_to_dict(row):
    return {
        "station": row["station"],
        "date": row["date"],
        "working": json.loads(row["working_payload"]),
        "published": json.loads(row["published_payload"]) if row["published_payload"] else None,
        "published_version": row["published_version"],
        "run_id": row["run_id"],
        "updated_at": row["updated_at"],
    }
